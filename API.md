# Story Voice API

生产地址：`https://tools.exnihilo.site/azure_api`。相同接口也挂载在 `/api/v1`。
两个域名 `tools.exnihilo.site` 和 `api.exnihilo.site` 均要求已有的客户端 mTLS 证书（例如 ba-user），并叠加 Bearer 鉴权。
浏览器使用已安装的证书；Baby Story Creator 服务端需配置客户端证书和私钥。

所有业务接口必须发送 `Authorization: Bearer <client-secret>`，包括音频、字幕下载。
只有 `/health` 无需鉴权。密钥只能放 Baby Story Creator **服务端环境变量**，不能打包进客户端。
每个客户端只可读写自己的任务。未配置密钥时拒绝业务访问。

## Baby Story Creator 调用流程

1. POST `/tts` 提交任务，返回 HTTP 202、`task_id`、`status_url`。
2. GET `/tts/{task_id}` 轮询（建议 2 秒），直到 `completed` 或 `failed`。
3. 使用相同鉴权头下载 `audio_url`、`timing_url`、`subtitles.srt`、`subtitles.vtt`。
   返回链接以 `/azure_api` 开头，和所请求的域名拼接。
4. 如需浏览器播放，由应用服务端代理音频，或鉴权 fetch 后转为 Blob URL。

```json
{
  "text": "小兔子打开窗户。月亮正对着它微笑。",
  "provider": "minimax",
  "voice": "babystory-moon-postman-narrator",
  "speed": 1.0,
  "minimax_pitch": 0,
  "model": "speech-2.8-hd",
  "sentences": ["小兔子打开窗户。", "月亮正对着它微笑。"]
}
```

- `provider`: `azure`（默认）或 `minimax`。请从音色目录选择对应 `voice`。
- `sentences`: 可选，传入应用自己的断句结果以精确对应故事句子；忽略空白后必须与 text 一致。
  省略时按中文/英文句末标点及换行切分；复杂缩写等建议显式传入。
- 上限：20,000 字符、200 句、每句 3,000 字符。
- Azure: `rate` 如 `+20%`、`-10%`；`pitch` 如 `+0Hz`、`-10Hz`。
- MiniMax: `speed` 0.5–2；`minimax_pitch` -12–12；model 默认由服务配置。
- 逐句合成再拼接，保留句间停顿；相比整篇合成，语气连贯性可能不同。

完成结果包含原文、引擎、音色、状态、创建和更新时间，以及：

```json
{
  "duration_ms": 3500,
  "total_ms": 3500,
  "sentence_timings": [
    {"index": 0, "text": "小兔子打开窗户。", "start_ms": 0, "end_ms": 1700, "duration_ms": 1700},
    {"index": 1, "text": "月亮正对着它微笑。", "start_ms": 1700, "end_ms": 3500, "duration_ms": 1800}
  ],
  "word_timings": [],
  "metadata": {
    "timing_source": "measured_sentence_audio",
    "sample_rate": 32000,
    "channels": 1,
    "format": "mp3",
    "size_bytes": 56000,
    "word_timings_available": false,
    "provider_segments": []
  }
}
```

时间均为毫秒，相对于完整音频。句子时长根据解码后的 PCM 样本数测量，包含该句首尾静音，
不是字数估算，也不是精确的发音起止检测。统一编码为真实 MP3，避免分段 MP3 编码填充累积漂移。
Azure 尽可能保留 SDK 词边界；部分音色可能不提供。MiniMax 本实现提供实测句级时间，
`word_timings=[]`，不伪造词级时间；保留每句的 `trace_id` 和 `extra_info`（用量、音频参数等）。

## 音色与多维过滤

GET `/voices?provider=azure` 或 `/voices?provider=minimax`。
返回 `voices`、`total`、`catalog_total`、`facets`（每个可筛选维度的值）。缓存一小时。

- Azure：当前已配置订阅区域的全部可用音色，含主语言、附加语言、性别、风格、角色、类型、状态及 VoiceTag。
  区域限制的预览音色以官方返回结果为准。
