#!/usr/bin/env bash
set -euo pipefail
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVER="${TTS_SERVER:-72}"
REMOTE_DIR=/opt/azure-tts-service
# Backups remain outside rsync target; include consistent SQLite snapshot.
ssh "$SERVER" 'sudo -n bash -s' <<'REMOTE'
set -e
backup=/opt/tts-backups/$(date +%Y%m%d-%H%M%S)
mkdir -p "$backup"
chmod 700 "$backup"
tar --exclude=venv --exclude=audio --exclude=logs --exclude=__pycache__ --exclude=tasks.db -czf "$backup/code.tgz" -C /opt azure-tts-service
python3 -c "import sqlite3; s=sqlite3.connect('/opt/azure-tts-service/tasks.db'); d=sqlite3.connect('$backup/tasks.db'); s.backup(d); d.close(); s.close()"
cp /etc/systemd/system/azure-tts.service "$backup/"
cp -a /etc/nginx/conf.d "$backup/nginx"
cp -a /var/www/tools.exnihilo.site/tts "$backup/webui"
echo "Backup: $backup"
REMOTE
rsync -az --exclude='.git/' --exclude='.env' --exclude='.venv/' --exclude='venv/' --exclude='audio/' --exclude='logs/' --exclude='*.db*' --exclude='__pycache__/' --exclude='.pytest_cache/' "$SRC_DIR/" "$SERVER:$REMOTE_DIR/"
ssh "$SERVER" 'sudo -n bash -s' <<'REMOTE'
set -e
cd /opt/azure-tts-service
venv/bin/pip install -q -r requirements.txt
/usr/local/bin/ffmpeg -version >/dev/null
venv/bin/python -m compileall -q . -x 'venv'
install -m 644 webui/index.html /var/www/tools.exnihilo.site/tts/index.html
# New API alias on the same two existing hosts, without modifying their TLS policy.
python3 - <<'PY'
import json
from pathlib import Path
for name in ('api.exnihilo.site.conf', 'tools.exnihilo.site.conf'):
    p=Path('/etc/nginx/conf.d')/name
    s=p.read_text()
    if 'location /api/v1/' not in s:
        s=s.replace('    location /azure_api/ {', '    location /api/v1/ {\n        proxy_pass http://127.0.0.1:12001;\n        proxy_set_header Host $host;\n        client_max_body_size 1m;\n    }\n\n    location /azure_api/ {')
        p.write_text(s)
# Browser access relies on the existing mandatory client certificate. The webui
# application credential stays in a root-only nginx include, never in HTML.
settings = dict(line.split('=', 1) for line in Path('.env').read_text().splitlines()
                if '=' in line and not line.lstrip().startswith('#'))
key = json.loads(settings['TTS_API_KEYS'])['webui']
if not key or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in key):
    raise ValueError('webui proxy key must be URL-safe')
snippet = Path('/etc/nginx/tts-workbench-auth.inc')
snippet.write_text('proxy_set_header Authorization "Bearer ' + key + '";\n')
snippet.chmod(0o600)
p = Path('/etc/nginx/conf.d/tools.exnihilo.site.conf')
s = p.read_text()
if 'ssl_verify_client on;' not in s:
    raise RuntimeError('Workbench requires mandatory nginx client certificate validation')
if 'location /workbench_api/' not in s:
    s = s.replace('    location /azure_api/ {', '''    location /workbench_api/ {
        if ($http_origin !~ "^(https://tools[.]exnihilo[.]site)?$") { return 403; }
        include /etc/nginx/tts-workbench-auth.inc;
        proxy_pass http://127.0.0.1:12001/azure_api/;
        proxy_set_header Host $host;
        client_max_body_size 1m;
        proxy_read_timeout 200s;
    }

    location /azure_api/ {''')
    p.write_text(s)
p=Path('/etc/systemd/system/azure-tts.service')
s=p.read_text().replace('--host 0.0.0.0', '--host 127.0.0.1')
# ffmpeg is installed in /usr/local/bin on 72.
s=s.replace('Environment="PATH=/opt/azure-tts-service/venv/bin"', 'Environment="PATH=/opt/azure-tts-service/venv/bin:/usr/local/bin:/usr/bin:/bin"')
p.write_text(s)
PY
nginx -t
systemctl daemon-reload
systemctl restart azure-tts
systemctl reload nginx
for attempt in {1..15}; do
    if curl --fail --silent http://127.0.0.1:12001/azure_api/health; then exit 0; fi
    sleep 1
done
systemctl status azure-tts --no-pager
exit 1
REMOTE
