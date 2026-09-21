"""API 冒烟测试:5 个接口 + SSE 对话 + 反例。用法:.venv/Scripts/python.exe scripts/smoke_api.py [base_url]"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✔' if ok else '✘'} {name}" + (f" | {detail}" if detail else ""))


def main() -> None:
    print("== 接口冒烟测试 ==")
    r = httpx.get(f"{BASE}/")
    check("GET / 前端页", r.status_code == 200 and "合同审查助手" in r.text, f"{r.status_code}")

    r = httpx.get(f"{BASE}/contract/list")
    contracts = r.json().get("contracts", [])
    check("GET /contract/list", r.status_code == 200 and len(contracts) >= 1, f"{len(contracts)} 份合同")

    r = httpx.get(f"{BASE}/file/list")
    files = r.json().get("files", [])
    check("GET /file/list", r.status_code == 200 and len(files) >= 1, f"{len(files)} 个文件")

    cid = contracts[0]["id"]
    r = httpx.get(f"{BASE}/finding/list", params={"contract_id": cid})
    findings = r.json().get("findings", [])
    check("GET /finding/list", r.status_code == 200 and len(findings) >= 3, f"{len(findings)} 条意见")

    fid = findings[0]["id"]
    r = httpx.post(f"{BASE}/finding/review", json={"finding_id": fid, "review_status": "已采纳"})
    check("POST /finding/review", r.status_code == 200 and r.json().get("ok") is True, r.text[:60])
    r2 = httpx.get(f"{BASE}/finding/list", params={"contract_id": cid})
    status = next(f["review_status"] for f in r2.json()["findings"] if f["id"] == fid)
    check("复核状态已落库", status == "已采纳", status)

    print("== SSE 对话(问风险,看事件流) ==")
    with httpx.stream("POST", f"{BASE}/chat", json={"contract_id": cid, "message": "这份合同有什么风险?"}, timeout=180) as resp:
        ev_counts: dict = {}
        tool_names: list = []
        got_done = False
        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            ev = json.loads(line[6:])
            ev_counts[ev["type"]] = ev_counts.get(ev["type"], 0) + 1
            if ev["type"] == "tool_call":
                tool_names.append(ev["name"])
            if ev["type"] == "done":
                got_done = True
        check("SSE 收到 done", got_done, f"事件分布: {ev_counts}")
        check("工具调用链完整", set(tool_names) == {"query_contract", "search_clause", "search_law", "save_finding"},
              f"实际: {sorted(set(tool_names))}")

    print("== 反例:与合同无关的话题 ==")
    with httpx.stream("POST", f"{BASE}/chat", json={"contract_id": cid, "message": "今天天气怎么样?"}, timeout=120) as resp:
        tools_used = []
        text = []
        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            ev = json.loads(line[6:])
            if ev["type"] == "tool_call":
                tools_used.append(ev["name"])
            if ev["type"] == "text":
                text.append(ev["content"])
        full = "".join(text)
        check("反例:不调用工具", len(tools_used) == 0, f"tools={tools_used}")
        check("反例:礼貌回绝", ("无关" in full) or ("无法" in full) or ("合同" in full and "抱歉" in full), full[:60])

    print("== 完成 ==")


if __name__ == "__main__":
    main()
