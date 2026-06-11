"""
Configuration: 路径、环境变量、默认值。

加载顺序：
  1. 系统环境变量（优先级最高）
  2. 项目根目录 .env 文件（兜底）

模块被 import 时自动执行，若最终 SPEECH_KEY 仍为空则抛出 RuntimeError。
"""
import os
from pathlib import Path

# ── 文件路径 ───────────────────────────────────────────────────────────────
# 可通过环境变量 TTS_AUDIO_DIR / TTS_DB_PATH 覆盖（本地开发时使用项目目录）
# 合成音频输出目录（不存在自动创建）
AUDIO_DIR = Path(os.environ.get("TTS_AUDIO_DIR", "/opt/azure-tts-service/audio"))
AUDIO_DIR.mkdir(exist_ok=True)

# SQLite 任务数据库
DB_PATH = Path(os.environ.get("TTS_DB_PATH", "/opt/azure-tts-service/tasks.db"))

# ── 运行时配置 ─────────────────────────────────────────────────────────────
# 后台合成线程数，可通过 TTS_MAX_WORKERS 环境变量覆盖
MAX_QUEUE_WORKERS = int(os.environ.get("TTS_MAX_WORKERS", "4"))

# Azure 语音服务凭证
SPEECH_KEY = os.environ.get("AZURE_SPEECH_KEY")       # 必填，在 Azure Portal 获取
SPEECH_REGION = os.environ.get("AZURE_SPEECH_REGION", "eastus")

# TTS 默认参数：当请求未指定 voice / rate 时使用
DEFAULT_VOICE = os.environ.get("TTS_DEFAULT_VOICE", "zh-CN-XiaochenNeural")
DEFAULT_RATE = os.environ.get("TTS_DEFAULT_RATE", "+20%")


def _load_dotenv():
    """
    从项目根目录 .env 文件加载 SPEECH_KEY / SPEECH_REGION。

    仅在系统环境变量已设置时跳过（环境变量优先级 > .env）。
    解析逻辑：跳过空行和 # 注释行，按 key=value 格式提取。
    """
    global SPEECH_KEY, SPEECH_REGION

    # 环境变量已设置则无需加载 .env
    if SPEECH_KEY:
        return

    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return

    for line in env_path.read_text().splitlines():
        line = line.strip()
        # 跳过空行和注释
        if line.startswith("#") or "=" not in line:
            continue
        # 仅按第一个 = 分割，value 中可能包含 =
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if k == "AZURE_SPEECH_KEY":
            SPEECH_KEY = v
        elif k == "AZURE_SPEECH_REGION":
            SPEECH_REGION = v


# 模块加载时自动执行 .env 回退
_load_dotenv()

# 最终校验：缺少 SPEECH_KEY 服务无法运行
if not SPEECH_KEY:
    raise RuntimeError(
        "AZURE_SPEECH_KEY not set — set the env var or place a .env file"
    )
