"""
Azure Batch Synthesis REST API 客户端：提交、轮询、下载、删除。

Batch Synthesis API 是完全异步的 REST API，不需要 Azure Speech SDK。
工作流程：PUT 创建任务 → GET 轮询状态 → Succeeded 时从 SAS URL 下载 ZIP 结果。

与实时 SDK 路径的差异：
  - 词边界数据来自 result ZIP 中的 .word.json 文件（单位 ms）
  - SDK 回调中 audio_offset 单位是 100ns，需要 // 10000 转换
  - Batch API 中 AudioOffset 直接就是 ms，无需转换

网络配置：
  - 支持 HTTP/HTTPS 代理：设置 HTTP_PROXY / HTTPS_PROXY 环境变量
  - 自动重试：最多 3 次，间隔 1s/2s/4s，仅对可重试错误
  - Azure 中国区用户可能需要代理才能直连 eastus 等海外 region
"""
import io
import json
import os
import traceback
import time
import uuid
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

import mutagen
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import SPEECH_KEY, SPEECH_REGION, BATCH_API_VERSION, BATCH_ENDPOINT
from logger import get_logger

log = get_logger(__name__)

# ── 端点构造 ──────────────────────────────────────────────────────────────────
# 优先使用自定义端点，否则使用区域端点
# 两种格式：
#   1. https://{region}.api.cognitive.microsoft.com         （区域格式）
#   2. https://{resource}.cognitiveservices.azure.com       （资源格式，权限更完整）
if BATCH_ENDPOINT:
    _BASE_URL = BATCH_ENDPOINT.rstrip("/")
else:
    _BASE_URL = f"https://{SPEECH_REGION}.api.cognitive.microsoft.com"

# ── Session + 重试 ────────────────────────────────────────────────────────────
def _build_session() -> requests.Session:
    """
    创建带重试逻辑和代理支持的 requests Session。

    重试策略：
      - 最多 3 次，退避间隔 1s / 2s / 4s
      - 仅重试可恢复错误：连接超时、SSL 错误、5xx
      - 4xx 不重试（认证失败、参数错误等不应重试）

    代理：
      - 自动读取 HTTP_PROXY / HTTPS_PROXY 环境变量
    """
    session = requests.Session()

    # 代理：读取标准环境变量
    proxies = {}
    for var, scheme in [("HTTPS_PROXY", "https"), ("HTTP_PROXY", "http")]:
        val = os.environ.get(var)
        if val:
            proxies[scheme] = val
    if proxies:
        session.proxies.update(proxies)

    # 重试适配器
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,         # 1s / 2s / 4s
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["PUT", "GET", "DELETE"],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    return session

# 模块级 session，复用 TCP 连接
_session = _build_session()

# 所有请求共用的 headers
def _headers():
    return {
        "Ocp-Apim-Subscription-Key": SPEECH_KEY,
        "Content-Type": "application/json",
    }


def _request(method: str, url: str, timeout: int = 30, **kwargs) -> requests.Response:
    """
    统一请求入口，附加 headers 并对各类错误提供诊断信息。

    常见错误诊断：
      - 401 PermissionDenied → key 无权限（F0 层不支持 batch API / endpoint 格式不对）
        → 解决：升级到 S0 层 或 设置 TTS_BATCH_ENDPOINT 为资源端点格式
      - SSLEOFError → 网络中间设备阻断了 Azure API
        → 解决：设置 HTTPS_PROXY 代理
      - ConnectTimeout → 网络不通 → 检查代理 / DNS / VPN
    """
    try:
        resp = _session.request(method, url, headers=_headers(), timeout=timeout, **kwargs)
        resp.raise_for_status()
        return resp
    except requests.exceptions.HTTPError as e:
        # 401 时打印完整响应体帮助诊断
        if e.response is not None and e.response.status_code == 401:
            raise RuntimeError(
                f"Azure Batch API 认证失败 (401)：\n"
                f"  URL: {url}\n"
                f"  响应: {e.response.text}\n"
                f"  可能原因：\n"
                f"  1. F0（免费层）不支持 Batch Synthesis API，需升级到 S0\n"
                f"  2. 端点格式不匹配 — 试试设置 TTS_BATCH_ENDPOINT 环境变量\n"
                f"     例如: export TTS_BATCH_ENDPOINT=https://<你的资源名>.cognitiveservices.azure.com\n"
                f"  3. 在 Azure Portal → Speech 资源 → 密钥和端点 → 查看端点 URL"
            ) from e
        raise RuntimeError(
            f"Azure Batch API 请求失败：{e}\n  响应: {getattr(e.response, 'text', 'N/A')}"
        ) from e
    except requests.exceptions.SSLError as e:
        raise RuntimeError(
            f"SSL 连接 Azure 失败 ({url})：\n"
            f"  {e}\n"
            f"  可能原因：HTTPS 代理未配置 或 网络防火墙阻断了 Azure API。\n"
            f"  解决方法：设置环境变量 HTTPS_PROXY（如 http://127.0.0.1:7890）\n"
            f"  或者在 config.py 中将 SPEECH_REGION 改为离你更近的区域。"
        ) from e
    except requests.exceptions.ConnectionError as e:
        raise RuntimeError(
            f"无法连接 Azure API ({url})：\n"
            f"  {e}\n"
            f"  可能原因：网络不通、DNS 解析失败、或需要代理。\n"
            f"  解决方法：检查网络连接，或设置 HTTP_PROXY / HTTPS_PROXY 环境变量。"
        ) from e


