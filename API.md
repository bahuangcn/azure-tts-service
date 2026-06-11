# Azure TTS Service — API 接口文档

> 适用版本: v1.0 | 基础路径: `http://{host}:8002/azure_api` | 内容类型: `application/json`

## 目录

- [1. 概述](#1-概述)
- [2. 任务状态流转](#2-任务状态流转)
- [3. 端点详情](#3-端点详情)
  - [POST /azure_api/tts — 提交合成任务](#post-azure_apitts--提交合成任务)
  - [GET /azure_api/tts/{task_id} — 查询任务详情](#get-azure_apittstask_id--查询任务详情)
  - [GET /azure_api/tts/audio/{task_id} — 下载音频文件](#get-azure_apittsaudiotask_id--下载音频文件)
  - [GET /azure_api/tts — 任务列表](#get-azure_apitts--任务列表)
  - [DELETE /azure_api/tts/{task_id} — 删除任务](#delete-azure_apittstask_id--删除任务)
  - [GET /azure_api/health — 健康检查](#get-azure_apihealth--健康检查)
- [4. 两种合成模式](#4-两种合成模式)
- [5. 错误处理](#5-错误处理)
- [6. 调用示例](#6-调用示例)

---

## 1. 概述

Azure TTS Service 是一个**异步**文本转语音 HTTP 微服务。核心流程：

```
提交任务 → 立即返回 task_id → 后台合成 → 轮询/回调获取结果 → 下载音频
```

**关键设计**：
- 所有合成操作是异步的——POST 立即返回，合成在后台线程执行
- 需要客户端**轮询** `GET /azure_api/tts/{task_id}` 直到 `status` 变为 `completed` 或 `failed`
- 支持两种合成引擎：SDK（实时）和 Batch（REST API），通过 `mode` 参数切换
- 返回**词级时间戳**（每个词的起止毫秒偏移），适用于字幕/对齐场景

---

## 2. 任务状态流转

```
pending ──→ processing ──→ completed
                │
                └──→ failed
```

| 状态 | 含义 | 此时可用的额外字段 |
|------|------|-------------------|
| `pending` | 已入队，等待处理 | — |
| `processing` | 正在合成中（batch 模式下 `azure_status` 字段指示 Azure 端状态） | `azure_status` |
| `completed` | 合成成功 | `audio_url`, `word_timings`, `total_ms`, `audio_file` |
| `failed` | 合成失败 | `error` |

**Batch 模式子状态**（`azure_status` 字段，仅在 `mode=batch` 且 `status=processing` 时出现）：

```
NotStarted → Running → Succeeded
                     → Failed
```

---

## 3. 端点详情

### POST /azure_api/tts — 提交合成任务

提交文本合成任务，立即返回 `task_id`。文本在后台由 worker 线程异步合成。

**请求体** (JSON):

```json
{
  "text": "要合成的文本内容（必填，不可为空或全空白）",
  "voice": "zh-CN-XiaochenNeural",
  "rate": "+20%",
  "mode": "sdk"
}
```

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `text` | `string` | **是** | — | 待合成文本，不能为空或全空白字符 |
| `voice` | `string` | 否 | `zh-CN-XiaochenNeural` | Azure 语音名称，如 `zh-CN-YunxiNeural`（男声）、`en-US-JennyNeural` |
| `rate` | `string` | 否 | `+20%` | 语速，SSML prosody rate 格式：`"+50%"` / `"-20%"` / `"1.0"` |
| `mode` | `string` | 否 | `"sdk"` | 合成引擎：`"sdk"`（Azure Speech SDK）或 `"batch"`（REST API） |

**响应** (200):

```json
{
  "task_id": "tts_a1b2c3d4e5f6",
  "status": "pending",
  "mode": "sdk"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `task_id` | `string` | 唯一任务标识，格式 `tts_` + 12 位十六进制 |
| `status` | `string` | 初始状态，固定为 `"pending"` |
| `mode` | `string` | 回显请求中指定的合成模式 |

**错误**:

| 状态码 | 说明 |
|--------|------|
| `400` | `text` 为空或全空白字符 |

---

### GET /azure_api/tts/{task_id} — 查询任务详情

**路径参数**:

| 参数 | 类型 | 说明 |
|------|------|------|
| `task_id` | `string` | 由 `POST /azure_api/tts` 返回的任务 ID |

**响应** (200) — pending 状态:

```json
{
  "task_id": "tts_a1b2c3d4e5f6",
  "status": "pending",
  "text": "你好世界",
  "voice": "zh-CN-XiaochenNeural",
  "rate": "+20%",
  "mode": "sdk",
  "audio_file": null,
  "word_timings": null,
  "total_ms": null,
  "error": null,
  "created_at": "2026-06-11T10:30:00.123456",
  "updated_at": "2026-06-11T10:30:00.123456"
}
```

**响应** (200) — completed 状态:

```json
{
  "task_id": "tts_a1b2c3d4e5f6",
  "status": "completed",
  "text": "你好世界",
  "voice": "zh-CN-XiaochenNeural",
  "rate": "+20%",
  "mode": "sdk",
  "audio_file": "tts_a1b2c3d4e5f6.mp3",
  "audio_url": "/azure_api/tts/audio/tts_a1b2c3d4e5f6",
  "word_timings": [
    {"text": "你好", "start_ms": 50, "end_ms": 187},
    {"text": "世界", "start_ms": 187, "end_ms": 350}
  ],
  "total_ms": 350,
  "error": null,
  "created_at": "2026-06-11T10:30:00.123456",
  "updated_at": "2026-06-11T10:30:05.654321"
}
```

**响应字段全表**:

| 字段 | 类型 | 何时出现 | 说明 |
|------|------|----------|------|
| `task_id` | `string` | 总是 | 唯一任务标识 |
| `status` | `string` | 总是 | `pending` / `processing` / `completed` / `failed` |
| `text` | `string` | 总是（单任务查询） | 原始合成文本，**列表查询中不返回**（隐私保护） |
| `voice` | `string` | 总是 | Azure 语音名称 |
| `rate` | `string` | 总是 | 语速参数 |
| `mode` | `string` | 总是 | `sdk` 或 `batch` |
| `audio_file` | `string\|null` | completed 时 | 音频文件名（不含目录路径） |
| `audio_url` | `string\|null` | completed 时 | 音频下载相对路径 `/azure_api/tts/audio/{task_id}` |
| `word_timings` | `array\|null` | completed 时 | 词级时间戳，见下方子表 |
| `total_ms` | `int\|null` | completed 时 | 音频总时长（毫秒） |
| `synthesis_id` | `string\|null` | batch 模式 | Azure Batch API 的合成 ID |
| `azure_status` | `string\|null` | batch processing | Azure 端子状态：`NotStarted`/`Running`/`Succeeded`/`Failed` |
| `error` | `string\|null` | failed 时 | 错误描述信息 |
| `created_at` | `string` | 总是 | 任务创建时间（ISO 8601） |
| `updated_at` | `string` | 总是 | 最后更新时间（ISO 8601） |

**word_timings 元素结构**:

| 字段 | 类型 | 说明 |
|------|------|------|
| `text` | `string` | 词文本（中文单字/词，英文单词） |
| `start_ms` | `int` | 词开始时间（毫秒，相对于音频起始） |
| `end_ms` | `int` | 词结束时间（毫秒，相对于音频起始） |

**错误**:

| 状态码 | 说明 |
|--------|------|
| `404` | `task_id` 不存在 |

---

### GET /azure_api/tts/audio/{task_id} — 下载音频文件

**路径参数**:

| 参数 | 类型 | 说明 |
|------|------|------|
| `task_id` | `string` | 已完成任务的任务 ID |

**响应** (200):

- `Content-Type: audio/mpeg`
- 二进制音频数据（MP3 格式，macOS 上可能实际为 WAV 但 MIME 类型统一声明为 `audio/mpeg`）

**错误**:

| 状态码 | 说明 |
|--------|------|
| `404` | 任务不存在 / 尚未完成 (`audio_file` 为 NULL) / 磁盘文件不存在 |

---

### GET /azure_api/tts — 任务列表

**查询参数**:

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `limit` | `int` | 否 | `50` | 最大返回条数 |

**响应** (200):

```json
[
  {
    "task_id": "tts_a1b2c3d4e5f6",
    "status": "completed",
    "voice": "zh-CN-XiaochenNeural",
    "rate": "+20%",
    "mode": "sdk",
    "audio_file": "tts_a1b2c3d4e5f6.mp3",
    "word_timings": [
      {"text": "你好", "start_ms": 50, "end_ms": 187}
    ],
    "total_ms": 350,
    "error": null,
    "created_at": "2026-06-11T10:30:00.123456",
    "updated_at": "2026-06-11T10:30:05.654321"
  }
]
```

> **注意**: 列表响应中 **不包含 `text` 字段**（原始文本仅在单任务查询 `GET /azure_api/tts/{task_id}` 中返回），以减少响应体积和保护用户隐私。

**排序**: 按 `created_at` 降序（最新任务在前）。

---

### DELETE /azure_api/tts/{task_id} — 删除任务

删除任务记录及关联的音频文件（磁盘 + Azure 端资源）。

**路径参数**:

| 参数 | 类型 | 说明 |
|------|------|------|
| `task_id` | `string` | 要删除的任务 ID |

**响应** (200):

```json
{"status": "deleted"}
```

**行为细节**:
1. 删除磁盘上的音频文件（不存在时不报错）
2. Batch 模式：尝试删除 Azure 端的合成任务（best-effort，失败不影响本地删除）
3. 从 SQLite 删除任务记录

**错误**:

| 状态码 | 说明 |
|--------|------|
| `404` | 任务不存在 |

---

### GET /azure_api/health — 健康检查

**响应** (200):

```json
{
  "status": "ok",
  "queue_size": 0,
  "batch_queue_size": 3
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `status` | `string` | 固定为 `"ok"` |
| `queue_size` | `int` | SDK 队列待处理任务数（>100 需关注） |
| `batch_queue_size` | `int` | Batch 队列待处理任务数 |

---

## 4. 两种合成模式

### SDK 模式 (`mode=sdk`)

- **适用场景**: 实时交互、短文本（<2000 字符）、F0/S0 层均可
- **引擎**: Azure Speech SDK (`azure-cognitiveservices-speech`)
- **并发**: 由 `TTS_MAX_WORKERS` 控制（默认 4 线程）
- **长文本**: 自动分块（>2000 字符），逐块合成后拼接音频并合并时间戳
- **超时**: SDK 单次调用最多 `TTS_SDK_TIMEOUT` 秒（默认 600s）

### Batch 模式 (`mode=batch`)

- **适用场景**: 批量处理、超长文本、不需要实时结果
- **引擎**: Azure Batch Synthesis REST API（纯 HTTP，不需要 SDK）
- **并发**: 由 `TTS_MAX_BATCH_WORKERS` 控制（默认 2 线程）
- **流程**: 提交 PUT → 轮询 GET → 下载 ZIP → 解压提取音频和词边界
- **限制**: **F0 层不支持**，需 S0 层；中国区用户可能需要 HTTP 代理
- **超时**: 最多轮询 `TTS_BATCH_MAX_TIMEOUT` 秒（默认 600s），间隔 `TTS_BATCH_POLL_INTERVAL` 秒（默认 5s）

### 模式对比

| 维度 | SDK | Batch |
|------|-----|-------|
| 启动延迟 | 低（立即合成） | 高（Azure 排队 + 轮询） |
| 长文本支持 | 自动分块 | 单次提交 |
| F0 层支持 | ✅ | ❌ |
| 网络要求 | 直连 Azure | 需要能访问 `*.api.cognitive.microsoft.com` |
| 代理支持 | 系统代理 | `HTTP_PROXY`/`HTTPS_PROXY` 环境变量 |
| 词边界精度 | 100ns 单位（回调） | ms 单位（JSON 文件） |
| 音频格式 | WAV（macOS） | WAV（Azure 输出） |

---

## 5. 错误处理

### HTTP 错误码

| 状态码 | 端点 | 含义 |
|--------|------|------|
| `400` | `POST /azure_api/tts` | `text` 字段为空或全空白 |
| `404` | `GET /azure_api/tts/{task_id}` | 任务 ID 不存在 |
| `404` | `GET /azure_api/tts/audio/{task_id}` | 音频未就绪或任务不存在 |
| `404` | `DELETE /azure_api/tts/{task_id}` | 任务 ID 不存在 |

### 任务级错误（`status=failed`）

当任务合成失败时，`GET /azure_api/tts/{task_id}` 响应中 `status` 为 `"failed"`，`error` 字段包含具体错误信息。常见错误：

| error 内容关键词 | 诊断 |
|-----------------|------|
| `PermissionDenied` / `401` | Batch 模式在 F0 层不可用（需 S0）或端点 URL 不对 |
| `timed out` | 合成超时——文本过长或网络延迟高 |
| `SSL 连接 Azure 失败` | 网络中间设备阻断了 Azure API（配置 `HTTPS_PROXY`） |
| `无法连接 Azure API` | DNS/网络不可达（检查代理/VPN） |
| `CancellationReason` | SDK 合成被取消（检查订阅/配额） |

---

## 6. 调用示例

### cURL

```bash
# 1. 提交任务
curl -s -X POST http://localhost:8002/azure_api/tts \
  -H "Content-Type: application/json" \
  -d '{"text":"你好世界，这是一段测试文本。","voice":"zh-CN-XiaochenNeural","rate":"+20%","mode":"sdk"}'
# → {"task_id":"tts_a1b2c3d4e5f6","status":"pending","mode":"sdk"}

# 2. 轮询任务状态（直到 completed）
TASK_ID="tts_a1b2c3d4e5f6"
while true; do
  STATUS=$(curl -s http://localhost:8002/azure_api/tts/$TASK_ID | jq -r '.status')
  echo "Status: $STATUS"
  [ "$STATUS" = "completed" ] || [ "$STATUS" = "failed" ] && break
  sleep 1
done

# 3. 查看完整结果（含词级时间戳）
curl -s http://localhost:8002/azure_api/tts/$TASK_ID | jq .

# 4. 下载音频
curl -o output.mp3 http://localhost:8002/azure_api/tts/audio/$TASK_ID

# 5. 健康检查
curl -s http://localhost:8002/azure_api/health | jq .
```

### Python

```python
import requests
import time

BASE = "http://localhost:8002"

# 提交任务
resp = requests.post(f"{BASE}/azure_api/tts", json={
    "text": "你好世界，这是一段测试文本。",
    "voice": "zh-CN-XiaochenNeural",
    "rate": "+20%",
    "mode": "sdk",
})
task = resp.json()
task_id = task["task_id"]
print(f"任务已提交: {task_id}")

# 轮询等待完成
while True:
    resp = requests.get(f"{BASE}/azure_api/tts/{task_id}")
    data = resp.json()
    print(f"状态: {data['status']}")
    if data["status"] in ("completed", "failed"):
        break
    time.sleep(1)

# 输出结果
if data["status"] == "completed":
    print(f"总时长: {data['total_ms']}ms")
    for w in data["word_timings"]:
        print(f"  {w['text']}: {w['start_ms']}ms → {w['end_ms']}ms")

    # 下载音频
    audio = requests.get(f"{BASE}/azure_api/tts/audio/{task_id}")
    with open(f"{task_id}.mp3", "wb") as f:
        f.write(audio.content)
    print(f"音频已保存: {task_id}.mp3")
else:
    print(f"合成失败: {data['error']}")
```

### JavaScript (fetch)

```javascript
const BASE = "http://localhost:8002";

async function synthesize(text, voice = "zh-CN-XiaochenNeural", rate = "+20%") {
  // 1. 提交任务
  const submitResp = await fetch(`${BASE}/azure_api/tts`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, voice, rate, mode: "sdk" }),
  });
  const { task_id } = await submitResp.json();
  console.log(`任务已提交: ${task_id}`);

  // 2. 轮询直到完成
  let task;
  while (true) {
    const resp = await fetch(`${BASE}/azure_api/tts/${task_id}`);
    task = await resp.json();
    console.log(`状态: ${task.status}`);
    if (task.status === "completed" || task.status === "failed") break;
    await new Promise(r => setTimeout(r, 1000));
  }

  // 3. 返回结果
  if (task.status === "completed") {
    return {
      taskId: task_id,
      totalMs: task.total_ms,
      wordTimings: task.word_timings,
      audioUrl: `${BASE}${task.audio_url}`,
    };
  } else {
    throw new Error(`合成失败: ${task.error}`);
  }
}

// 使用
synthesize("你好世界").then(console.log);
```
