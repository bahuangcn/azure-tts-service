#!/usr/bin/env python3
"""
Azure TTS Service — 异步词级时间戳 TTS 微服务。

功能：
  - POST /tts           提交文本合成任务
  - GET  /tts/{id}      查询任务状态（含词级时间戳）
  - GET  /tts/audio/{id} 下载合成 MP3
  - GET  /tts           任务列表
  - DELETE /tts/{id}    删除任务
  - GET  /health        健康检查

模块架构：
  config.py    — 配置（路径、凭证、默认值）
  database.py  — SQLite 表结构和连接
  worker.py    — 后台队列 + Azure TTS 合成引擎
  routes.py    — FastAPI 路由定义
  app.py       — 应用入口（本文件）

启动方式：
  1. 开发模式：  python app.py
  2. 生产模式：  uvicorn app:app --host 0.0.0.0 --port 8002
  3. systemd：   systemctl start azure-tts（使用 azure-tts.service）
"""
import threading

from fastapi import FastAPI

from config import MAX_QUEUE_WORKERS
from database import init_db
from routes import router
from worker import _worker

# ── FastAPI 应用实例 ──────────────────────────────────────────────────────
app = FastAPI(title="Azure TTS Service")

# 注册路由（来自 routes.py 的 APIRouter）
app.include_router(router)


# ── 启动逻辑 ───────────────────────────────────────────────────────────────
def _start_workers():
    """
    初始化数据库并启动后台 Worker 线程池。

    线程配置：
      - 数量 = MAX_QUEUE_WORKERS（默认 4）
      - daemon = True → 主进程退出时自动终止（无需手动 join）
      - 每个线程运行 _worker()，阻塞在 _queue.get() 等待任务
    """
    init_db()
    for _ in range(MAX_QUEUE_WORKERS):
        t = threading.Thread(target=_worker, daemon=True)
        t.start()


# 模块被 import 时自动启动 workers
# （uvicorn 在生产模式下需用 --factory 避免重复启动，
#   或使用 uvicorn app:app 直接 import 模块）
_start_workers()


# ── 开发入口 ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    # reload=True：代码变更时自动重启（仅开发环境使用）
    uvicorn.run("app:app", host="0.0.0.0", port=8002, reload=True)