# ── 创建 batch 合成任务 ──────────────────────────────────────────────────────
def submit_batch_job(text: str, voice: str, rate: str) -> dict:
    """
    提交 PlainText 格式的 batch 合成任务。

    参数：
      text  — 要合成的文本
      voice — Azure 语音名称（如 zh-CN-XiaochenNeural）
      rate  — 语速（如 "+20%" "-10%" "1.0"）

    返回：
      {"synthesis_id": str, "azure_status": str}

    synthesis_id 格式：batch_<12位hex>，用于后续轮询。
    azure_status 初始值："NotStarted"（Azure 创建任务后立即返回）。
    """
    synthesis_id = f"batch_{uuid.uuid4().hex[:12]}"
    url = f"{_BASE_URL}/texttospeech/batchsyntheses/{synthesis_id}?api-version={BATCH_API_VERSION}"

    # 构建 SSML（用 <prosody rate> 可靠控制语速）
    # Batch API 的 synthesisConfig.rate 不被所有 API 版本支持，
    # 且 PlainText 模式下语速控制不可靠，因此统一使用 SSML 输入。
    ssml = (
        f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis"'
        f' xml:lang="zh-CN">'
        f'<voice name="{voice}">'
        f'<prosody rate="{rate}">{xml_escape(text)}</prosody>'
        f'</voice></speak>'
    )

    body = {
        "inputKind": "SSML",
        "inputs": [
            {"content": ssml}
        ],
        "properties": {
            "outputFormat": "audio-24khz-96kbitrate-mono-mp3",
            "wordBoundaryEnabled": True,
            "sentenceBoundaryEnabled": False,
            "concatenateResult": True,
            "decompressOutputFiles": False,
            "destinationContainerUrl": None,
            "timeToLiveInHours": 168,  # 7 天
        },
    }

    resp = requests.put(url, headers=_headers(), json=body, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    return {
        "synthesis_id": synthesis_id,
        "azure_status": data.get("status", "NotStarted"),
    }


# ── 查询 batch 任务状态 ──────────────────────────────────────────────────────
def get_batch_status(synthesis_id: str) -> dict:
    """
    查询 batch 任务状态，返回 Azure 原始 JSON 响应。

    关键字段：
      - status: "NotStarted" | "Running" | "Succeeded" | "Failed"
      - outputs.result: SAS URL（仅 Succeeded 时存在）
      - error.message: 错误描述（仅 Failed 时存在）
      - properties.durationInMilliseconds, billingDetails 等
    """
    url = f"{_BASE_URL}/texttospeech/batchsyntheses/{synthesis_id}?api-version={BATCH_API_VERSION}"
    resp = requests.get(url, headers=_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()


# ── 下载并解压 batch 结果 ────────────────────────────────────────────────────
def download_batch_results(result_url: str, task_id: str, audio_dir: Path) -> dict:
    """
    从 Azure 返回的 SAS URL 下载结果 ZIP，提取音频和词边界数据。

    参数：
      result_url — Azure outputs.result 中的 SAS URL
      task_id    — 本地任务 ID（用于命名输出文件）
      audio_dir  — 音频输出目录（Path 对象）

    返回：
      {
        "audio_file": str,        — 输出的音频文件名（如 "tts_xxx.mp3"）
        "word_timings": list[dict], — [{"text":"","start_ms":int,"end_ms":int}, ...]
        "total_ms": int,           — 音频总时长
      }

    ZIP 内容示例（concatenateResult=True 时只有一个文件）：
      summary.json          — 任务摘要
      0001.wav              — 合并后的音频（Azure 输出 WAV，即使配置 MP3）
      0001.word.json        — 词边界：[{"Text":"...","AudioOffset":ms,"Duration":ms}]
    """
    # 1. 下载 ZIP
    resp = requests.get(result_url, timeout=120)
    resp.raise_for_status()

    # 2. 在内存中解压
    word_timings = []
    audio_file = f"{task_id}.mp3"
    audio_path = audio_dir / audio_file

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = zf.namelist()

        # 找到词边界文件（.word.json）
        word_files = [n for n in names if n.endswith(".word.json")]
        if word_files:
            word_data = json.loads(zf.read(word_files[0]).decode("utf-8"))
            word_timings = _map_word_boundaries(word_data)

        # 找到音频文件（.wav / .mp3 / .ogg）
        audio_names = [n for n in names if n.endswith((".wav", ".mp3", ".ogg"))]
        if audio_names:
            # 提取并重命名
            audio_bytes = zf.read(audio_names[0])
            audio_path.write_bytes(audio_bytes)
        else:
            # 降级：可能只有 summary.json，尝试提取任意非 JSON 文件
            non_json = [n for n in names if not n.endswith(".json")]
            if non_json:
                audio_bytes = zf.read(non_json[0])
                audio_path.write_bytes(audio_bytes)

    # 3. 通过 mutagen 获取总时长（与 SDK 路径一致）
    total_ms = None
    if audio_path.exists() and audio_path.stat().st_size > 0:
        try:
            audio = mutagen.File(str(audio_path))
            if audio is not None and hasattr(audio.info, "length"):
                total_ms = int(audio.info.length * 1000)
        except Exception:
            log.warning(f"mutagen 解析 batch 音频时长失败: {traceback.format_exc()}")

    # mutagen 失败时用词边界时间戳估算
    if total_ms is None and word_timings:
        last = word_timings[-1]
        total_ms = last["end_ms"]
    elif total_ms is None:
        total_ms = 0

    return {
        "audio_file": audio_file,
        "word_timings": word_timings,
        "total_ms": total_ms,
    }


# ── 删除 Azure 端 batch 任务 ──────────────────────────────────────────────────
def delete_batch_job(synthesis_id: str) -> None:
    """
    删除 Azure 端的 batch 合成任务及其输出文件。
    Best-effort：捕获所有异常，不抛出（调用方不依赖此操作成功）。
    """
    url = f"{_BASE_URL}/texttospeech/batchsyntheses/{synthesis_id}?api-version={BATCH_API_VERSION}"
    try:
        resp = requests.delete(url, headers=_headers(), timeout=30)
        resp.raise_for_status()
    except Exception:
        log.warning(f"删除 Azure batch 任务失败 ({synthesis_id}): {traceback.format_exc()}")


# ── 词边界格式映射 ──────────────────────────────────────────────────────────
def _map_word_boundaries(raw: list[dict]) -> list[dict]:
    """
    将 Azure Batch API 的词边界格式映射为内部统一格式。

    Batch API 格式：
      [{"Text": "你好", "AudioOffset": 50, "Duration": 137}]
      - AudioOffset：毫秒（整数）
      - Duration：毫秒（整数）

    内部统一格式：
      [{"text": "你好", "start_ms": 50, "end_ms": 187}]
      - end_ms = AudioOffset + Duration

    注意：与 SDK 路径不同，Batch API 的 AudioOffset 本身就是毫秒，
    不需要 // 10000 的 100ns→ms 转换。
    """
    result = []
    for i, w in enumerate(raw):
        start = int(w.get("AudioOffset", 0))
        duration = int(w.get("Duration", 0))
        entry = {
            "text": w.get("Text", ""),
            "start_ms": start,
            "end_ms": start + duration,
        }
        result.append(entry)
    return result
