"""SQLite 连接与建表。"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

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

-- 成绩发布版本：版本号单调递增，整版快照原子写入
CREATE TABLE IF NOT EXISTS publications (
    version       INTEGER PRIMARY KEY AUTOINCREMENT,  -- 发布版本号（从 1 递增）
    published_at  TEXT NOT NULL,                      -- 发布时间（ISO 8601，UTC）
    station_count INTEGER NOT NULL,                   -- 发布时的赛道站点总数
    runner_count  INTEGER NOT NULL                    -- 本版本包含的选手数
);

-- 每个版本中各选手的完整分段结果（快照 JSON）；发布后不可变
CREATE TABLE IF NOT EXISTS publication_entries (
    version   INTEGER NOT NULL REFERENCES publications(version) ON DELETE CASCADE,
    runner_id TEXT NOT NULL,
    position  INTEGER NOT NULL,                  -- 版内顺序（按选手号排序）
    data      TEXT NOT NULL,                     -- SplitsOut 快照 JSON
    PRIMARY KEY (version, runner_id)
);
"""

# 写锁竞争时等待而非立即 "database is locked"；
# 发布（BEGIN IMMEDIATE）与并发打卡借此串行化
BUSY_TIMEOUT_MS = 5000


def db_path() -> str:
    return os.environ.get("DB_PATH", "timing.db")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


@contextmanager
def immediate_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """BEGIN IMMEDIATE 事务：先获取写锁再读写。

    持锁期间其他连接的写入（打卡 / 更正 / 撤销 / 其他发布）一律阻塞，
    因此事务内读到的全部打卡都来自同一个提交时点，同一版本不会混入
    不同时间点的数据；版本头与全部选手快照在同事务内原子提交。
    """
    old_level = conn.isolation_level
    conn.isolation_level = None  # 关闭 sqlite3 自动 BEGIN，自行发出 BEGIN IMMEDIATE
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.isolation_level = old_level


def init_db() -> None:
    directory = os.path.dirname(os.path.abspath(db_path()))
    os.makedirs(directory, exist_ok=True)
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
