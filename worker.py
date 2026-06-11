"""
后台合成引擎：任务队列 → TTS 合成 → 结果回写。

架构：
  主线程                          后台 Worker 线程 (×N)
  ────────                       ──────────────────────
  POST /tts → _queue.put(id)     _queue.get() → 阻塞等待
                                  ↓
                                  1. 读取任务（SELECT）
                                  2. 更新状态 → processing
                                  3. 构建 SSML → 调用 Azure Speech SDK
                                  4. 收集 word_boundary 回调（词级时间戳）
                                  5. mutagen 读取 MP3 总时长
                                  6. 更新状态 → completed（含 word_timings）
                                  异常 → failed（含 error 信息）
"""
import json
import os
import queue
import threading
import traceback
from pathlib import Path

import azure.cognitiveservices.speech as speechsdk
import mutagen

from config import SPEECH_KEY, SPEECH_REGION, AUDIO_DIR, TTS_MAX_CHUNK_CHARS, TTS_SDK_TIMEOUT
from database import get_db
from chunker import split_text, concat_audio_files, merge_word_timings
from logger import get_logger

log = get_logger(__name__)

# ── 共享任务队列 ────────────────────────────────────────────────────────────
# 线程安全的无界 FIFO 队列，routes.py 通过 from worker import _queue 引用
_queue: queue.Queue = queue.Queue()


# ── Worker 主循环 ───────────────────────────────────────────────────────────
def _worker():
    """
    后台 Worker 线程入口：无限循环从队列取任务并合成。

    退出方式：
      - 向队列放入 None（sentinel 值），本函数会 break 退出
      - 线程是 daemon 模式，主进程退出时自动终止

    异常处理：
      合成中任何异常被捕获 → 任务标记为 failed，不导致线程退出。
    """
    while True:
        task_id = _queue.get()          # 阻塞等待新任务

        if task_id is None:             # sentinel：优雅关闭
            break

        try:
            _synthesize(task_id)
        except Exception as e:
            _mark_failed(task_id, str(e))
        finally:
            _queue.task_done()          # 通知队列任务完成（配合 Queue.join）


# ── 数据库状态更新 ─────────────────────────────────────────────────────────
def _mark_status(task_id: str, status: str, **extra):
    """
    更新任务状态及可选的额外字段。

    使用动态 SQL 构建避免多函数重复：
      UPDATE tasks SET status=?, updated_at=?, <extra_keys> WHERE task_id=?

    extra 键值示例：
      _mark_status(tid, "completed", audio_file="xxx.mp3", total_ms=12345)
    """
    from datetime import datetime

    now = datetime.utcnow().isoformat()
    parts = ["status = ?", "updated_at = ?"]     # 必更字段
    vals = [status, now]

    # 动态拼接额外字段（audio_file, word_timings, total_ms, error 等）
    for k, v in extra.items():
        parts.append(f"{k} = ?")
        vals.append(v)

    query = f"UPDATE tasks SET {', '.join(parts)} WHERE task_id = ?"
    vals.append(task_id)

    with get_db() as conn:
        conn.execute(query, vals)
        conn.commit()


def _mark_failed(task_id: str, error: str):
    """将任务标记为失败并记录错误信息。"""
    _mark_status(task_id, "failed", error=error)


# ── 音频时长读取 ───────────────────────────────────────────────────────────
def _get_audio_duration(audio_path: str) -> int | None:
    """
    通过 mutagen 读取音频文件总时长（毫秒）。

    mutagen.File() 自动检测格式（WAV / MP3 均可）。
    解析失败或文件为空时返回 None，调用方自行降级处理。

    注意：macOS 上 Azure SDK 不支持 MP3 编码，配置 MP3 输出实际得到 WAV 文件。
    """
    total_ms = None
    file_size = os.path.getsize(audio_path)
    if file_size > 0:
        try:
            audio = mutagen.File(audio_path)
            if audio is not None and hasattr(audio.info, "length"):
                total_ms = int(audio.info.length * 1000)
        except Exception:
            log.warning(f"mutagen 解析音频时长失败: {traceback.format_exc()}")
    else:
        log.warning(f"音频文件为空（{file_size} bytes），Azure SDK 未写入数据")
    return total_ms


