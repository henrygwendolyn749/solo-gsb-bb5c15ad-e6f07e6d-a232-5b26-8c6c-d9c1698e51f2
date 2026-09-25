"""核心业务逻辑：打卡写入 / 更正 / 撤销 / 分段重算 / 更正预演。"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

from fastapi import HTTPException

from .models import (
    BatchCheckinCreate,
    BatchCheckinOut,
    BatchItemOut,
    BatchRunnerResult,
    CheckinCreate,
    CheckinOut,
    CorrectionPreview,
    PreviewCorrectionOut,
    RunnerPreview,
    RunnerSummary,
    Segment,
    SegmentChange,
    SplitsOut,
    StationTime,
)

ACTIVE = "active"
SUPERSEDED = "superseded"
REVOKED = "revoked"


def station_count() -> int:
    """赛道站点总数（分段数 = 站点数 - 1）。"""
    return int(os.environ.get("STATION_COUNT", "6"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _to_utc(dt: datetime) -> datetime:
    """统一为 UTC；无时区信息的时间按 UTC 处理。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _fmt_duration(seconds: int) -> str:
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{sign}{h}:{m:02d}:{s:02d}"


def _row_to_out(row: sqlite3.Row) -> CheckinOut:
    return CheckinOut(
        event_no=row["event_no"],
        runner_id=row["runner_id"],
        station_seq=row["station_seq"],
        occurred_at=datetime.fromisoformat(row["occurred_at"]),
        status=row["status"],
        corrects=row["corrects"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _get_row(conn: sqlite3.Connection, event_no: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM checkins WHERE event_no = ?", (event_no,)
    ).fetchone()


def _validate_payload(
    conn: sqlite3.Connection, payload: CheckinCreate
) -> sqlite3.Row | None:
    """登记与预演共用的校验：站点范围、事件号唯一性、corrects 目标存在且有效。

    返回被更正的原事件行；payload 未携带 corrects 时返回 None。
    """
    count = station_count()
    if payload.station_seq > count:
        raise HTTPException(
            status_code=422,
            detail=f"站点序号 {payload.station_seq} 超出范围（1..{count}）",
        )
    if _get_row(conn, payload.event_no) is not None:
        raise HTTPException(status_code=409, detail=f"事件号 {payload.event_no} 已存在")

    origin = None
    if payload.corrects is not None:
        origin = _get_row(conn, payload.corrects)
        if origin is None:
            raise HTTPException(
                status_code=404, detail=f"被更正的事件 {payload.corrects} 不存在"
            )
        if origin["status"] != ACTIVE:
            raise HTTPException(
                status_code=409,
                detail=f"事件 {payload.corrects} 状态为 {origin['status']}，不能更正；"
                "请更正当前有效的替代事件",
            )
    return origin


def _insert_correction(
    conn: sqlite3.Connection,
    payload: CheckinCreate,
    occurred: datetime,
    created: datetime,
) -> None:
    """在当前事务内执行更正写入：原事件置为 superseded 并插入新事件。"""
    if payload.corrects is not None:
        conn.execute(
            "UPDATE checkins SET status = ? WHERE event_no = ?",
            (SUPERSEDED, payload.corrects),
        )
    conn.execute(
        "INSERT INTO checkins"
        " (event_no, runner_id, station_seq, occurred_at, status, corrects, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            payload.event_no,
            payload.runner_id,
            payload.station_seq,
            occurred.isoformat(),
            ACTIVE,
            payload.corrects,
            created.isoformat(),
        ),
    )


def create_checkin(conn: sqlite3.Connection, payload: CheckinCreate) -> CheckinOut:
    """登记打卡；携带 corrects 时视为更正，原事件被取代。"""
    _validate_payload(conn, payload)
    occurred = _to_utc(payload.occurred_at)
    created = _now()
    with conn:  # 事务：更正与写入同时成功或失败
        _insert_correction(conn, payload, occurred, created)
    return CheckinOut(
        event_no=payload.event_no,
        runner_id=payload.runner_id,
        station_seq=payload.station_seq,
        occurred_at=occurred,
        status=ACTIVE,
        corrects=payload.corrects,
        created_at=created,
    )


def _batch_failure(status_code: int, index: int, event_no: str, reason: str) -> HTTPException:
    """构造批量失败响应：携带失败下标与原因，状态码沿用单条登记的语义。"""
    return HTTPException(
        status_code=status_code,
        detail={
            "message": f"批量打卡在下标 {index} 处失败，整批已回滚",
            "failures": [{"index": index, "event_no": event_no, "reason": reason}],
        },
    )


def create_checkins_batch(
    conn: sqlite3.Connection, payload: BatchCheckinCreate
) -> BatchCheckinOut:
    """批量登记打卡：1~100 条按顺序在同一事务内处理。

    - 每条沿用单条登记的字段与校验（站点范围、事件号唯一、corrects 目标有效）；
    - 后续记录可用 corrects 更正本批前面刚写入且仍有效的事件；
    - 前向引用（含更正自身）、批内重复事件号、无效更正在对应下标处失败；
    - 任一条失败时回滚整批的新增记录与状态变更，并返回失败下标与原因。
    """
    items = payload.checkins
    # 批内事件号首次出现的下标，用于识别批内重复与前向引用
    first_pos: dict[str, int] = {}
    for i, item in enumerate(items):
        first_pos.setdefault(item.event_no, i)

    results: list[BatchItemOut] = []
    runner_ids: list[str] = []

    with conn:  # 单事务：任一失败时回滚整批写入与状态变更
        for i, item in enumerate(items):
            dup = first_pos[item.event_no]
            if dup < i:
                raise _batch_failure(
                    409, i, item.event_no,
                    f"事件号 {item.event_no} 与本批下标 {dup} 重复",
                )
            if item.corrects is not None:
                target = first_pos.get(item.corrects)
                if target is not None and target >= i:
                    reason = (
                        f"事件不能更正自身（corrects={item.corrects}）"
                        if target == i
                        else f"前向引用：被更正的事件 {item.corrects} "
                        f"在本批下标 {target} 才登记"
                    )
                    raise _batch_failure(422, i, item.event_no, reason)
            try:
                origin = _validate_payload(conn, item)
            except HTTPException as exc:
                raise _batch_failure(
                    exc.status_code, i, item.event_no, str(exc.detail)
                ) from exc
            _insert_correction(conn, item, _to_utc(item.occurred_at), _now())
            results.append(BatchItemOut(index=i, event_no=item.event_no))
            if item.runner_id not in runner_ids:
                runner_ids.append(item.runner_id)
            if origin is not None and origin["runner_id"] not in runner_ids:
                runner_ids.append(origin["runner_id"])

        # 全部写入后按现有分段规则重算受影响选手的最终结果
        affected = [
            BatchRunnerResult(
                runner_id=rid, **_summary(_compute_splits(conn, rid)).model_dump()
            )
            for rid in runner_ids
        ]
    return BatchCheckinOut(results=results, affected_runners=affected)


def revoke_checkin(conn: sqlite3.Connection, event_no: str) -> CheckinOut:
    """按事件号撤销一条有效打卡。"""
    row = _get_row(conn, event_no)
    if row is None:
        raise HTTPException(status_code=404, detail=f"事件 {event_no} 不存在")
    if row["status"] != ACTIVE:
        raise HTTPException(
            status_code=409,
            detail=f"事件 {event_no} 状态为 {row['status']}，无需撤销",
        )
    with conn:
        conn.execute(
            "UPDATE checkins SET status = ? WHERE event_no = ?", (REVOKED, event_no)
        )
    return CheckinOut(
        event_no=row["event_no"],
        runner_id=row["runner_id"],
        station_seq=row["station_seq"],
        occurred_at=datetime.fromisoformat(row["occurred_at"]),
        status=REVOKED,
        corrects=row["corrects"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def get_checkin(conn: sqlite3.Connection, event_no: str) -> CheckinOut:
    row = _get_row(conn, event_no)
    if row is None:
        raise HTTPException(status_code=404, detail=f"事件 {event_no} 不存在")
    return _row_to_out(row)


def list_checkins(
    conn: sqlite3.Connection, runner_id: str | None = None
) -> list[CheckinOut]:
    if runner_id is None:
        rows = conn.execute(
            "SELECT * FROM checkins ORDER BY created_at, event_no"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM checkins WHERE runner_id = ? ORDER BY created_at, event_no",
            (runner_id,),
        ).fetchall()
    return [_row_to_out(r) for r in rows]


def get_splits(conn: sqlite3.Connection, runner_id: str) -> SplitsOut:
    """按站点顺序重算某选手的相邻站分段用时（详见 _compute_splits）。"""
    any_row = conn.execute(
        "SELECT 1 FROM checkins WHERE runner_id = ? LIMIT 1", (runner_id,)
    ).fetchone()
    if any_row is None:
        raise HTTPException(status_code=404, detail=f"选手 {runner_id} 无任何打卡记录")
    return _compute_splits(conn, runner_id)


def _compute_splits(conn: sqlite3.Connection, runner_id: str) -> SplitsOut:
    """按站点顺序重算某选手的相邻站分段用时。

    - 仅统计 status='active' 的打卡；同站多条有效打卡取最早时间；
    - 相邻站缺打卡或时间倒退的分段标记为 pending 并说明原因；
    - 每次查询都基于最新数据重算，因此补传、更正、撤销即时生效。
    """
    rows = conn.execute(
        "SELECT * FROM checkins WHERE runner_id = ? AND status = ?",
        (runner_id, ACTIVE),
    ).fetchall()

    # 同站多条有效打卡取最早时间
    best: dict[int, tuple[datetime, str]] = {}
    for r in rows:
        t = datetime.fromisoformat(r["occurred_at"])
        cur = best.get(r["station_seq"])
        if cur is None or t < cur[0]:
            best[r["station_seq"]] = (t, r["event_no"])

    count = station_count()
    stations = [
        StationTime(station_seq=seq, event_no=ev, occurred_at=t)
        for seq, (t, ev) in sorted(best.items())
    ]

    segments: list[Segment] = []
    for k in range(1, count):
        a = best.get(k)
        b = best.get(k + 1)
        if a is None and b is None:
            seg = Segment(
                from_station=k,
                to_station=k + 1,
                status="pending",
                reason=f"站点 {k} 与站点 {k + 1} 均缺少有效打卡",
            )
        elif a is None:
            seg = Segment(
                from_station=k,
                to_station=k + 1,
                status="pending",
                reason=f"站点 {k} 缺少有效打卡",
            )
        elif b is None:
            seg = Segment(
                from_station=k,
                to_station=k + 1,
                status="pending",
                reason=f"站点 {k + 1} 缺少有效打卡",
            )
        elif b[0] < a[0]:
            seg = Segment(
                from_station=k,
                to_station=k + 1,
                status="pending",
                reason=(
                    f"站点 {k + 1} 时间 {b[0].isoformat()} 早于"
                    f"站点 {k} 时间 {a[0].isoformat()}（时间倒退）"
                ),
            )
        else:
            seconds = int((b[0] - a[0]).total_seconds())
            seg = Segment(
                from_station=k,
                to_station=k + 1,
                status="ok",
                duration_seconds=seconds,
                duration=_fmt_duration(seconds),
            )
        segments.append(seg)

    complete = all(s.status == "ok" for s in segments)
    total_seconds = (
        sum(s.duration_seconds for s in segments) if complete else None
    )
    return SplitsOut(
        runner_id=runner_id,
        station_count=count,
        complete=complete,
        total_seconds=total_seconds,
        total=_fmt_duration(total_seconds) if total_seconds is not None else None,
        stations=stations,
        segments=segments,
    )


def _summary(splits: SplitsOut) -> RunnerSummary:
    return RunnerSummary(
        complete=splits.complete,
        total_seconds=splits.total_seconds,
        total=splits.total,
    )


def preview_correction(
    conn: sqlite3.Connection, payload: CorrectionPreview
) -> PreviewCorrectionOut:
    """预演更正：与正式更正相同的校验和写入逻辑，但整体回滚。

    在 SAVEPOINT 内模拟"原事件被取代 + 新事件生效"，按现有同站最早与
    相邻站规则重算受影响选手（跨选手时含原选手与新选手）的更正前后结果，
    随后回滚：不占用事件号、不改变打卡状态或成绩，相同请求随后可正式提交。
    """
    origin = _validate_payload(conn, payload)  # corrects 必填，origin 必非 None

    runner_ids = [origin["runner_id"]]
    if payload.runner_id not in runner_ids:
        runner_ids.append(payload.runner_id)

    before = {rid: _compute_splits(conn, rid) for rid in runner_ids}

    occurred = _to_utc(payload.occurred_at)
    conn.execute("SAVEPOINT preview_correction")
    try:
        _insert_correction(conn, payload, occurred, _now())
        after = {rid: _compute_splits(conn, rid) for rid in runner_ids}
    finally:
        conn.execute("ROLLBACK TO SAVEPOINT preview_correction")
        conn.execute("RELEASE SAVEPOINT preview_correction")

    affected = []
    for rid in runner_ids:
        b, a = before[rid], after[rid]
        changes = [
            SegmentChange(
                from_station=seg_b.from_station,
                to_station=seg_b.to_station,
                before=seg_b,
                after=seg_a,
            )
            for seg_b, seg_a in zip(b.segments, a.segments)
            if (seg_b.status, seg_b.duration_seconds, seg_b.reason)
            != (seg_a.status, seg_a.duration_seconds, seg_a.reason)
        ]
        affected.append(
            RunnerPreview(
                runner_id=rid,
                before=_summary(b),
                after=_summary(a),
                changed_segments=changes,
            )
        )
    return PreviewCorrectionOut(
        event_no=payload.event_no,
        runner_id=payload.runner_id,
        station_seq=payload.station_seq,
        occurred_at=occurred,
        corrects=payload.corrects,
        affected_runners=affected,
    )
