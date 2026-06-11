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

import azure.cognitiveservices.speech as speechsdk
import mutagen

from config import SPEECH_KEY, SPEECH_REGION, AUDIO_DIR
from database import get_db

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


# ── TTS 合成核心 ───────────────────────────────────────────────────────────
def _synthesize(task_id: str):
    """
    执行完整的 TTS 合成流程：

    1. 从 DB 读取任务（text, voice, rate）
    2. 更新状态 → processing
    3. 构建 SSML（含 prosody 语速 + word_boundary 事件）
    4. 调用 Azure Speech SDK 异步合成 MP3
    5. 通过 word_boundary 回调逐词记录 offset_ms
    6. 用 mutagen 读取 MP3 文件总时长
    7. 计算每个词的 end_ms（下一词的 start_ms，末词用 total_ms）
    8. 更新状态 → completed，写入音频文件名和词时间戳
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

    # ── 4. 配置 Azure Speech SDK ───────────────────────────────────────
    # 注意：不用 SSML，因为 Azure SDK 已知 Bug —— synthesis_word_boundary
    # 事件在 speak_ssml_async 下不触发，只有 speak_text_async 才触发。
    # voice / rate 通过 SpeechConfig 设置，不走 SSML。
    speech_config = speechsdk.SpeechConfig(
        subscription=SPEECH_KEY, region=SPEECH_REGION
    )
    speech_config.speech_synthesis_voice_name = voice
    # 输出格式：48kHz / 192kbps / 单声道 / MP3
    speech_config.speech_synthesis_output_format = (
        speechsdk.SpeechSynthesisOutputFormat.Audio48Khz192KBitRateMonoMp3
    )
    # 语速（与 SSML <prosody rate="..."> 等效）
    speech_config.set_property(
        speechsdk.PropertyId.SpeechSynthesisRequest_Rate, rate
    )

    audio_file = f"{task_id}.mp3"
    audio_path = str(AUDIO_DIR / audio_file)
    audio_config = speechsdk.audio.AudioOutputConfig(filename=audio_path)

    synthesizer = speechsdk.SpeechSynthesizer(
        speech_config=speech_config, audio_config=audio_config
    )

    # ── 5. 注册词边界回调 ──────────────────────────────────────────────
    # Azure SDK 在合成每个词时触发该事件，我们在闭包中实时收集词文本和起始偏移量。
    # evt.audio_offset 是 int，单位 100ns tick → // 10000 得到毫秒
    # evt.duration 是 datetime.timedelta → .total_seconds() * 1000 得到毫秒
    timings = []

    def _on_word_boundary(evt: speechsdk.SpeechSynthesisWordBoundaryEventArgs):
        # audio_offset 是 int（100ns 单位），直接除以 10000 得毫秒
        offset_ms = int(evt.audio_offset) // 10000
        # duration 是 timedelta，取毫秒
        try:
            duration_ms = int(evt.duration.total_seconds() * 1000)
        except Exception:
            duration_ms = 0
            print(f"[WARN] word_boundary duration 获取失败: {traceback.format_exc()}")

        entry = {
            "text": evt.text,
            "offset_ms": offset_ms,
            "duration_ms": duration_ms,
        }
        timings.append(entry)
        print(f"[DEBUG] word_boundary: text=\"{evt.text}\" offset_ms={offset_ms} duration_ms={duration_ms}")

    synthesizer.synthesis_word_boundary.connect(_on_word_boundary)
    print(f"[DEBUG] word_boundary 回调已注册")

    # ── 6. 执行合成（阻塞等待完成）─────────────────────────────────────
    # 用 speak_text_async 而非 speak_ssml_async——SSML 下 word_boundary 事件不触发（Azure SDK 已知 Bug）
    result = synthesizer.speak_text_async(text).get()
    print(f"[DEBUG] 合成完成, reason={result.reason}, timings 数量={len(timings)}")

    # 检查合成结果：失败时抛异常 → 外层捕获 → 标记 failed
    if result.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
        cancellation = result.cancellation_details
        raise RuntimeError(
            f"TTS failed: {cancellation.reason} — {cancellation.error_details}"
        )

    # ── 7. 读取音频总时长 ──────────────────────────────────────────────
    # 使用 mutagen.File() 自动检测格式（WAV / MP3 均可），获取文件总时长
    # 注意：macOS 上 Azure SDK 不支持 MP3 编码，配置 MP3 输出实际得到 WAV 文件
    total_ms = None
    file_size = os.path.getsize(audio_path)
    if file_size > 0:
        try:
            audio = mutagen.File(audio_path)
            if audio is not None and hasattr(audio.info, "length"):
                total_ms = int(audio.info.length * 1000)
        except Exception:
            print(f"[WARN] mutagen 解析音频时长失败: {traceback.format_exc()}")
    else:
        print(f"[WARN] 音频文件为空（{file_size} bytes），Azure SDK 未写入数据")

    # mutagen 解析失败 或 timings 为空时，用词边界时间戳推算
    if total_ms is None and timings:
        last = timings[-1]
        # 优先用回调中的 duration，否则取前一个词的持续时长作为估算
        fallback_dur = last.get("duration_ms") or (
            (timings[-1]["offset_ms"] - timings[-2]["offset_ms"])
            if len(timings) >= 2 else 500
        )
        total_ms = last["offset_ms"] + fallback_dur

    # ── 8. 计算词级时间戳 ──────────────────────────────────────────────
    # 每个词的 end_ms = 下一个词的 start_ms
    # 最后一个词：有 total_ms 用 total_ms，没有则用自身 start_ms（降级处理）
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

    # ── 9. 回写完成状态 ────────────────────────────────────────────────
    # word_timings 以 JSON 字符串存入（ensure_ascii=False 保留中文原文）
    wt_json = json.dumps(word_timings, ensure_ascii=False)
    print(f"[DEBUG] 写入 DB, word_timings={wt_json}, total_ms={total_ms}")
    _mark_status(
        task_id,
        "completed",
        audio_file=audio_file,
        word_timings=wt_json,
        total_ms=total_ms,
    )
