"""人审规则引擎:『什么算需要人审』由代码规则决定,不由模型自由发挥。
命中任一硬规则 → need_review=1(进人审队列);软规则只出意见。
"""
from typing import Any, Dict, List

from .config import RULES
from .parse_pdf import FIXED_COURT_RE, PAYMENT_DAYS_RE, PENALTY_RE

# 本地可接受的管辖表述(命中任一 → 不算单方不利管辖)
OK_JURISDICTION_WORDS = ("被告住所地", "合同签订地", "甲方所在地", "原告住所地", "履行地")


def _extract_quotable(clause_text: str, pattern: "re.Pattern[str]") -> str:
    m = pattern.search(clause_text)
    return m.group(0) if m else ""


def apply_rules(contract: Dict[str, Any]) -> List[Dict[str, Any]]:
    """对一份合同跑全部规则,返回 findings 候选列表(未写库)。"""
    full_text = contract.get("full_text", "") or ""
    clause_texts: List[str] = contract.get("clause_texts", []) or []
    joined = "\n".join(clause_texts) if clause_texts else full_text
    findings: List[Dict[str, Any]] = []

    # —— 硬规则 1:违约金比例 > 20% → 需人审 ——
    p_max = 0.0
    p_quote = ""
    for m in PENALTY_RE.finditer(full_text):
        v = float(m.group(1) or m.group(2))
        if v > p_max:
            p_max, p_quote = v, m.group(0)
    if p_max > RULES["penalty_max"]:
        findings.append({
            "risk_point": f"违约金比例过高({p_max}%)",
            "quote": p_quote,
            "need_review": 1,
            "reason": f"违约金比例为 {p_max}%,超过标准上限 {RULES['penalty_max']}%,"
                      "过分高于实际损失时法院可依民法典第 585 条调整",
        })

    # —— 硬规则 2:管辖约定不利(具体法院且非本地锚点)→ 需人审 ——
    jur_m = FIXED_COURT_RE.search(joined)
    if jur_m:
        jur_text = jur_m.group(0)
        if not any(w in jur_text for w in OK_JURISDICTION_WORDS):
            findings.append({
                "risk_point": "管辖约定不利",
                "quote": jur_text,
                "need_review": 1,
                "reason": "约定管辖地与被告住所地/合同签订地等法定联结点不符,"
                          "增加我方维权成本(民事诉讼法第 35 条)",
            })

    # —— 硬规则 3:缺少保密条款 → 需人审 ——
    if "保密" not in full_text:
        findings.append({
            "risk_point": "缺少保密条款",
            "quote": "(全文未出现『保密』)",
            "need_review": 1,
            "reason": "合同中未约定保密义务,商业秘密无合同约束(民法典第 509 条、缔约过失保密义务)",
        })

    # —— 软规则:付款账期 > 30 天 → 出意见但不强制人审 ——
    pay_max = 0
    pay_quote = ""
    for m in PAYMENT_DAYS_RE.finditer(full_text):
        v = int(m.group(1))
        if 0 < v <= 365 and v > pay_max:
            pay_max, pay_quote = v, m.group(0)
    if pay_max > RULES["payment_days"]:
        findings.append({
            "risk_point": f"付款账期过长({pay_max}天)",
            "quote": pay_quote,
            "need_review": 0,
            "reason": f"付款账期 {pay_max} 天,超过标准 {RULES['payment_days']} 天,现金流风险",
        })

    return findings