- MiniMax：官方 `system`、账号设计 `designed`、克隆 `cloned` 三种来源。
- `official` 保留原始字段。MiniMax 当前返回的很多标签是自然语言描述：
  `tag` 保留描述；语言、性别、年龄可从明确描述提取，标记 `classification=official_description_derived`。
  未提供或无法可靠提取的信息留空。没有完整结构化标签时不保证每个音色均有每个维度。
- 不同维度 AND，同维度重复参数 OR。`q` 搜索名称、ID 和全部官方信息。

例：`/voices?provider=azure&language=en-US&gender=Female&style=chat`。
`/voices?provider=minimax&source=designed`。

## 其他接口

- GET `/tts?limit=50`：当前客户端最近任务，limit 为 1–200。
- DELETE `/tts/{task_id}`：删除已完成/失败任务及音频，处理中返回 409。
- GET `/tts/{task_id}/timing`：`sentence_timings` 和 `word_timings`。
- GET `/tts/{task_id}/subtitles.srt` / `.vtt`：字幕。
- 错误：401 未授权、404 不存在或其他客户端资源、409 未完成/不可删除、422 参数错误、
  429 提交限流/队列已满、502 上游目录失败、503 未配置凭证。

## 运维

`.env`（不纳入 Git，权限 0600）：

```dotenv
AZURE_SPEECH_KEY=<azure-key>
AZURE_SPEECH_REGION=eastus
MINIMAX_API_KEY=<minimax-key>
MINIMAX_BASE_URL=https://api.minimaxi.com
MINIMAX_MODEL=speech-2.8-hd
TTS_API_KEYS={"baby-story-creator":"<至少32位随机密钥>","webui":"<另一个随机密钥>"}
TTS_CORS_ORIGINS=https://tools.exnihilo.site
TTS_RATE_LIMIT=20
TTS_MAX_PENDING=100
```

国际站账号使用 `https://api.minimax.io`。密钥按站点区分。
必须安装 ffmpeg（可用 `TTS_FFMPEG` 指定路径）。使用一个 uvicorn 进程，内部 worker 线程并发，
限流是每客户端每分钟提交次数。中途重启的 processing 任务标记失败，防止自动重复计费；pending 任务恢复入队。
旧的无 owner 历史任务默认不开放，通过管理员离线确认后才能分配 owner。

部署：`bash deploy.sh`，会备份代码、SQLite、服务配置、网页及 nginx 配置，然后同步、验证、重启。
密钥保存在 72 的 `/opt/azure-tts-service/.env`，Baby Story Creator 部署者通过 SSH 安全读取对应客户端密钥。
工作台使用 webui 客户端密钥。更换 TTS_API_KEYS 对应值并重启即可轮换。

旧版本文档中的 batch 参数不是本版支持的对外模式；接口统一走受保护的逐句工作流。

实现依据：[MiniMax TTS](https://platform.minimax.io/docs/api-reference/speech-t2a-http)、
[MiniMax 音色目录](https://platform.minimax.io/docs/api-reference/voice-management-get)、
[Azure 音色目录](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/rest-text-to-speech)。

## Python 客户端

仓库 `story_client.py` 提供带 mTLS、鉴权、轮询、下载的客户端：

```python
from story_client import StoryVoiceClient
client = StoryVoiceClient()  # 从 STORY_TTS_TOKEN / STORY_TTS_CERT / STORY_TTS_CERT_KEY 读取
result = client.synthesize("你好。晚安。", "babystory-moon-postman-narrator", provider="minimax")
client.download(result["audio_url"], "story.mp3")
client.download(result["subtitles"]["srt"], "story.srt")
```

2026-09-20 验收：Azure 779 个音色；MiniMax 303 个官方音色、2 个设计音色。
两家引擎均通过两句真实合成及 MP3 / JSON / SRT / VTT 下载。
已补齐 tools 域名缺少的 Let’s Encrypt Root YE / X2 交叉签名链，修复 TLS 验证。
证书续期时需保持完整链，参考 [官方证书链](https://letsencrypt.org/certificates/)。

本地测试（独立 ffmpeg 避免依赖系统编解码库）：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
TTS_FFMPEG="$(.venv/bin/python -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())')" .venv/bin/python -m pytest -q
```
