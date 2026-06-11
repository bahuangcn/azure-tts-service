"""
SQLite 持久化：建表 + 连接管理。

数据库文件路径来自 config.DB_PATH。
所有连接使用 sqlite3.Row 工厂，访问列时可用 row["column_name"] 语法。
"""
import sqlite3
from contextlib import contextmanager

from config import DB_PATH


def init_db():
    """
    创建 tasks 表（如不存在）。

    表结构说明：
      task_id      — 主键，格式 tts_<12位hex>，由 create_tts_task 生成
      status       — pending → processing → completed | failed
      text         — 原始合成文本，列表查询时不返回（隐私 + 减少传输量）
      voice        — Azure 语音名称（如 zh-CN-XiaochenNeural）
      rate         — 语速调节（如 +20%、-10%），对应 SSML prosody rate
      audio_file   — 合成后 MP3 文件名（不含目录），completed 时写入
      word_timings — JSON 字符串，词级时间戳 [{"text":"…","start_ms":…,"end_ms":…}]
      total_ms     — 音频总时长（毫秒），从 MP3 文件元数据读取
      error        — 失败时的错误详情
      created_at   — ISO 8601 时间字符串
      updated_at   — ISO 8601 时间字符串，每次状态变更更新

    返回 sqlite3.Connection（调用方负责关闭）。
    """
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            task_id     TEXT PRIMARY KEY,
            status      TEXT NOT NULL DEFAULT 'pending',
            text        TEXT NOT NULL,
            voice       TEXT NOT NULL,
            rate        TEXT NOT NULL,
            audio_file  TEXT,
            word_timings TEXT,
            total_ms    INTEGER,
            error       TEXT,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        )
    """)
    conn.commit()

    # ── 迁移：为 batch synthesis 增加列（幂等，已存在则跳过）──────────────
    migrations = [
        ("mode",          "TEXT NOT NULL DEFAULT 'sdk'"),
        ("synthesis_id",  "TEXT"),
        ("azure_status",  "TEXT"),
        ("result_url",    "TEXT"),
    ]
    for col, col_def in migrations:
        try:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} {col_def}")
        except sqlite3.OperationalError:
            pass  # 列已存在

    # 存量数据回填：确保现有行 mode = 'sdk'
    conn.execute("UPDATE tasks SET mode = 'sdk' WHERE mode IS NULL")
    conn.commit()

    return conn


@contextmanager
def get_db():
    """
    SQLite 连接上下文管理器，自动关闭连接。

    特性：
      - row_factory = sqlite3.Row → 支持 row["column"] 字典式访问
      - timeout = 10s → 遇到锁时最多等待 10 秒（避免并发写入时立即报错）
      - 退出 with 块时自动 conn.close()

    用法：
      with get_db() as conn:
          row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (tid,)).fetchone()
    """
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()
