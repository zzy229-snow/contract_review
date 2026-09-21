"""Agent 主循环:deepseek-chat + 强制工具调用(首轮锚定 query_contract)。
事件回调(event_cb)把文本增量/工具调用推给 SSE。
规则引擎在 LLM 之后兜底:模型漏报的风险,规则一定补上(『规则先行』)。
"""
import json
from typing import Any, Callable, Dict, List, Optional

from openai import OpenAI

from . import db
from .config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, LLM_MODEL, LLM_TEMPERATURE
from .rules import apply_rules
from .tools import TOOL_SCHEMAS, dispatch

SYSTEM_PROMPT = """你是合同审查助手,负责审查业务合同。始终使用中文回复。

工作流程(必须遵守):
1. 先用 query_contract 拿到合同全文与条款。
2. 逐条识别风险:付款/违约/保密/管辖/交付/质量。
3. 对每个疑似风险点,用 search_clause 比对标准条款库(看常见偏差和风险等级),
   用 search_law 找法律依据。
4. 审查完成后,调用 save_finding 一次性写入全部意见。

铁律:
- 每条意见的 quote 必须是合同原文的真实片段,禁止编造。
- 判定 need_review 遵循规则:违约金比例超过 20%、管辖约定对己方不利、
  缺少保密条款 → true;付款账期偏长 → false(仅提示)。
- 回答要给出明确的『可自动通过 / 需人审』结论,并说清命中哪条规则。
- 全程只能输出中文:不得出现任何英文单词或英文句子(调用工具前的思考、预告、
  过渡语同样算),像 need_review 这类字段名只允许出现在工具参数里,不得写进给用户的文字。
- 调用工具前不要写任何旁白或预告(例如"我将逐条比对各条款…")。检索过程不需要向用户
  解释:直接用工具干活,中文结论留到所有工具调用结束后一次性给出。
- 边界(第一优先级):如果用户消息与合同审查无关(闲聊、天气、新闻、代码、
  其他领域问题),【禁止调用任何工具】,直接回复类似
  「抱歉,我只能处理合同审查相关的问题。」无论上下文中正在审查哪份合同都先停下。"""


def _merge_tool_calls(acc: List[Dict[str, Any]], tool_calls: Any) -> None:
    """把流式分片的 tool_calls delta 增量合并进累积列表(跨 chunk 保持状态)。"""
    for tc in tool_calls or []:
        idx = tc.index
        while len(acc) <= idx:
            acc.append({"id": "", "name": "", "arguments": ""})
        if tc.id:
            acc[idx]["id"] = tc.id
        if tc.function:
            if tc.function.name:
                acc[idx]["name"] = tc.function.name
            if tc.function.arguments:
                acc[idx]["arguments"] += tc.function.arguments


# 话题前置过滤:与合同审查无关的消息直接回绝,不发起 LLM 调用(演示稳定第一优先)
CONTRACT_KEYWORDS = (
    "合同", "条款", "风险", "审查", "违约", "付款", "保密", "管辖", "发票",
    "交付", "甲方", "乙方", "验收", "金额", "质保", "仲裁", "诉讼", "签订",
    "价款", "文件", "账期", "违约金", "这份", "该合同",
)


def _prefilter(message: str) -> bool:
    """True=与合同相关(继续审查流程);False=无关,直接回绝。"""
    return any(k in message for k in CONTRACT_KEYWORDS)


def _sanitize_history(
    history: Optional[List[Dict[str, Any]]],
    max_msgs: int = 10,
    max_chars: int = 2000,
) -> List[Dict[str, str]]:
    """把前端传来的历史裁成安全的 messages:只留 user/assistant 纯文本,限量限长。"""
    out: List[Dict[str, str]] = []
    for m in (history or [])[-max_msgs:]:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role", ""))
        content = str(m.get("content", "") or "").strip()
        if role in ("user", "assistant") and content:
            out.append({"role": role, "content": content[:max_chars]})
    return out


def _existing_findings_brief(contract_id: int) -> str:
    """把已入库的意见压成带编号的清单 —— 模型据此回答"第二条怎么样"这类追问。"""
    rows = db.list_findings(contract_id)
    if not rows:
        return "该合同当前还没有审查意见。"
    lines = [
        f"{i}. {f['risk_point']}(判定:{'需人审' if f['need_review'] else '可自动通过'};"
        f"复核:{f['review_status']};依据:{f.get('law_cite') or '—'})"
        for i, f in enumerate(rows, 1)
    ]
    return "该合同已有的审查意见(按顺序编号,追问时直接引用编号):\n" + "\n".join(lines)


