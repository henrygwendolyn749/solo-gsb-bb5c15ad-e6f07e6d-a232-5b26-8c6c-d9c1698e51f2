"""启动时的示例数据：仅在数据库为空时写入，覆盖各种典型场景。"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from . import service
from .models import CheckinCreate

# (event_no, runner_id, station_seq, occurred_at, corrects, revoke)
# 站点总数默认为 6（STATION_COUNT），分段为 1→2 … 5→6。
SEED_EVENTS = [
    # R001：完整成绩，顺序打卡
    ("S-R001-1", "R001", 1, "2026-09-25T08:00:00", None, False),
    ("S-R001-2", "R001", 2, "2026-09-25T08:32:00", None, False),
    ("S-R001-3", "R001", 3, "2026-09-25T09:12:00", None, False),
    ("S-R001-4", "R001", 4, "2026-09-25T09:55:00", None, False),
    ("S-R001-5", "R001", 5, "2026-09-25T10:45:00", None, False),
    ("S-R001-6", "R001", 6, "2026-09-25T11:30:00", None, False),
    # R002：乱序补传（先传 1/3/5 站，后补 2/4/6 站），最终分段完整
    ("S-R002-1", "R002", 1, "2026-09-25T08:05:00", None, False),
    ("S-R002-3", "R002", 3, "2026-09-25T09:15:00", None, False),
    ("S-R002-5", "R002", 5, "2026-09-25T10:50:00", None, False),
    ("S-R002-2", "R002", 2, "2026-09-25T08:45:00", None, False),
    ("S-R002-4", "R002", 4, "2026-09-25T10:00:00", None, False),
    ("S-R002-6", "R002", 6, "2026-09-25T11:40:00", None, False),
    # R003：缺第 4 站，分段 3→4、4→5 待定
    ("S-R003-1", "R003", 1, "2026-09-25T08:10:00", None, False),
    ("S-R003-2", "R003", 2, "2026-09-25T08:50:00", None, False),
    ("S-R003-3", "R003", 3, "2026-09-25T09:35:00", None, False),
    ("S-R003-5", "R003", 5, "2026-09-25T11:05:00", None, False),
    ("S-R003-6", "R003", 6, "2026-09-25T11:55:00", None, False),
    # R004：第 2 站先录入错误时间 12:30，随后用新事件更正为 08:40
    ("S-R004-1", "R004", 1, "2026-09-25T08:02:00", None, False),
    ("S-R004-2X", "R004", 2, "2026-09-25T12:30:00", None, False),
    ("S-R004-3", "R004", 3, "2026-09-25T09:20:00", None, False),
    ("S-R004-4", "R004", 4, "2026-09-25T10:05:00", None, False),
    ("S-R004-5", "R004", 5, "2026-09-25T11:00:00", None, False),
    ("S-R004-6", "R004", 6, "2026-09-25T11:50:00", None, False),
    ("S-R004-2", "R004", 2, "2026-09-25T08:40:00", "S-R004-2X", False),
    # R005：第 2 站打卡被撤销，分段 1→2、2→3 待定
    ("S-R005-1", "R005", 1, "2026-09-25T08:15:00", None, False),
    ("S-R005-2", "R005", 2, "2026-09-25T08:55:00", None, True),
    ("S-R005-3", "R005", 3, "2026-09-25T09:40:00", None, False),
    # R006：第 3 站时间早于第 2 站（时间倒退），分段 2→3 待定
    ("S-R006-1", "R006", 1, "2026-09-25T08:20:00", None, False),
    ("S-R006-2", "R006", 2, "2026-09-25T09:30:00", None, False),
    ("S-R006-3", "R006", 3, "2026-09-25T09:10:00", None, False),
    ("S-R006-4", "R006", 4, "2026-09-25T10:20:00", None, False),
    ("S-R006-5", "R006", 5, "2026-09-25T11:10:00", None, False),
    ("S-R006-6", "R006", 6, "2026-09-25T12:00:00", None, False),
]


def seed_if_empty(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM checkins LIMIT 1").fetchone() is not None:
        return
    for event_no, runner_id, station_seq, occurred_at, corrects, revoke in SEED_EVENTS:
        service.create_checkin(
            conn,
            CheckinCreate(
                event_no=event_no,
                runner_id=runner_id,
                station_seq=station_seq,
                occurred_at=datetime.fromisoformat(occurred_at),
                corrects=corrects,
            ),
        )
        if revoke:
            service.revoke_checkin(conn, event_no)
