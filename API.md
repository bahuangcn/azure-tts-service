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
  仅用于原生词时间戳的句子归组，不决定合成请求如何分块；无法对应时保留平台原生分句并报告警告。
- 总正文上限：20,000 字符；不再限制正文 200 句或单句 3,000 字。可选 `sentences` 最多 2,000 项。
- Azure: `rate` 如 `+20%`、`-10%`；`pitch` 如 `+0Hz`、`-10Hz`。
- MiniMax: `speed` 0.5–2；`minimax_pitch` -12–12；model 默认由服务配置。
- 连续合成原文，3,000 字符以内一次请求；长文本按自然边界打包分块，不逐句调用。3,000 为服务的单块运行限制，不是平台统一官方上限。

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
    "timing_source": "provider_native",
    "sample_rate": 32000,
    "channels": 1,
    "format": "mp3",
    "size_bytes": 56000,
    "word_timings_available": false,
    "provider_segments": []
  }
}
```

时间均为毫秒，相对于完整音频。Azure 开启 SDK 句子边界事件，使用平台提供的音频偏移和持续时间；
缺少句级事件但存在词级数据时，按原文归组词级时间戳。MiniMax 开启 `subtitle_enable=true`、`subtitle_type=word`，
下载并保留官方字幕 JSON，使用词时间戳归并句子。若只返回字幕段则保留原生段落边界，不伪造细分句子时间。
`duration_ms = end_ms - start_ms`，不以逐句音频长度或字数估计。长文本仅测量合成块长度，用于拼接后偏移后续块时间轴。
`metadata.synthesis_mode=continuous_blocks`，`block_count` 为合成请求块数，`timing_sources` 说明实际时间来源。
`provider_segments` 保留每块原生字幕/句子事件、用量等。字幕缺失时保留音频和已有数据，并返回 `timing_warnings`，页面显示提醒。
既有历史音频及时间轴保持原样，新任务使用此流程。

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
工作台自动使用已配置的供应商密钥，无需填写任何 API Key。浏览器通过已有 mTLS 证书访问 `/workbench_api/`，nginx 在服务端注入 webui 凭证；该凭证保存在 root-only include 文件中，不发给浏览器。该代理只允许本站 Origin。
Baby Story Creator 的 `/api/v1` 和 `/azure_api` 仍使用 mTLS + Bearer。更换客户端密钥后重启；若更换 webui 密钥，运行部署脚本以同步 nginx 内部凭证。

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


工作台支持自动加载、多维分类及音色卡片；各分类按其他已选条件显示可选数量。
语言、角色类型、来源、风格显示在主筛选区；Azure 角色和 MiniMax 年龄分别映射到 role_type，其他官方标签位于“更多分类标签”。
缺少元信息的音色归入“未标注”，仍可选择；工作台仅提供分类筛选，不显示名称搜索框。
语速滑块范围 0.5–2×；Azure 音调滑块为 -50–50 Hz，MiniMax 为 -12–12 半音，切换平台重置音调。

工作台交互回归测试：

```bash
npm install --prefix /tmp/story-voice-ui-test --cache /tmp/story-voice-npm-cache jsdom
NODE_PATH=/tmp/story-voice-ui-test/node_modules node tests/workbench.cjs
```


## 工作台语言与默认配置

`/tts/languages.html` 为语言配置页，先选择平台，再按语言族勾选该平台目录中的主语言；两个平台分别保存配置，默认 `zh`、`en`。
同一语言的地区版本一起启用，工作台筛选保留完整地区代码与名称（例如 `en-US` 美国英语、`en-GB` 英国英语、`en-AU` 澳大利亚英语），不会合并为一个英语选项；多语言音色只按主语言归类（`primary_languages`），不会因附加语言混入；缺失语言信息的音色放在 `unknown`，需在页面显式勾选。
此设置只控制工作台显示，不限制 Baby Story Creator 通过 API 使用其他语言音色。

GET `/preferences?provider=azure|minimax` 返回当前客户端、指定平台的 `languages`（语言族代码数组）和 `preset`（可为空）。
PUT `/preferences?provider=azure|minimax` 只更新该平台的语言配置，默认平台为 Azure。未传字段保留，按客户端隔离并保存到 SQLite；`preset` 为含平台信息的全局默认配置。旧版共用语言配置自动迁移为两个平台各自的初始配置。

```json
{"preset":{"provider":"azure","voice":"zh-CN-XiaochenNeural","speed":1,"pitch":0,"filters":{}}}
```

工作台“保存为默认配置”保存当前引擎、音色、语速、音调和筛选条件；下次打开自动恢复。
如音色已不可用或被语言配置隐藏，显示提示并要求重新选择，不自动换成另一音色合成。

统一显示字段为 `role_type`，选项仅取当前平台：Azure `RolePlayList=YoungAdultFemale` 映射“青年女声”，MiniMax `age=青年` 映射“青年”，不拼接性别，也不混用另一平台字段。`role_type_source` 标明来源。Azure 未提供角色时显示“未标注”。
`OlderAdult*` 映射中年，`Senior*` 映射老年，`Boy/Girl` 映射儿童，Narrator 映射旁白。
原始官方数据保持在 `official`，不独立显示性别、年龄、角色筛选。

轮询仅更新改变的任务，保留未变化记录的 DOM、展开状态和播放器，避免每五秒折叠时间轴。
回归测试：`tests/timeline.cjs`（旧实现可复现折叠）、`tests/languages.cjs`、`tests/workbench.cjs`。


## 多套配置与音色试听

工作台可命名保存多套配置（每客户端最多 50 套），通过下拉框选择后加载，也可更新或删除所选配置。
配置包含平台、音色、语速、音调和筛选条件；最近保存或加载的配置在下次打开时自动恢复。
旧版单一默认配置自动保留为“原默认配置”。全部数据按鉴权客户端隔离。

- `POST /presets`：新建配置；请求为原 `SavedPreset` 字段加 `name`（1–60 字）。
- `PUT /presets/{id}`：更新配置。
- `POST /presets/{id}/load`：设为最近加载的配置，并返回完整偏好。
- `DELETE /presets/{id}`：删除指定配置。
- `GET /preferences`：新增 `presets` 数组和 `default_preset_id`；保留兼容字段 `preset`。

`POST /voices/preview` 接收 `provider`、`voice`、`speed`、`pitch`，校验音色属于当前平台，
使用对应平台实际合成固定短句（中文音色中文，其他音色英文，未标注语言时中文）。
语速和音调与工作台当前设置一致；风格筛选只用于发现音色，不指定试听情绪。
返回 `task_id` 后使用现有任务状态和音频下载接口播放，沿用鉴权、客户端隔离、队列上限和提交限流。
同一客户端、音色和参数的待处理或已生成试听会复用，失败任务可重试，音频文件丢失时重新生成。
试听任务不显示在普通故事合成列表中。


### MiniMax 语言识别来源

MiniMax `get_voice` 实际可能只返回 `voice_id`、`voice_name`、`description`、`created_time`，没有独立 `language`。
语言归类优先使用明确语言字段，再使用 MiniMax 官方系统音色目录分类，最后匹配官方描述。
英文音色描述中的美式、英式、澳大利亚、印度等明确口音分别保留为 `en-US`、`en-GB`、`en-AU`、`en-IN`；未注明地区的英语保留 `en`。
`language_source` 记录字段、官方目录或描述的来源，原始返回保持在 `official`。
自设计/克隆音色不会根据任意自定义 ID 推断语言，缺失信息仍标记未标注。
分类规则只作用于接口实际返回的音色，不把文档中列出的其他音色直接添加为账号可用音色，也不改变 Azure 目录。

参考：[官方系统音色表](https://platform.minimax.io/docs/faq/system-voice-id)、[MiniMax 官方音色参考](https://github.com/MiniMax-AI/skills/blob/main/skills/frontend-dev/references/minimax-voice-catalog.md)（2026-09-21 核对）。


试听交互：播放器在对应音色条目内部展开，条目右上角播放键可暂停/继续；暂停保留播放器和进度。
关闭按钮停止播放并收起播放器；同一时间只播放一个试听。切换试听音色、切换平台或将正在试听的音色筛出结果时停止旧试听。
选择音色或展开更多结果保留仍可见的播放器节点；关闭后的延迟合成结果不会重新打开播放器。


原生时间信息参考：[Azure 合成边界事件](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/how-to-speech-synthesis#subscribe-to-synthesizer-events)、[MiniMax 字幕参数](https://platform.minimax.io/docs/api-reference/speech-t2a-http)（2026-09-21 已用实际请求验证）。

## AI 朗读编排

工作台选择音色并输入原文后，可选择「妈妈讲故事」「睡前安抚」「自然讲述」或自定义要求，生成并查看编排，试听第一段，再勾选应用后生成完整音频。修改原文、音色、语速等输入会使旧编排失效。

POST `/reading-plans`（相同鉴权）示例：

```json
{"text":"很久以前，小兔子看着月亮。妈妈轻声讲起了故事。","provider":"azure","voice":"zh-CN-XiaoxiaoNeural","scene":"mother","instruction":"温暖舒缓，避免夸张表演","speed":0.85,"pitch":0}
```

返回 `id`、`summary`、`blocks`、`warnings`、`capability_note`。段落包含服务端保留的原文、语速、音调、情绪、停顿位置及强调词。模型只生成控制参数，不生成或替换故事正文；找不到原文对应位置的控制会跳过并报告警告。`pitch` 单位为 Azure Hz / MiniMax 半音。文本上限仍为 20,000 字符。

- GET `/reading-plans/{id}`：获取自己的完整编排，可保存为 JSON。
- POST `/reading-plans/{id}/preview`：试听第一段，返回普通任务 ID，通过现有任务和音频接口获取结果；试听不出现在故事历史列表。
- POST `/tts`：在原请求中增加 `arrangement_id`。原文、平台、音色必须与编排一致，否则返回 409；使用编排中的语速、音调等控制。无该字段则保留普通合成方式。

Azure 普通 Neural 音色通过 SSML 执行停顿、语速、音调及该音色支持的风格，强调词通过轻微放慢突出。当前 HD 音色精细控制支持不完整，编排接口会要求换用普通 Neural 音色。MiniMax 使用段落级语速、音调、情绪和段内停顿；强调词使用前后留白，不承诺词级重音控制。

编排合成按自然段落打包（每块最多 1,400 字符），允许不同段落采用不同语气，不为获取句子时间戳而逐句合成。时间轴仍来自平台原生数据；结果 `metadata.synthesis_mode=ai_arranged_blocks`，`metadata.reading_plan` 保留已执行编排。

编排模型使用服务端已有 `MINIMAX_API_KEY`，可通过 `TTS_PLANNER_MODEL` 配置，默认 `MiniMax-M3`；正文及朗读要求会发送给 MiniMax 文本模型。请求沿用客户端鉴权、限流及所有者隔离，模型凭证不返回浏览器。
