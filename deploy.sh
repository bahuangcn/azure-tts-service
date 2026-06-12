#!/bin/bash
# 部署并重启 azure-tts-service 到 72 服务器（api.exnihilo.site）
set -e

SRC_DIR="$HOME/AIWorkspace/azure-tts-service"
SERVER="72"
REMOTE_DIR="/opt/azure-tts-service"

RSYNC_OPTS=(-avz --delete)

# 排除不需要部署的文件/目录
EXCLUDES=(
    --exclude '.git/'
    --exclude '.claude/'
    --exclude '.codegraph/'
    --exclude '.gitignore'
    --exclude '.env'
    --exclude '__pycache__/'
    --exclude '*.pyc'
    --exclude '.DS_Store'
    --exclude 'venv/'
    --exclude 'deploy.sh'
    --exclude 'logs/'
    --exclude '*.log'
    --exclude '*.db'
    --exclude 'audio/'
)

echo "=== 同步文件到 72 服务器 ==="

ssh "$SERVER" "mkdir -p $REMOTE_DIR"

echo "[0/3] 清理远程 __pycache__（确保重新编译）"
ssh "$SERVER" "find $REMOTE_DIR -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; find $REMOTE_DIR -type f -name '*.pyc' -delete 2>/dev/null; echo '  清理完成'"

echo "[1/3] 同步项目文件（rsync）"
rsync "${RSYNC_OPTS[@]}" "${EXCLUDES[@]}" "$SRC_DIR/" "$SERVER:$REMOTE_DIR/"
echo "  同步完成"

echo "[2/3] 安装/更新依赖"
ssh "$SERVER" "
    # 检查 venv 是否可用，不可用则重建
    if ! $REMOTE_DIR/venv/bin/python -c '' 2>/dev/null; then
        echo '  venv 不可用，重建...'
        rm -rf $REMOTE_DIR/venv
        python3 -m venv $REMOTE_DIR/venv
    fi
    $REMOTE_DIR/venv/bin/pip install -r $REMOTE_DIR/requirements.txt -q
    echo '  依赖安装完成'"

echo ""
echo "=== 重启服务 ==="
echo "[3/3] 重启 azure-tts 服务"
ssh "$SERVER" "systemctl daemon-reload && systemctl restart azure-tts && systemctl status azure-tts --no-pager | head -8"

echo ""
echo "✓ 部署完成"
