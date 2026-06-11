# Azure TTS Service

异步词级时间戳文本转语音微服务，基于 Azure Cognitive Services Speech API。

## 项目概况

- **语言**: Python 3.10+
- **框架**: FastAPI + Uvicorn
- **数据库**: SQLite（任务持久化）
- **语音引擎**: Azure Speech SDK（实时）+ Azure Batch Synthesis REST API（异步批量）
- **默认端口**: 8002

## 架构总览

```
HTTP 请求 → routes.py (FastAPI) → 任务写入 SQLite → 队列分发
                                                    ├── SDK 路径:  worker.py  → Azure Speech SDK
                                                    └── Batch 路径: batch_worker.py → Azure REST API
```

两条合成路径：

| 维度 | SDK 路径 (`mode=sdk`) | Batch 路径 (`mode=batch`) |
|------|----------------------|--------------------------|
| 引擎 | Azure Speech SDK | REST API (HTTP PUT/GET) |
| 方式 | 同步阻塞，线程池并发 | 异步：提交 → 轮询 → 下载 ZIP |
| 默认线程数 | 4 (`TTS_MAX_WORKERS`) | 2 (`TTS_MAX_BATCH_WORKERS`) |
| 超时 | 600s/次 (`TTS_SDK_TIMEOUT`) | 600s/任务 (`TTS_BATCH_MAX_TIMEOUT`) |
| 词边界来源 | SDK `word_boundary` 回调 (100ns单位) | ZIP 中 `.word.json` 文件 (ms单位) |
| 依赖 | `azure-cognitiveservices-speech` | 仅 `requests` |
| F0 层支持 | ✅ | ❌ (需 S0 层) |

## 模块地图

| 文件 | 职责 | 关键导出 |
|------|------|---------|
| [app.py](app.py) | 应用入口，FastAPI 实例化，CORS，worker 启动 | `app` |
| [routes.py](routes.py) | 全部 HTTP 端点定义（APIRouter） | `router` |
| [worker.py](worker.py) | SDK 合成引擎：队列消费 → SSML/SSML-less 合成 → 词边界收集 | `_queue`, `_mark_status`, `_mark_failed` |
| [batch_worker.py](batch_worker.py) | Batch 合成引擎：队列消费 → REST API 提交/轮询/下载 | `_batch_queue` |
| [batch_client.py](batch_client.py) | Azure Batch Synthesis REST API 客户端（纯 HTTP） | `submit_batch_job`, `get_batch_status`, `download_batch_results`, `delete_batch_job` |
| [chunker.py](chunker.py) | 长文本分块 + 音频拼接 + 词边界时间戳合并 | `split_text`, `concat_audio_files`, `merge_word_timings` |
| [config.py](config.py) | 环境变量读取 + 默认值 + .env 回退 | 全部 `TTS_*` / `AZURE_*` 配置常量 |
| [database.py](database.py) | SQLite 建表、迁移、连接管理 | `init_db`, `get_db` |
| [logger.py](logger.py) | 双输出日志（终端 + 按天轮转文件） | `get_logger` |

## 数据流：一个 TTS 请求的生命周期

```
1. POST /azure_api/tts {"text": "...", "voice": "zh-CN-XiaochenNeural", "rate": "+20%", "mode": "sdk"}
2. routes.create_tts_task() → 生成 task_id → INSERT tasks (status=pending) → _queue.put(task_id)
3. worker._worker() 从 _queue.get() 取出 → _synthesize(task_id)
4. _synthesize:
   a. SELECT task → UPDATE status=processing
   b. len(text) ≤ 2000? → _synth_one() 单次合成
   c. len(text) > 2000? → _synthesize_chunked() 分块合成+拼接
   d. 收集 word_boundary 回调 → 计算词级时间戳
   e. mutagen 读取 MP3 总时长
   f. UPDATE status=completed, audio_file, word_timings, total_ms
5. GET /azure_api/tts/{task_id} → 返回状态 + word_timings + audio_url + timing_url
6. GET /azure_api/tts/audio/{task_id} → FileResponse 返回 MP3
   GET /azure_api/tts/{task_id}/timing → Response 返回词级时间戳 JSON
```

