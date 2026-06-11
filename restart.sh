#!/bin/bash
# ============================================================
# Azure TTS Service — 重启脚本
# 用法: ./restart.sh
# ============================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

PORT=8002
PID=$(lsof -ti :$PORT 2>/dev/null || true)

echo "========================================"
echo "  Azure TTS Service — 重启"
echo "========================================"

# ── 1. 停掉旧进程 ──────────────────────────────────────────
if [ -n "$PID" ]; then
    echo "→ 停止旧进程 (PID: $PID)..."
    kill $PID 2>/dev/null || true
    sleep 1
    # 如果还没死，强制杀掉
    if lsof -ti :$PORT >/dev/null 2>&1; then
        echo "→ 强制终止..."
        kill -9 $(lsof -ti :$PORT) 2>/dev/null || true
        sleep 0.5
    fi
    echo "✓ 已停止"
else
    echo "→ 端口 $PORT 无运行中的进程"
fi

# ── 2. 加载环境变量 ────────────────────────────────────────
echo "→ 加载环境变量..."

# .env 文件（Azure 凭证）
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    source "$SCRIPT_DIR/.env"
    set +a
fi

# 本地开发路径覆盖（生产环境用 /opt 默认值，无需设置）
if [ -z "$TTS_AUDIO_DIR" ]; then
    export TTS_AUDIO_DIR="$SCRIPT_DIR/audio"
fi
if [ -z "$TTS_DB_PATH" ]; then
    export TTS_DB_PATH="$SCRIPT_DIR/tasks.db"
fi

echo "  AUDIO_DIR = $TTS_AUDIO_DIR"
echo "  DB_PATH   = $TTS_DB_PATH"
echo "  REGION    = ${AZURE_SPEECH_REGION:-eastus}"
echo "  KEY       = ${AZURE_SPEECH_KEY:0:8}***"

# ── 3. 检测 Python ─────────────────────────────────────────
# 优先使用系统 Python 3.13（安装了 fastapi 的那个）
PYTHON=""
for py in /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
          /usr/bin/python3 \
          python3; do
    if command -v "$py" &>/dev/null && "$py" -c "import fastapi" 2>/dev/null; then
        PYTHON="$py"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo "❌ 找不到可用的 Python（需要 fastapi）"
    exit 1
fi
echo "  PYTHON    = $PYTHON"

# ── 4. 启动服务 ────────────────────────────────────────────
echo "→ 启动服务..."
nohup "$PYTHON" app.py > "$SCRIPT_DIR/server.log" 2>&1 &
NEW_PID=$!
echo "  PID = $NEW_PID"

# 等待服务就绪
echo -n "→ 等待服务就绪"
for i in $(seq 1 20); do
    if curl -s http://localhost:$PORT/health >/dev/null 2>&1; then
        echo ""
        echo "✓ 服务已启动 — http://localhost:$PORT"
        echo "========================================"
        exit 0
    fi
    echo -n "."
    sleep 0.5
done

echo ""
echo "⚠ 服务启动超时，请查看日志: tail -f $SCRIPT_DIR/server.log"
echo "========================================"