# ── 单次合成 ───────────────────────────────────────────────────────────────
def _synth_one(text: str, voice: str, rate: str, output_path: str) -> tuple:
    """
    对单段文本执行一次 SDK 合成，返回 (word_timings, total_ms)。

    参数：
      text        — 待合成的文本（单段，已切分后）
      voice       — Azure 语音名称
      rate        — 语速
      output_path — 输出音频文件的完整路径

    返回：
      (word_timings: list[dict], total_ms: int)

    word_timings 格式：[{"text":"...", "start_ms": int, "end_ms": int}, ...]

    异常：
      RuntimeError — 合成失败、超时、或结果异常
    """
    # ── 1. 配置 Azure Speech SDK ───────────────────────────────────────
    speech_config = speechsdk.SpeechConfig(
        subscription=SPEECH_KEY, region=SPEECH_REGION
    )
    speech_config.speech_synthesis_voice_name = voice
    speech_config.speech_synthesis_output_format = (
        speechsdk.SpeechSynthesisOutputFormat.Audio48Khz192KBitRateMonoMp3
    )
    speech_config.set_property(
        speechsdk.PropertyId.SpeechSynthesisRequest_Rate, rate
    )

    audio_config = speechsdk.audio.AudioOutputConfig(filename=output_path)
    synthesizer = speechsdk.SpeechSynthesizer(
        speech_config=speech_config, audio_config=audio_config
    )

    # ── 2. 注册词边界回调 ──────────────────────────────────────────────
    timings = []

    def _on_word_boundary(evt: speechsdk.SpeechSynthesisWordBoundaryEventArgs):
        offset_ms = int(evt.audio_offset) // 10000
        try:
            duration_ms = int(evt.duration.total_seconds() * 1000)
        except Exception:
            duration_ms = 0
            log.warning(f"word_boundary duration 获取失败: {traceback.format_exc()}")

        timings.append({
            "text": evt.text,
            "offset_ms": offset_ms,
            "duration_ms": duration_ms,
        })
        log.debug(f"word_boundary: text=\"{evt.text}\" offset_ms={offset_ms} duration_ms={duration_ms}")

    synthesizer.synthesis_word_boundary.connect(_on_word_boundary)

    # ── 3. 执行合成（带超时保护）───────────────────────────────────────
    # Azure SDK 的 ResultFuture.get() 不支持 timeout 参数，用线程 join 超时实现
    synth_result = None
    synth_error = None

    def _run_synth():
        nonlocal synth_result, synth_error
        try:
            synth_result = synthesizer.speak_text_async(text).get()
        except Exception as e:
            synth_error = e

    synth_thread = threading.Thread(target=_run_synth, daemon=True)
    synth_thread.start()
    synth_thread.join(timeout=TTS_SDK_TIMEOUT)

    if synth_thread.is_alive():
        raise RuntimeError(
            f"SDK synthesis timed out after {TTS_SDK_TIMEOUT}s "
            f"(text length: {len(text)} chars)"
        )
    if synth_error:
        raise synth_error

    result = synth_result
    log.debug(f"合成完成, reason={result.reason}, timings 数量={len(timings)}")
    log.info(f"合成完成: {len(text)} chars, {len(timings)} words")

    # ── 4. 检查合成结果 ────────────────────────────────────────────────
    if result.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
        cancellation = result.cancellation_details
        raise RuntimeError(
            f"TTS failed: {cancellation.reason} — {cancellation.error_details}"
        )

    # ── 5. 读取音频时长 ────────────────────────────────────────────────
    total_ms = _get_audio_duration(output_path)

    # mutagen 解析失败 或 timings 为空时，用词边界时间戳推算
    if total_ms is None and timings:
        last = timings[-1]
        fallback_dur = last.get("duration_ms") or (
            (timings[-1]["offset_ms"] - timings[-2]["offset_ms"])
            if len(timings) >= 2 else 500
        )
        total_ms = last["offset_ms"] + fallback_dur

    # ── 6. 计算词级时间戳 ──────────────────────────────────────────────
    word_timings = []
    for i, t in enumerate(timings):
        start = int(t["offset_ms"])
        if i + 1 < len(timings):
            end = int(timings[i + 1]["offset_ms"])
        else:
            end = total_ms or start
        word_timings.append({
            "text": t["text"],
            "start_ms": start,
            "end_ms": end,
        })

    return word_timings, total_ms