## 环境变量

全部配置见 [config.py](config.py)。关键变量：

| 变量 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `AZURE_SPEECH_KEY` | **是** | — | Azure 语音服务密钥 |
| `AZURE_SPEECH_REGION` | 否 | `eastus` | Azure 区域 |
| `TTS_AUDIO_DIR` | 否 | `/opt/azure-tts-service/audio` | 音频输出目录 |
| `TTS_DB_PATH` | 否 | `/opt/azure-tts-service/tasks.db` | SQLite 数据库路径 |
| `TTS_MAX_WORKERS` | 否 | `4` | SDK worker 线程数 |
| `TTS_MAX_BATCH_WORKERS` | 否 | `2` | Batch worker 线程数 |
| `TTS_MAX_CHUNK_CHARS` | 否 | `2000` | 单次合成最大字符数 |
| `TTS_SDK_TIMEOUT` | 否 | `600` | SDK 单次合成超时（秒） |
| `TTS_BATCH_MAX_TIMEOUT` | 否 | `600` | Batch 任务最大等待（秒） |
| `TTS_BATCH_POLL_INTERVAL` | 否 | `5` | Batch 轮询间隔（秒） |
| `TTS_BATCH_ENDPOINT` | 否 | — | 自定义 Batch API 端点（F0→S0 迁移时使用） |
| `TTS_LOG_LEVEL` | 否 | `INFO` | 日志级别 |
| `TTS_LOG_DIR` | 否 | `./logs/` | 日志文件目录 |

## 启动方式

```bash
# 开发模式（代码变更自动重启）
python app.py

# 生产模式
uvicorn app:app --host 0.0.0.0 --port 8002

# 带 factory 模式（避免重复启动 workers）
uvicorn app:app --host 0.0.0.0 --port 8002
```

## 关键技术决策

1. **SQLite 而非 Redis/PostgreSQL**：单机部署、零运维，适合小规模使用。并发写使用 WAL 模式 + timeout 避免锁冲突。
2. **线程池而非 asyncio**：Azure Speech SDK 是同步阻塞的，必须在独立线程中运行，所以整体采用同步路由 + 后台线程 + 队列的架构。
3. **WAV→MP3 兼容**：macOS 上 Azure SDK 不支持 MP3 编码（实际输出 WAV 文件），但文件名和 MIME 类型统一用 `.mp3`/`audio/mpeg`，mutagen 自动检测格式。
4. **长文本分块**：超过 `TTS_MAX_CHUNK_CHARS` 字符时自动切分（段落 → 句子 → 子句 → 硬切分），逐块合成后拼接音频并合并时间戳。
5. **词边界格式统一**：SDK 路径（100ns 单位）和 Batch 路径（ms 单位）内部统一为 `{"text","start_ms","end_ms"}` 格式。

## 常用命令

```bash
# 提交合成任务
curl -X POST http://localhost:8002/azure_api/tts \
  -H "Content-Type: application/json" \
  -d '{"text": "你好世界", "voice": "zh-CN-XiaochenNeural", "rate": "+20%"}'

# 查询任务状态
curl http://localhost:8002/azure_api/tts/{task_id}

# 下载音频
curl -o output.mp3 http://localhost:8002/azure_api/tts/audio/{task_id}

# 下载词级时间戳
curl -o timing.json http://localhost:8002/azure_api/tts/{task_id}/timing

# 查看任务列表
curl http://localhost:8002/azure_api/tts

# 健康检查
curl http://localhost:8002/azure_api/health
```

## API 文档

详细的 API 接口文档见 [API.md](API.md)，包含所有端点的请求/响应格式、状态流转、错误码和调用示例。
