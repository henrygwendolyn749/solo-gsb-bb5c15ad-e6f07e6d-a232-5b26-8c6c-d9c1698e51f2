"""SQLite 连接与建表。"""
from __future__ import annotations

import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS checkins (
    event_no    TEXT PRIMARY KEY,               -- 唯一事件号
    runner_id   TEXT NOT NULL,                  -- 选手号
    station_seq INTEGER NOT NULL,               -- 站点序号（从 1 开始）
    occurred_at TEXT NOT NULL,                  -- 打卡发生时间（ISO 8601，UTC）
    status      TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'superseded', 'revoked')),
    corrects    TEXT REFERENCES checkins(event_no),  -- 本事件更正的原事件号
    created_at  TEXT NOT NULL                   -- 记录写入时间（ISO 8601，UTC）
);
CREATE INDEX IF NOT EXISTS idx_checkins_runner_station
    ON checkins (runner_id, station_seq);

-- 已发布的成绩版本：发布时整版快照，此后不再随实时打卡变化
CREATE TABLE IF NOT EXISTS publications (
    version        INTEGER PRIMARY KEY AUTOINCREMENT,  -- 递增版本号，从 1 开始
    created_at     TEXT NOT NULL,                      -- 发布时间（ISO 8601，UTC）
    runner_count   INTEGER NOT NULL,                   -- 本版选手数（含未完赛）
    complete_count INTEGER NOT NULL                    -- 本版完赛选手数
);
-- 每个版本内每位选手的完整分段结果（含 pending 分段），以 JSON 原样固化
CREATE TABLE IF NOT EXISTS publication_runners (
    version     INTEGER NOT NULL REFERENCES publications(version),
    runner_id   TEXT NOT NULL,
    complete    INTEGER NOT NULL,                      -- 1=完赛，0=未完赛
    splits_json TEXT NOT NULL,                         -- SplitsOut 的 JSON 快照
    PRIMARY KEY (version, runner_id)
);
"""


def db_path() -> str:
    return os.environ.get("DB_PATH", "timing.db")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 并发写（打卡 / 发布）排队等待对方事务完成，而不是立即 SQLITE_BUSY
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db() -> None:
    directory = os.path.dirname(os.path.abspath(db_path()))
    os.makedirs(directory, exist_ok=True)
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