# ── 分块合成 ───────────────────────────────────────────────────────────────
def _synthesize_chunked(task_id: str, text: str, voice: str, rate: str,
                        final_path: str) -> tuple:
    """
    将长文本分块合成，拼接音频，合并词边界时间戳。

    流程：
      1. split_text → chunks
      2. 逐 chunk 调用 _synth_one → 收集 timings + durations
      3. concat_audio_files → 拼接为 final_path
      4. merge_word_timings → 全局时间戳
      5. _get_audio_duration → 拼接后总时长
      6. 清理临时 chunk 文件

    返回：
      (merged_word_timings: list[dict], total_ms: int)
    """
    chunks = split_text(text, TTS_MAX_CHUNK_CHARS)
    log.info(f"长文本分块: {len(text)} chars → {len(chunks)} chunks (task={task_id})")

    chunk_paths: list = []
    chunk_timings: list[list[dict]] = []
    chunk_durations: list[int] = []

    try:
        for i, chunk_text in enumerate(chunks):
            chunk_path = str(AUDIO_DIR / f"{task_id}_chunk_{i}.mp3")
            timings, duration = _synth_one(chunk_text, voice, rate, chunk_path)
            chunk_paths.append(chunk_path)
            chunk_timings.append(timings)
            chunk_durations.append(duration)
            log.info(f"Chunk {i + 1}/{len(chunks)} done: "
                  f"{len(chunk_text)} chars, {duration}ms")

        # 拼接音频（chunk_paths 是 Path 对象列表）
        chunk_path_objs = [Path(p) for p in chunk_paths]
        final_path_obj = Path(final_path)
        concat_audio_files(chunk_path_objs, final_path_obj)

        # 合并词边界时间戳
        merged_timings = merge_word_timings(chunk_timings, chunk_durations)

        # 拼接后总时长（mutagen 更准确）
        total_ms = _get_audio_duration(final_path)
        if total_ms is None:
            total_ms = sum(chunk_durations)

        return merged_timings, total_ms

    finally:
        # 清理临时 chunk 文件
        for p in chunk_paths:
            try:
                Path(p).unlink(missing_ok=True)
            except OSError:
                pass


# ── TTS 合成核心 ───────────────────────────────────────────────────────────
def _synthesize(task_id: str):
    """
    执行完整的 TTS 合成流程。

    短文本（≤ TTS_MAX_CHUNK_CHARS）：单次 SDK 合成，与原有逻辑一致。
    长文本（> TTS_MAX_CHUNK_CHARS）：分块合成 → 拼接音频 → 合并时间戳。

    步骤：
      1. 从 DB 读取任务（text, voice, rate）
      2. 更新状态 → processing
      3. 按文本长度选择单次合成或分块合成
      4. 更新状态 → completed，写入音频文件名和词时间戳
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

    voice = task["voice"]
    rate = task["rate"]
    text = task["text"]

    audio_file = f"{task_id}.mp3"
    audio_path = str(AUDIO_DIR / audio_file)

    # ── 3. 选择合成路径 ────────────────────────────────────────────────
    if len(text) <= TTS_MAX_CHUNK_CHARS:
        # 短文本：单次合成（与原有逻辑一致，仅增加了 timeout）
        word_timings, total_ms = _synth_one(text, voice, rate, audio_path)
    else:
        # 长文本：分块合成 + 拼接
        word_timings, total_ms = _synthesize_chunked(
            task_id, text, voice, rate, audio_path
        )

    # ── 4. 回写完成状态 ────────────────────────────────────────────────
    wt_json = json.dumps(word_timings, ensure_ascii=False)
    log.debug(f"写入 DB, word_timings={wt_json}, total_ms={total_ms}")
    log.info(f"任务完成: task={task_id}, total_ms={total_ms}, words={len(word_timings)}")
    _mark_status(
        task_id,
        "completed",
        audio_file=audio_file,
        word_timings=wt_json,
        total_ms=total_ms,
    )
