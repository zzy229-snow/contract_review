"""四个工具函数:query_contract / search_clause / search_law / save_finding。
实现与 LLM 无关,可单独测试;LLM 只负责决定调用顺序与参数。
"""
import json
from typing import Any, Dict, List

from . import db, milvus_db
from .config import CLAUSE_COLLECTION, LAW_COLLECTION
from .embed import get_model
from .rules import apply_rules


def query_contract(contract_id: int) -> Dict[str, Any]:
    """查一份合同:要素 + 条款数 + 全文(截断给模型,防上下文爆炸)。"""
    c = db.get_contract(contract_id)
    if c is None:
        return {"error": f"合同 {contract_id} 不存在"}
    return {
        "contract_id": c["id"],
        "filename": c["filename"],
        "party_a": c["party_a"] or "(待抽取)",
        "party_b": c["party_b"] or "(待抽取)",
        "amount": c["amount"] or "(待抽取)",
        "term": c["term"] or "(待抽取)",
        "clause_count": c["clause_count"],
        "status": c["status"],
        "full_text": c["full_text"][:2500],
        "clause_titles": [t.strip().splitlines()[0][:30] for t in (c["clause_texts"] or [])][:20],
    }


def _fmt_hits(hits: List[Dict[str, Any]]) -> str:
    lines = []
    for h in hits:
        meta = h.get("metadata", {})
        lines.append(
            f"- {meta.get('clause_type') or meta.get('law_no') or '—'} "
            f"[相似度 {h['distance']}] {h['text'][:120]}"
        )
    return "\n".join(lines)


def search_clause(risk_desc: str, limit: int = 3) -> str:
    """语义检索标准条款库,返回命中条款文本。"""
    hits = milvus_db.hybrid_search(
        milvus_db.get_client(), CLAUSE_COLLECTION, risk_desc, get_model(), limit=limit
    )
    if not hits:
        return "标准条款库未命中任何条款"
    return f"标准条款库命中 {len(hits)} 条:\n" + _fmt_hits(hits)


def search_law(issue: str, limit: int = 2) -> str:
    """语义检索法条库,返回相关法条。"""
    hits = milvus_db.hybrid_search(
        milvus_db.get_client(), LAW_COLLECTION, issue, get_model(), limit=limit
    )
    if not hits:
        return "法条库未命中"
    return "相关法条:\n" + _fmt_hits(hits)


def save_finding(contract_id: int, findings: List[Dict[str, Any]]) -> str:
    """批量写审查意见 —— **替换语义**:本次结果覆盖该合同上一次的意见,避免重复堆积。

    任一 need_review=1 → 合同状态置为『需人审』,否则『已审』(由 db 统一推导)。
    """
    if not findings:
        return "没有写入任何意见(空列表)"
    removed = db.delete_findings(contract_id)      # 先清旧意见:一次审查 = 一份结论
    saved = 0
    for f in findings:
        db.insert_finding(
            contract_id=contract_id,
            risk_point=f.get("risk_point", "未命名风险"),
            quote=f.get("quote", ""),
            matched_clause=f.get("matched_clause", ""),
            law_cite=f.get("law_cite", ""),
            need_review=1 if f.get("need_review") else 0,
            reason=f.get("reason", ""),
        )
        saved += 1
    # 合同状态统一由意见推导(见 db.recompute_contract_status),不在这里单独维护
    status = db.recompute_contract_status(contract_id)
    return f"已替换 {removed} 条旧意见,写入 {saved} 条新意见,合同状态 → {status}"


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "query_contract",
            "description": "查询合同全文、条款与已抽取要素。审查任何合同前必须先调用它。",
            "parameters": {
                "type": "object",
                "properties": {"contract_id": {"type": "integer", "description": "合同 ID"}},
                "required": ["contract_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_clause",
            "description": "按风险描述语义检索标准条款库,得到标准表述/风险等级/常见偏差,用于比对合同条款。",
            "parameters": {
                "type": "object",
                "properties": {"risk_desc": {"type": "string", "description": "风险描述,如『付款账期 90 天是否超标准』"}},
                "required": ["risk_desc"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_law",
            "description": "检索相关法条作为审查意见的法律依据。",
            "parameters": {
                "type": "object",
                "properties": {"issue": {"type": "string", "description": "法律问题描述"}},
                "required": ["issue"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_finding",
            "description": "把一次审查的完整结论批量写入数据库。注意:这是替换语义——会先清空该合同此前的全部意见,本次结果覆盖上一次,不是追加。每条意见必须带:风险点、合同原文片段、命中条款/法条依据、是否需人审(need_review)、理由。仅在用户要求审查或重新审查整份合同时调用一次;用户只是追问某一条意见时不要调用它,直接回答。",
            "parameters": {
                "type": "object",
                "properties": {
                    "contract_id": {"type": "integer"},
                    "findings": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "risk_point": {"type": "string"},
                                "quote": {"type": "string", "description": "合同原文片段,必须真实摘自合同"},
                                "matched_clause": {"type": "string", "description": "命中的标准条款"},
                                "law_cite": {"type": "string", "description": "法条依据,如『民法典第 585 条』"},
                                "need_review": {"type": "boolean", "description": "true=需人审,false=可自动通过"},
                                "reason": {"type": "string"},
                            },
                            "required": ["risk_point", "quote", "need_review", "reason"],
                        },
                    },
                },
                "required": ["contract_id", "findings"],
            },
        },
    },
]


def dispatch(name: str, args: Dict[str, Any]) -> str:
    """工具分发:返回给 LLM 的字符串结果。"""
    try:
        if name == "query_contract":
            return json.dumps(query_contract(int(args.get("contract_id", 0))), ensure_ascii=False)
        if name == "search_clause":
            return search_clause(str(args.get("risk_desc", "")), limit=3)
        if name == "search_law":
            return search_law(str(args.get("issue", "")), limit=2)
        if name == "save_finding":
            return save_finding(int(args.get("contract_id", 0)), args.get("findings") or [])
        return f"未知工具 {name}"
    except Exception as e:
        return f"工具执行出错: {type(e).__name__}: {e}"
