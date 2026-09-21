"""端到端测试:清空意见 → 跑 agent 完整流程 → 打印事件流 + 落库结果。
用法:cd E:/Agent/contract_review && .venv/Scripts/python.exe scripts/run_agent_test.py [合同ID]
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
from app.agent import run_analysis

contract_id = int(sys.argv[1]) if len(sys.argv) > 1 else 1


def main() -> None:
    with db.get_conn() as c:
        c.execute("DELETE FROM findings WHERE contract_id=?", (contract_id,))
        c.execute("UPDATE contracts SET status='待审' WHERE id=?", (contract_id,))

    events = []
    run_analysis(contract_id, "这份合同有什么风险?请完整审查并写入意见", events.append)

    for ev in events:
        t = ev["type"]
        if t == "tool_call":
            print(f"\n[TOOL] {ev['name']} {json.dumps(ev['arguments'], ensure_ascii=False)[:120]}")
        elif t == "tool_result":
            print(f"   => {ev['content'][:140].replace(chr(10), ' ')}")
        elif t == "text":
            print(f"[TEXT] {ev['content']}", end="")
        elif t == "done":
            print("\n[DONE]")

    print("\n=== 落库结果 ===")
    for f in db.list_findings(contract_id):
        tag = "人审" if f["need_review"] else "意见"
        print(f"  [{tag}] {f['risk_point']} | 法条: {f['law_cite'][:40]} | {f['review_status']}")
    c = db.get_contract(contract_id)
    print(f"合同状态: {c['status']}")

    n_review = sum(1 for f in db.list_findings(contract_id) if f["need_review"])
    assert n_review >= 3, f"应至少 3 条人审,实际 {n_review}"
    assert c["status"] == "需人审", f"合同状态应为需人审,实际 {c['status']}"
    print("\n✔ 验收通过:3 条人审 + 合同状态=需人审")


if __name__ == "__main__":
    main()