def run_analysis(
    contract_id: int,
    user_message: str,
    event_cb: Callable[[Dict[str, Any]], None],
    max_turns: int = 6,
    history: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """执行一轮对话,所有输出经 event_cb 推送。

    - history 为空 → 视为"开始一次审查"(走关键词闸门 + 允许规则引擎兜底)
    - history 非空 → 视为"追问"(跳过闸门,并告知模型已有意见,不要重跑审查/重写库)
    """
    hist = _sanitize_history(history)
    if not hist and not _prefilter(user_message):
        # 关键词闸门只对会话首轮生效;否则"那第二条呢?"这类追问会被误杀
        reply = "抱歉,我只能处理合同审查相关的问题。"
        event_cb({"type": "text", "content": reply})
        event_cb({"type": "done", "summary": reply, "saved": False})
        return
    if not DEEPSEEK_API_KEY:
        # 换机器后最常见的问题:没配 key。给一句能照着做的提示,而不是丢一堆鉴权异常
        reply = ("未配置 DEEPSEEK_API_KEY:请在项目根目录新建 .env(可参照 .env.example)"
                 "填好自己的 key,然后重启服务;或设置同名环境变量。")
        event_cb({"type": "text", "content": reply})
        event_cb({"type": "done", "summary": reply, "saved": False})
        return
    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        # 必须显式告知目标合同 ID:否则模型只能猜(实测它会一律猜 1,导致读错文档、
        # 意见写进别的合同)
        {"role": "system", "content": (
            f"本次要审查的合同 ID = {contract_id}。"
            f"调用 query_contract、save_finding 时必须使用这个 ID({contract_id}),"
            f"禁止使用其他 ID 或凭印象猜测。"
        )},
    ]
    if hist:
        messages.append({"role": "system", "content": _existing_findings_brief(contract_id)})
        messages.append({"role": "system", "content": (
            "用户是在追问上面已给出的意见,只回答他问的那一点(引用意见编号与依据即可),"
            "不要重复整份审查,也不要调用 save_finding —— 除非用户明确要求重新审查整份合同。"
        )})
        messages.extend(hist)
    messages.append({"role": "user", "content": user_message})
    called_save = False

    for _ in range(max_turns):
        stream = client.chat.completions.create(
            model=LLM_MODEL,
            messages=messages,
            tools=TOOL_SCHEMAS,
            tool_choice="required" if _ == 0 else "auto",
            temperature=LLM_TEMPERATURE,
            stream=True,
        )
        text_delta = ""
        tool_calls: List[Dict[str, Any]] = []
        finish_reason: Optional[str] = None
        for chunk in stream:
            choice = chunk.choices[0] if chunk.choices else None
            if not choice:
                continue
            if choice.delta and choice.delta.content:
                text_delta += choice.delta.content
                event_cb({"type": "text", "content": choice.delta.content})
            if choice.delta and choice.delta.tool_calls:
                _merge_tool_calls(tool_calls, choice.delta.tool_calls)
            if choice.finish_reason:
                finish_reason = choice.finish_reason

        # 本轮输出落回消息历史
        msg: Dict[str, Any] = {"role": "assistant", "content": text_delta or None}
        if tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": tc["arguments"] or "{}"},
                }
                for tc in tool_calls if tc["id"]
            ]
        messages.append(msg)
        if not tool_calls:
            break  # 纯文本回复,循环结束

        # 这一轮带工具调用 → 轮内文本属于"我先去做…"式旁白(实测模型会吐英文),
        # 已经逐字推给前端了,这里发一个撤回事件让前端把它清掉,只保留最终中文结论
        if tool_calls and text_delta.strip():
            event_cb({"type": "text_reset", "reason": "narration_dropped"})

        # 执行工具并回填结果
        for tc in tool_calls:
            raw_args = tc["arguments"] or "{}"
            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError:
                try:
                    args = json.loads(raw_args.strip())
                except json.JSONDecodeError as e:
                    event_cb({"type": "text",
                              "content": f"\n[警告] 工具参数解析失败({tc['name']}): {raw_args[:80]!r}"})
                    args = {}
            # 防御:即使提示了 ID,模型仍可能猜错 → 读写前强制对齐本次目标合同,
            # 避免"意见写进别的合同"这种脏数据
            if tc["name"] in ("query_contract", "save_finding") and args.get("contract_id") != contract_id:
                print(f"[agent] 模型给的 contract_id={args.get('contract_id')!r} 与目标 {contract_id} 不符,已纠正", flush=True)
                args["contract_id"] = contract_id
            event_cb({"type": "tool_call", "name": tc["name"], "arguments": args})
            result = dispatch(tc["name"], args)
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
            event_cb({"type": "tool_result", "name": tc["name"], "content": result[:400]})
            if tc["name"] == "save_finding":
                called_save = True

    # —— 规则引擎兜底:仅"首轮审查"且模型没写意见时补录 ——
    # 追问绝不能触发它:否则会把用户已复核的结果整体替换成规则版本
    contract = db.get_contract(contract_id)
    if contract and not called_save and not hist:
        from .tools import save_finding
        extra = [f for f in apply_rules(contract) if f["need_review"] == 1]
        if extra:
            save_finding(contract_id, extra)
            event_cb({
                "type": "text",
                "content": f"\n\n[规则引擎兜底] 模型未写入意见,已按规则补录 {len(extra)} 条需人审项。",
            })

    event_cb({"type": "done", "summary": text_delta, "saved": called_save})
