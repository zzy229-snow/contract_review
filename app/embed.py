"""bge-m3 模型单例:进程内只加载一次,FastAPI 启动时预热。"""
from typing import Any, Optional

from .config import BGE_MODEL_PATH

_model: Optional[Any] = None


def get_model() -> Any:
    global _model
    if _model is None:
        from FlagEmbedding import BGEM3FlagModel
        _model = BGEM3FlagModel(model_name_or_path=BGE_MODEL_PATH)
    return _model


def warmup() -> None:
    get_model()
