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
"""


def db_path() -> str:
    return os.environ.get("DB_PATH", "timing.db")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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
