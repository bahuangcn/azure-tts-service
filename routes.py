"""
FastAPI 路由定义：TTS 任务的 CRUD + 音频下载 + 健康检查。

使用 APIRouter 而非直接 @app 装饰器：
  - 避免 routes.py ↔ app.py 循环导入
  - app.py 通过 app.include_router(router) 注册所有路由
"""
import json
import traceback
import uuid
import sqlite3
from datetime import datetime

from fastapi import APIRouter, HTTPException, Body
from fastapi.responses import FileResponse, Response

from config import DEFAULT_VOICE, DEFAULT_RATE, AUDIO_DIR
from database import get_db
from worker import _queue
from logger import get_logger

log = get_logger(__name__)

router = APIRouter()


# ── 响应工具函数 ────────────────────────────────────────────────────────────
def _task_to_dict(row: sqlite3.Row) -> dict:
    """
    将 sqlite3.Row 转为普通 dict，并处理特殊字段。

    特殊处理：
      - word_timings：从 JSON 字符串解析为 list[dict]
      - text：删除，不在列表接口中暴露用户原始文本（隐私 + 减少响应体积）

    仅在 list_tasks 中使用，get_task 使用内联解析以保留完整字段。
    """
    d = dict(row)
    if d.get("word_timings"):
        d["word_timings"] = json.loads(d["word_timings"])
    d.pop("text", None)
    return d


# ── 端点：提交 TTS 任务 ────────────────────────────────────────────────────
@router.post("/tts")
def create_tts_task(
    text: str = Body(...),
    voice: str = Body(default=DEFAULT_VOICE),
    rate: str = Body(default=DEFAULT_RATE),
):
    """
    提交文本合成任务，立即返回 task_id。

    请求体 (JSON)：
      text  — 要合成的文本（必填，不能为空或全空白）
      voice — Azure 语音名称，默认 zh-CN-XiaochenNeural
      rate  — 语速，如 "+20%" "-10%" "1.0"，对应 SSML prosody rate

    返回：
      200  {"task_id": "tts_...", "status": "pending"}
      400  text 为空时

    流程：
      DB 写入 pending 任务 → 入队 → 立即返回
    """
    if not text.strip():
        raise HTTPException(400, "text is empty")

    # 生成唯一任务 ID（tts_ + 12 位 hex）
    task_id = f"tts_{uuid.uuid4().hex[:12]}"
    now = datetime.utcnow().isoformat()

    with get_db() as conn:
        conn.execute(
            "INSERT INTO tasks (task_id, text, voice, rate, mode, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'sdk', ?, ?)",
            (task_id, text, voice, rate, now, now),
        )
        conn.commit()

    _queue.put(task_id)

    return {"task_id": task_id, "status": "pending"}


# ── 端点：查询任务详情 ──────────────────────────────────────────────────────
@router.get("/tts/{task_id}")
def get_task(task_id: str):
    """
    查询单个任务的状态和结果。

    返回字段：
      task_id, status, voice, rate, created_at, updated_at,
      error（仅 failed 时）,
      word_timings（仅 completed 时，list[{"text","start_ms","end_ms"}]）,
      audio_url（仅 completed 时，"/tts/audio/{task_id}"）,
      text（原始文本，单任务查询中返回，列表查询隐藏）

    状态枚举：pending → processing → completed | failed
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "task not found")

    d = dict(row)

    # JSON 字符串 → Python 对象
    if d.get("word_timings"):
        d["word_timings"] = json.loads(d["word_timings"])

    # 完成的任务附加下载 URL（相对路径）
    if d["status"] == "completed":
        d["audio_url"] = f"/azure_api/tts/audio/{d['task_id']}"
        d["timing_url"] = f"/azure_api/tts/{d['task_id']}/timing"

    return d


# ── 端点：下载音频文件 ──────────────────────────────────────────────────────
@router.get("/tts/audio/{task_id}")
def download_audio(task_id: str):
    """
    下载已完成任务的 MP3 音频文件。

    返回：
      200  application/octet-stream 响应（FileResponse）
      404  任务不存在 / 未完成 / 音频文件被删除

    注意：
      下载路径使用 task_id 而非文件名，避免暴露内部命名规则。
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT audio_file FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()

    # 任务不存在 或 尚未完成（audio_file 为 NULL）
    if not row or not row["audio_file"]:
        raise HTTPException(404, "audio not found")

    filepath = AUDIO_DIR / row["audio_file"]
    if not filepath.exists():
        raise HTTPException(404, "audio file missing on disk")

    return FileResponse(str(filepath), media_type="audio/mpeg")


# ── 端点：下载词级时间戳文件 ──────────────────────────────────────────────────
@router.get("/tts/{task_id}/timing")
def download_timing(task_id: str):
    """
    下载已完成任务的词级时间戳 JSON 文件。

    返回：
      200  application/json，Content-Disposition: attachment
      404  任务不存在 / 未完成（word_timings 为 NULL）

    与 audio 下载不同：timing 数据来自 DB JSON 列而非磁盘文件，
    因此不依赖 FileResponse，直接构造 Response 返回。
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT word_timings FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()

    # 任务不存在 或 尚未完成（word_timings 为 NULL）
    if not row or not row["word_timings"]:
        raise HTTPException(404, "timing not found")

    return Response(
        content=row["word_timings"],
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{task_id}_timing.json"'
        },
    )


# ── 端点：任务列表 ──────────────────────────────────────────────────────────
@router.get("/tts")
def list_tasks(limit: int = 50):
    """
    获取最近的任务列表（按创建时间倒序）。

    参数：
      limit — 最大返回条数，默认 50

    返回：
      list[dict]，每个元素不含 text 字段（原始文本仅在单任务查询中返回）
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_task_to_dict(r) for r in rows]


# ── 端点：删除任务 ──────────────────────────────────────────────────────────
@router.delete("/tts/{task_id}")
def delete_task(task_id: str):
    """
    删除任务及其音频文件。

    操作：
      1. 如任务有音频文件，从磁盘删除（文件不存在不报错）
      2. 从 DB 删除任务记录

    返回：
      200  {"status": "deleted"}
      404  任务不存在
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT audio_file FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()

        if not row:
            raise HTTPException(404, "task not found")

        # 删除磁盘上的音频文件
        if row["audio_file"]:
            try:
                (AUDIO_DIR / row["audio_file"]).unlink(missing_ok=True)
            except OSError:
                log.warning(f"删除音频文件失败: {traceback.format_exc()}")

        # 删除数据库记录
        conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
        conn.commit()

    return {"status": "deleted"}


# ── 端点：健康检查 ──────────────────────────────────────────────────────────
@router.get("/health")
def health():
    """
    服务健康检查。

    返回：
      {"status": "ok", "queue_size": <int>}

    queue_size 可用于监控积压情况：
      - 0    = 空闲，队列无待处理任务
      - >100 = 需要关注，处理速度跟不上提交速度
    """
    return {
        "status": "ok",
        "queue_size": _queue.qsize(),
    }
