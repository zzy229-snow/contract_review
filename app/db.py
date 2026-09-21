"""SQLite 数据层:5 张业务表 + DAO。业务数据(文件/合同/意见)与向量库(Milvus)分工。"""
import json
import sqlite3
from typing import Any, Dict, List, Optional

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    filepath TEXT NOT NULL,
    size INTEGER DEFAULT 0,
    upload_time TEXT DEFAULT (datetime('now','localtime')),
    parse_status TEXT DEFAULT '待解析'
);
CREATE TABLE IF NOT EXISTS clauses(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    clause_type TEXT NOT NULL,
    standard_text TEXT NOT NULL,
    risk_level TEXT DEFAULT '中',
    common_deviation TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS contracts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    party_a TEXT DEFAULT '',
    party_b TEXT DEFAULT '',
    amount TEXT DEFAULT '',
    term TEXT DEFAULT '',
    full_text TEXT DEFAULT '',
    clause_texts TEXT DEFAULT '[]',
    clause_count INTEGER DEFAULT 0,
    status TEXT DEFAULT '待审',
    created_time TEXT DEFAULT (datetime('now','localtime')),
    file_id INTEGER
);
CREATE TABLE IF NOT EXISTS laws(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    law_no TEXT NOT NULL,
    content TEXT NOT NULL,
    scenario TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS findings(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER,
    risk_point TEXT NOT NULL,
    quote TEXT DEFAULT '',
    matched_clause TEXT DEFAULT '',
    law_cite TEXT DEFAULT '',
    need_review INTEGER DEFAULT 0,
    reason TEXT DEFAULT '',
    review_status TEXT DEFAULT '待复核',
    created_time TEXT DEFAULT (datetime('now','localtime'))
);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_schema() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)


# ---------- files ----------
def insert_file(filename: str, filepath: str, size: int) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO files(filename, filepath, size) VALUES(?,?,?)",
            (filename, filepath, size),
        )
        return cur.lastrowid


def update_file_status(file_id: int, status: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE files SET parse_status=? WHERE id=?", (status, file_id))


def list_files() -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM files ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]


# ---------- clauses(标准条款库,向量化文本也取自这里) ----------
def insert_clause(clause_type: str, standard_text: str, risk_level: str, common_deviation: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO clauses(clause_type, standard_text, risk_level, common_deviation) VALUES(?,?,?,?)",
            (clause_type, standard_text, risk_level, common_deviation),
        )
        return cur.lastrowid


def count_clauses() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM clauses").fetchone()[0]


def all_clauses() -> List[Dict[str, Any]]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM clauses").fetchall()]


# ---------- laws ----------
def insert_law(law_no: str, content: str, scenario: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO laws(law_no, content, scenario) VALUES(?,?,?)",
            (law_no, content, scenario),
        )
        return cur.lastrowid


def count_laws() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM laws").fetchone()[0]


def all_laws() -> List[Dict[str, Any]]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM laws").fetchall()]


# ---------- contracts ----------
def insert_contract(
    filename: str,
    party_a: str = "",
    party_b: str = "",
    amount: str = "",
    term: str = "",
    full_text: str = "",
    clause_texts: Optional[List[str]] = None,
    status: str = "待审",
    file_id: Optional[int] = None,
) -> int:
    clause_texts = clause_texts or []
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO contracts(filename, party_a, party_b, amount, term, full_text, clause_texts, clause_count, status, file_id)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (filename, party_a, party_b, amount, term, full_text,
             json.dumps(clause_texts, ensure_ascii=False), len(clause_texts), status, file_id),
        )
        return cur.lastrowid


def get_contract(contract_id: int) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM contracts WHERE id=?", (contract_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["clause_texts"] = json.loads(d.get("clause_texts") or "[]")
        # 状态也实时推导:库里那一列可能停留在人工复核之前(道理同 list_contracts)
        frows = conn.execute(
            "SELECT need_review, review_status FROM findings WHERE contract_id=?",
            (contract_id,),
        ).fetchall()
        d["status"] = _derive_status(
            bool(frows),
            any(r["need_review"] and r["review_status"] == "待复核" for r in frows),
        )
        return d


def update_contract_fields(contract_id: int, party_a: str, party_b: str, amount: str, term: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE contracts SET party_a=?, party_b=?, amount=?, term=? WHERE id=?",
            (party_a, party_b, amount, term, contract_id),
        )


