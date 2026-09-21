"""全局配置:路径、模型、API 密钥、常量。

换机器部署只需要两件事(详见 .env.example 与 部署说明.md):
  1) 在项目根目录放一个 .env,填自己的 DEEPSEEK_API_KEY
  2) 把 bge-m3 模型放到 项目/models/bge-m3(或用环境变量 BGE_MODEL_PATH 指到别处)
其余路径全部基于项目目录推导,不需要改代码。
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
PARSE_OUT_DIR = DATA_DIR / "parse_out"
DB_PATH = DATA_DIR / "contract.db"
MILVUS_URI = str(DATA_DIR / "milvus.db")


# ---------- .env:项目自己的优先,本机旧课程项目目录只作兜底 ----------
def _load_env(path: Path) -> None:
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


_load_env(BASE_DIR / ".env")                      # ← 换机器后把配置写这里(参照 .env.example)
_load_env(Path(r"E:\Agent\langchain1.2\.env"))    # ← 仅开发者本机存在,别人机器没有也不影响


# ---------- LLM ----------
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
LLM_TEMPERATURE = 0.1


# ---------- 向量模型 bge-m3(本地加载,不联网) ----------
def _pick_bge_model() -> str:
    """查找顺序:环境变量 BGE_MODEL_PATH → 项目内 models/bge-m3 → 本机旧课程项目目录。"""
    for p in (
        os.getenv("BGE_MODEL_PATH", ""),
        str(BASE_DIR / "models" / "bge-m3"),
        r"E:\Agent\langchain1.2\assets\models\bge-m3",
    ):
        if p and Path(p).is_dir():
            return p
    return str(BASE_DIR / "models" / "bge-m3")   # 都没有:指向项目内,便于按说明放模型


BGE_MODEL_PATH = _pick_bge_model()
DIM = 1024  # bge-m3 稠密向量维度


# ---------- MinerU 解析 ----------
# 默认用项目 venv 里的 mineru;按平台自动选目录,也可用环境变量 MINERU_EXE 覆盖
_VENV_BIN = BASE_DIR / ".venv" / ("Scripts" if os.name == "nt" else "bin")
MINERU_EXE = Path(
    os.getenv("MINERU_EXE") or (_VENV_BIN / ("mineru.exe" if os.name == "nt" else "mineru"))
)
MINERU_MODEL_SOURCE = os.getenv("MINERU_MODEL_SOURCE", "modelscope")
MODELSCOPE_CACHE = str(BASE_DIR / "models")

# Milvus 集合
CLAUSE_COLLECTION = "clause_collection"
LAW_COLLECTION = "law_collection"

# 人审规则阈值
RULES = {
    "penalty_max": 20.0,        # 违约金比例 > 20% → 需人审
    "payment_days": 30,         # 付款账期 > 30 天 → 风险意见
    "required_words": ["保密"],  # 合同中必须出现的关键词(缺 → 需人审)
}

for d in (DATA_DIR, UPLOAD_DIR, PARSE_OUT_DIR):
    d.mkdir(parents=True, exist_ok=True)
