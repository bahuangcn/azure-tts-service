"""
Batch Synthesis 后台引擎：任务队列 → REST API 提交 → 轮询 → 下载 → 结果回写。

架构（与 worker.py 对称）：
  主线程                               后台 Batch Worker 线程 (×M)
  ────────                             ───────────────────────────
  POST /tts → _batch_queue.put(id)     _batch_queue.get() → 阻塞等待
                                         ↓
                                         1. 读取任务（SELECT）
                                         2. 更新状态 → processing
                                         3. submit_batch_job() → PUT Azure
                                         4. 轮询 get_batch_status() → GET
                                         5. Succeeded → download_batch_results()
                                         6. 更新状态 → completed（含 word_timings）
                                         异常 → failed（含 error 信息）

与 worker.py 的关键差异：
  - 不需要 Azure Speech SDK，纯 HTTP 调用 batch_client.py
  - 合成是异步的（提交 → 等几秒~几十秒 → 下载），而非同步阻塞
  - 词边界来自 REST API 的 .word.json，而非 SDK 的 synthesis_word_boundary 回调
"""
import json
import queue
import threading
import time
import traceback

from config import AUDIO_DIR, BATCH_POLL_INTERVAL, BATCH_MAX_TIMEOUT
from database import get_db
from worker import _mark_status, _mark_failed
from batch_client import submit_batch_job, get_batch_status, download_batch_results

# ── 共享任务队列 ────────────────────────────────────────────────────────────
# routes.py 通过 from batch_worker import _batch_queue 引用
_batch_queue: queue.Queue = queue.Queue()


# ── Worker 主循环 ───────────────────────────────────────────────────────────
def _batch_worker():
    """
    后台 Batch Worker 线程入口：无限循环从 _batch_queue 取任务并处理。

    退出方式：
      - 向队列放入 None（sentinel），break 退出
      - 线程是 daemon 模式，主进程退出时自动终止

    异常处理：
      合成中任何异常被捕获 → 标记 failed，不导致线程退出。
    """
    while True:
        task_id = _batch_queue.get()        # 阻塞等待新任务

        if task_id is None:                 # sentinel：优雅关闭
            break

        try:
            _batch_synthesize(task_id)
        except Exception as e:
            _mark_failed(task_id, str(e))
        finally:
            _batch_queue.task_done()        # 通知队列任务完成


# ── Batch 合成核心 ──────────────────────────────────────────────────────────
def _batch_synthesize(task_id: str):
    """
    执行完整的 Batch 合成流程：

    1. 从 DB 读取任务（text, voice, rate）
    2. 更新状态 → processing
    3. PUT 提交到 Azure Batch Synthesis API
    4. 存储 synthesis_id + azure_status
    5. 轮询直到终态（Succeeded / Failed）或超时
    6. 下载 ZIP、解压音频和词边界
    7. 更新状态 → completed（含 audio_file, word_timings, total_ms）
    """
    # ── 1. 读取任务 ────────────────────────────────────────────────────
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if not row:
            return                          # 任务不存在（可能被删除）
        task = dict(row)

    # ── 2. 更新状态 ────────────────────────────────────────────────────
    _mark_status(task_id, "processing")

    text = task["text"]
    voice = task["voice"]
    rate = task.get("rate", "+20%")

    # ── 3. 提交到 Azure Batch API ──────────────────────────────────────
    result = submit_batch_job(text, voice, rate)
    synthesis_id = result["synthesis_id"]
    azure_status = result["azure_status"]

    # ── 4. 存储 synthesis 元数据 ────────────────────────────────────────
    _mark_status(task_id, "processing",
                 synthesis_id=synthesis_id,
                 azure_status=azure_status)

    # ── 5. 轮询直到终态 ────────────────────────────────────────────────
    start_time = time.time()
    while azure_status in ("NotStarted", "Running"):
        if time.time() - start_time > BATCH_MAX_TIMEOUT:
            raise RuntimeError(
                f"Batch job timed out after {BATCH_MAX_TIMEOUT}s "
                f"(last status: {azure_status})"
            )

        time.sleep(BATCH_POLL_INTERVAL)

        status_data = get_batch_status(synthesis_id)
        azure_status = status_data.get("status", azure_status)

        # 每次轮询更新 azure_status 到 DB（前端可通过 GET /tts/{id} 看到进度）
        _mark_status(task_id, "processing", azure_status=azure_status)

    # ── 6. 处理终态 ────────────────────────────────────────────────────
    if azure_status == "Succeeded":
        result_url = status_data.get("outputs", {}).get("result")
        if not result_url:
            raise RuntimeError(
                "Batch job status=Succeeded but no result_url in response"
            )

        # ── 7. 下载并解压结果 ───────────────────────────────────────
        results = download_batch_results(result_url, task_id, AUDIO_DIR)

        # ── 8. 回写完成状态 ─────────────────────────────────────────
        wt_json = json.dumps(results["word_timings"], ensure_ascii=False)
        _mark_status(
            task_id,
            "completed",
            audio_file=results["audio_file"],
            word_timings=wt_json,
            total_ms=results["total_ms"],
            result_url=result_url,
        )

    elif azure_status == "Failed":
        error = status_data.get("error", {})
        error_msg = error.get("message", "Unknown batch failure")
        raise RuntimeError(f"Batch job failed: {error_msg}")

    else:
        # 未知状态（如 Cancelled）
        raise RuntimeError(f"Unexpected Azure batch status: {azure_status}")
