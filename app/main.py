"""FastAPI 主服务:5 个接口 + SSE 流式对话。启动时预热 bge-m3。"""
import json
import queue
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agent, db, embed, milvus_db
from .config import BGE_MODEL_PATH, DB_PATH, DEEPSEEK_API_KEY, UPLOAD_DIR
from .parse_pdf import parse_and_store

app = FastAPI(title="合同审查助手")
STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
def _startup() -> None:
    db.init_schema()          # 业务表(幂等)
    milvus_db.get_client()    # Milvus Lite 客户端
    # 换机器部署最容易踩的两个坑,启动时就把话说明白
    if not DEEPSEEK_API_KEY:
        print("[startup] ⚠ 未配置 DEEPSEEK_API_KEY → 对话会提示去填 .env(参照 .env.example)")
    if not Path(BGE_MODEL_PATH).is_dir():
        print(f"[startup] ⚠ bge-m3 模型目录不存在:{BGE_MODEL_PATH} → 检索会失败,见 部署说明.md")
    embed.warmup()            # bge-m3 预热(首次约 30~60s)
    print("[startup] bge-m3 已就绪")


# ---------- 模型 ----------
class ChatRequest(BaseModel):
    contract_id: int
    message: str
    # 多轮上下文:前端把之前的问答(纯文本)一起带过来;为空即"开始一次审查"
    history: Optional[List[Dict[str, Any]]] = None


class ReviewRequest(BaseModel):
    finding_id: int
    review_status: str  # 已采纳 / 已驳回


# ---------- 接口 ----------
@app.post("/file/upload")
async def upload_file(file: UploadFile = File(...)):
    """上传合同文件 → 落盘 → MinerU 解析 → contracts 入库。"""
    path = UPLOAD_DIR / file.filename
    path.write_bytes(await file.read())
    file_id = db.insert_file(file.filename, str(path), path.stat().st_size)
    contract_id = parse_and_store(file_id, str(path), file.filename)
    return {"file_id": file_id, "contract_id": contract_id, "status": "已解析"}


@app.get("/file/list")
def file_list():
    return {"files": db.list_files()}


@app.delete("/file/{file_id}")
def delete_file(file_id: int):
    """删除一个文件 → 连带删掉它生成的合同与全部审查意见(右侧合同列表/审查意见随之消失)。"""
    result = db.delete_file_cascade(file_id)
    if result["files"] == 0:
        return {"ok": False, "message": f"文件 {file_id} 不存在"}
    return {"ok": True, **result}


@app.get("/contract/list")
def contract_list():
    return {"contracts": db.list_contracts()}


@app.get("/finding/list")
def finding_list(contract_id: Optional[int] = None):
    return {"findings": db.list_findings(contract_id)}


@app.post("/finding/review")
def finding_review(req: ReviewRequest):
    """人工复核一条意见 → 顺带重算该合同状态(合同状态只由意见推导)。"""
    contract_id = db.update_finding_review(req.finding_id, req.review_status)
    contract_status = db.recompute_contract_status(contract_id) if contract_id is not None else None
    f = [x for x in db.list_findings() if x["id"] == req.finding_id]
    return {"ok": True, "finding": f[0] if f else None,
            "contract_id": contract_id, "contract_status": contract_status}


@app.post("/demo/prepare")
def demo_prepare():
    """预置演示合同(带 4 类问题的采购合同)→ 解析入库。答辩前调一次。"""
    from .demo import prepare_demo
    contract_id = prepare_demo()
    return {"contract_id": contract_id, "status": "已解析"}


@app.post("/chat")
def chat(req: ChatRequest):
    """SSE 流式:agent 跑在后台线程,事件经队列转发。"""
    q: queue.Queue = queue.Queue()

    def events():
        while True:
            item = q.get()
            if item is None:
                break
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"

    def worker():
        try:
            agent.run_analysis(req.contract_id, req.message, q.put, history=req.history)
        except Exception as e:  # 兜底:不让 SSE 挂死
            q.put({"type": "error", "content": f"{type(e).__name__}: {e}"})
        finally:
            q.put(None)

    threading.Thread(target=worker, daemon=True).start()
    return StreamingResponse(events(), media_type="text/event-stream")


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))
