"""MinerU 解析管线:PDF/DOCX/图片 → 全文 → 条款切分 → 落库。"""
import os
import re
import subprocess
from pathlib import Path
from typing import List, Tuple

from . import db
from .config import MINERU_EXE, MINERU_MODEL_SOURCE, MODELSCOPE_CACHE, PARSE_OUT_DIR

# 条款锚点:行首的 "第X条xxx" —— 兼容纯文本、"## 标题"、"**加粗**" 前缀(MinerU 输出)
HEAD_RE = re.compile(r"(?m)^\s*(?:#+\s*)?(?:\*\*)?(第[一二三四五六七八九十百零〇\d]+条)[^\n]*")

# 要素抽取用的几个锚点(供规则引擎复用)——按真实合同表述写稳
# 违约金比例:兼容 "违约金 30%" 与 "30% 的违约金" 两种写法
PENALTY_RE = re.compile(
    r"(?:违约金[^。\n]{0,30}?(\d+(?:\.\d+)?)\s*%|(\d+(?:\.\d+)?)\s*%\s*的违约金)"
)
# 付款期限:抓 "应于…90 日内支付" 类表述
PAYMENT_DAYS_RE = re.compile(
    r"(?:应于|应当于|须于)[^。\n]{0,25}?(\d+)\s*(?:日|天)内[^。\n]{0,10}?(?:支付|付款|交付)"
)
# 管辖法院:抓 "提交/由 XXX 市/区人民法院" 的具体法院名
FIXED_COURT_RE = re.compile(
    r"(?:提交|由)([\u4e00-\u9fa5]{2,12}(?:市|区|县|省))[^。\n]{0,10}?人民法院[^。\n]{0,20}"
)


def parse_document(src_path: str) -> Tuple[str, List[str]]:
    """调 MinerU(pipeline CPU 后端)解析文档,返回 (全文, 条款列表)。"""
    src = Path(src_path)
    out_root = PARSE_OUT_DIR
    env = dict(os.environ)
    env["MINERU_MODEL_SOURCE"] = MINERU_MODEL_SOURCE
    env["MODELSCOPE_CACHE"] = MODELSCOPE_CACHE

    cmd = [
        str(MINERU_EXE),
        "-p", str(src),
        "-o", str(out_root),
        "-b", "pipeline",
    ]
    proc = subprocess.run(
        cmd, env=env, capture_output=True, text=True, timeout=600,
        encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"MinerU 解析失败: {proc.stderr[-500:]}")
    if src.suffix.lower() == ".pdf":
        md_candidates = [
            out_root / src.stem / "auto" / f"{src.stem}.md",
            out_root / src.stem / "auto" / "content.md",
        ]
        md_path = next((p for p in md_candidates if p.exists()), None)
    else:  # docx / 图片走 auto 目录
        md_path = next(out_root.glob(f"{src.stem}/**/*.md"), None)
    if md_path is None:
        raise RuntimeError(f"MinerU 未产出 markdown({src.stem}/auto 下没有 .md)")
    full_text = md_path.read_text(encoding="utf-8")
    return full_text, split_clauses(full_text)


def split_clauses(full_text: str) -> List[str]:
    """按行首 '第X条' 锚点切分条款;切不到则整文单条。"""
    matches = list(HEAD_RE.finditer(full_text))
    if not matches:
        t = full_text.strip()
        return [t] if t else []
    clauses: List[str] = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(full_text)
        seg = full_text[m.start():end].strip()
        if seg:
            clauses.append(seg)
    return clauses


def parse_and_store(file_id: int, src_path: str, filename: str) -> int:
    """解析文档 → 写 contracts 表 → 更新 files 解析状态。返回 contract_id。"""
    try:
        full_text, clauses = parse_document(src_path)
    except Exception as e:
        db.update_file_status(file_id, "解析失败")
        raise
    # 演示合同缺要素时留空,要素由 agent 抽取后回填
    contract_id = db.insert_contract(
        filename=filename, full_text=full_text, clause_texts=clauses,
        status="待审", file_id=file_id,
    )
    db.update_file_status(file_id, "已解析")
    return contract_id