def update_contract_status(contract_id: int, status: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE contracts SET status=? WHERE id=?", (status, contract_id))


def _derive_status(has_findings: bool, has_pending_review: bool) -> str:
    """合同状态规则(全项目唯一一份):无意见=待审;有需人审且未复核=需人审;其余=已审。"""
    if not has_findings:
        return "待审"
    return "需人审" if has_pending_review else "已审"


def recompute_contract_status(contract_id: int) -> str:
    """把推导出来的状态写回 contracts.status —— 人工复核、Agent 写意见后调用。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT need_review, review_status FROM findings WHERE contract_id=?",
            (contract_id,),
        ).fetchall()
        status = _derive_status(
            bool(rows),
            any(r["need_review"] and r["review_status"] == "待复核" for r in rows),
        )
        conn.execute("UPDATE contracts SET status=? WHERE id=?", (status, contract_id))
        return status


def _status_map(conn) -> Dict[int, str]:
    """一次算出所有合同的状态(两条聚合查询,不逐条遍历)。"""
    has = {r["contract_id"] for r in conn.execute("SELECT DISTINCT contract_id FROM findings")}
    pending = {
        r["contract_id"] for r in conn.execute(
            "SELECT DISTINCT contract_id FROM findings"
            " WHERE need_review=1 AND review_status='待复核'"
        )
    }
    return {cid: _derive_status(cid in has, cid in pending) for cid in (has | pending)}


def list_contracts() -> List[Dict[str, Any]]:
    """列表里的 status 实时推导,不读库里那一列 —— 否则人工复核前写死的状态会一直挂在界面上。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, filename, party_a, party_b, amount, term, clause_count, status, created_time"
            " FROM contracts ORDER BY id DESC"
        ).fetchall()
        smap = _status_map(conn)
        out = []
        for r in rows:
            d = dict(r)
            d["status"] = smap.get(d["id"], "待审")
            out.append(d)
        return out


# ---------- findings ----------
def insert_finding(
    contract_id: int,
    risk_point: str,
    quote: str,
    matched_clause: str,
    law_cite: str,
    need_review: int,
    reason: str,
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO findings(contract_id, risk_point, quote, matched_clause, law_cite, need_review, reason)"
            " VALUES(?,?,?,?,?,?,?)",
            (contract_id, risk_point, quote, matched_clause, law_cite, need_review, reason),
        )
        return cur.lastrowid


def list_findings(contract_id: Optional[int] = None) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM findings"
    args: tuple = ()
    if contract_id is not None:
        sql += " WHERE contract_id=?"
        args = (contract_id,)
    sql += " ORDER BY id DESC"
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def delete_findings(contract_id: int) -> int:
    """删除某合同下的全部意见,返回删除条数(重新审查时用于"替换"上一次结果)。"""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM findings WHERE contract_id=?", (contract_id,))
        return cur.rowcount


def delete_file_cascade(file_id: int) -> Dict[str, Any]:
    """删除一个文件,并级联删除它生成的合同与这些合同的全部审查意见。

    返回 {"files": n, "contracts": n, "findings": n, "contract_ids": [...]},
    其中 contract_ids 供前端判断"删掉的是不是当前选中的合同"。
    """
    with get_conn() as conn:
        cids = [
            r["id"] for r in conn.execute(
                "SELECT id FROM contracts WHERE file_id=?", (file_id,)
            ).fetchall()
        ]
        n_find = 0
        n_cont = 0
        if cids:
            marks = ",".join("?" * len(cids))
            n_find = conn.execute(
                f"DELETE FROM findings WHERE contract_id IN ({marks})", cids
            ).rowcount
            n_cont = conn.execute(
                f"DELETE FROM contracts WHERE id IN ({marks})", cids
            ).rowcount
        n_file = conn.execute("DELETE FROM files WHERE id=?", (file_id,)).rowcount
    return {"files": n_file, "contracts": n_cont, "findings": n_find, "contract_ids": cids}


def update_finding_review(finding_id: int, review_status: str) -> Optional[int]:
    """更新复核状态,并返回该意见所属的合同 ID(供调用方重算合同状态)。

    返回 None 表示该意见不存在。
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT contract_id FROM findings WHERE id=?", (finding_id,)
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE findings SET review_status=? WHERE id=?",
            (review_status, finding_id),
        )
        return row["contract_id"]
