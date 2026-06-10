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
import queue
import threading

import azure.cognitiveservices.speech as speechsdk
import mutagen.mp3

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

    # ── 3. 构建 SSML ───────────────────────────────────────────────────
    # SSML（Speech Synthesis Markup Language）是 Azure TTS 的 XML 控制语言
    #   <voice>     — 指定发音人
    #   <prosody>   — 控制语速（rate）、音调、音量
    #   <mstts:viseme type='word_boundary'/> — 启用词边界事件（回调中获取时间戳）
    voice = task["voice"]
    rate = task["rate"]
    text = task["text"]
    ssml = (
        f"<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' "
        f"xmlns:mstts='http://www.w3.org/2001/mstts' xml:lang='zh-CN'>"
        f"<voice name='{voice}'>"
        f"<mstts:viseme type='word_boundary'/>"
        f"<prosody rate='{rate}'>"
        f"{text}"
        f"</prosody>"
        f"</voice>"
        f"</speak>"
    )

    # ── 4. 配置 Azure Speech SDK ───────────────────────────────────────
    speech_config = speechsdk.SpeechConfig(
        subscription=SPEECH_KEY, region=SPEECH_REGION
    )
    # 输出格式：48kHz / 192kbps / 单声道 / MP3
    speech_config.speech_synthesis_output_format = (
        speechsdk.SpeechSynthesisOutputFormat.Audio48Khz192KBitRateMonoMp3
    )

    audio_file = f"{task_id}.mp3"
    audio_path = str(AUDIO_DIR / audio_file)
    audio_config = speechsdk.audio.AudioOutputConfig(filename=audio_path)

    synthesizer = speechsdk.SpeechSynthesizer(
        speech_config=speech_config, audio_config=audio_config
    )

    # ── 5. 注册词边界回调 ──────────────────────────────────────────────
    # Azure SDK 在合成每个词时触发该事件，我们在闭包中实时收集词文本和起始偏移量。
    # evt.audio_offset.ticks 单位是 100ns tick，除以 10000 得到毫秒。
    timings = []

    def _on_word_boundary(evt: speechsdk.SpeechSynthesisWordBoundaryEventArgs):
        timings.append({
            "text": evt.text,
            "offset_ms": evt.audio_offset.ticks // 10000,
        })

    synthesizer.synthesis_word_boundary.connect(_on_word_boundary)

    # ── 6. 执行合成（阻塞等待完成）─────────────────────────────────────
    result = synthesizer.speak_ssml_async(ssml).get()

    # 检查合成结果：失败时抛异常 → 外层捕获 → 标记 failed
    if result.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
        cancellation = result.cancellation_details
        raise RuntimeError(
            f"TTS failed: {cancellation.reason} — {cancellation.error_details}"
        )

    # ── 7. 读取音频总时长 ──────────────────────────────────────────────
    # 使用 mutagen 解析 MP3 文件头获取时长，用于计算最后一个词的 end_ms
    try:
        audio = mutagen.mp3.MP3(audio_path)
        total_ms = int(audio.info.length * 1000)
    except Exception:
        total_ms = None

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
    _mark_status(
        task_id,
        "completed",
        audio_file=audio_file,
        word_timings=json.dumps(word_timings, ensure_ascii=False),
        total_ms=total_ms,
    )
