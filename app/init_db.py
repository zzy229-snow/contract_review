"""初始化数据库:SQLite 建表+灌标准条款库/法条库,向量化写入 Milvus Lite。幂等(可反复跑)。"""
import sys
from pathlib import Path

from . import db, milvus_db
from .config import (
    BGE_MODEL_PATH,
    CLAUSE_COLLECTION,
    LAW_COLLECTION,
)
from .seed_data import LAWS, STANDARD_CLAUSES


def _reset_sqlite() -> None:
    """开发期重置:清掉业务表重建,避免重复造数据。"""
    with db.get_conn() as conn:
        conn.executescript(
            "DROP TABLE IF EXISTS findings; DROP TABLE IF EXISTS contracts;"
            " DROP TABLE IF EXISTS files; DROP TABLE IF EXISTS clauses; DROP TABLE IF EXISTS laws;"
        )
    db.init_schema()


def _seed_sqlite() -> dict:
    for c in STANDARD_CLAUSES:
        db.insert_clause(c["clause_type"], c["standard_text"], c["risk_level"], c["common_deviation"])
    for l in LAWS:
        db.insert_law(l["law_no"], l["content"], l["scenario"])
    return {"clauses": db.count_clauses(), "laws": db.count_laws()}


def _encode_to_milvus(model) -> dict:
    client = milvus_db.get_client()
    milvus_db.ensure_collection(client, CLAUSE_COLLECTION, reset=True)
    milvus_db.ensure_collection(client, LAW_COLLECTION, reset=True)

    # 向量的检索文本 = 类型/场景 + 正文 + 常见偏差,让它语义更完整
    clause_texts = [
        f"标准条款[{c['clause_type']}]:{c['standard_text']} 常见偏差:{c['common_deviation']}"
        for c in STANDARD_CLAUSES
    ]
    clause_metas = [{k: c[k] for k in ("clause_type", "risk_level")} for c in STANDARD_CLAUSES]

    law_texts = [
        f"[{l['scenario']}]{l['law_no']}:{l['content']}"
        for l in LAWS
    ]
    law_metas = [{"law_no": l["law_no"], "scenario": l["scenario"]} for l in LAWS]

    n_clause = milvus_db.insert_texts(client, CLAUSE_COLLECTION, clause_texts, clause_metas, model)
    n_law = milvus_db.insert_texts(client, LAW_COLLECTION, law_texts, law_metas, model)
    return {"clause_vectors": n_clause, "law_vectors": n_law}


def main() -> None:
    print("================ 初始化数据库 ================")
    _reset_sqlite()
    counts = _seed_sqlite()
    print(f"[SQLite] 条款库 {counts['clauses']} 条,法条库 {counts['laws']} 条 ✓")

    print("加载 bge-m3(首次较慢,请耐心)...")
    sys.path.insert(0, str(Path(BGE_MODEL_PATH).parent))
    from FlagEmbedding import BGEM3FlagModel

    model = BGEM3FlagModel(model_name_or_path=BGE_MODEL_PATH)
    vec_counts = _encode_to_milvus(model)
    print(f"[Milvus] 条款向量 {vec_counts['clause_vectors']} 条,法条向量 {vec_counts['law_vectors']} 条 ✓")

    # 冒烟测试:两个库各检一条
    client = milvus_db.get_client()
    clause_hits = milvus_db.hybrid_search(client, CLAUSE_COLLECTION, "付款账期 90 天,是否超出标准?", model, limit=2)
    law_hits = milvus_db.hybrid_search(client, LAW_COLLECTION, "违约金比例过高怎么办?", model, limit=2)
    print("\n[冒烟] 条款库检索『付款账期 90 天』:")
    for h in clause_hits:
        print("  •", h["metadata"].get("clause_type"), "|", h["text"][:60], "...")
    print("[冒烟] 法条库检索『违约金过高』:")
    for h in law_hits:
        print("  •", h["metadata"].get("law_no"), "|", h["text"][:40], "...")
    print("\n初始化完成 ✓")


if __name__ == "__main__":
    main()
