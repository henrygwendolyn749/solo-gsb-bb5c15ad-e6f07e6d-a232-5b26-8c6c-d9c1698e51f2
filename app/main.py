"""FastAPI 入口：越野赛分段计时 API。"""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from typing import Iterator, Optional

from fastapi import Depends, FastAPI

from . import db, seed, service
from .models import (
    BatchCheckinCreate,
    BatchCheckinOut,
    CheckinCreate,
    CheckinOut,
    CorrectionPreview,
    PreviewCorrectionOut,
    PublicationOut,
    SplitsOut,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 容器启动时初始化数据库；库为空时写入示例数据
    db.init_db()
    conn = db.get_conn()
    try:
        seed.seed_if_empty(conn)
    finally:
        conn.close()
    yield


app = FastAPI(
    title="越野赛分段计时 API",
    version="1.2.0",
    description=(
        "接收带唯一事件号的打卡，支持按事件号更正 / 撤销、批量登记（同一事务，"
        "任一失败整体回滚），并可预演更正（整体回滚）；"
        "按站点顺序重算相邻站分段用时，缺站或时间倒退的分段标记为待定。"
        "支持成绩发布：整版快照原子保存为递增版本，发布后不受后续打卡影响。"
    ),
    lifespan=lifespan,
)


def get_db() -> Iterator[sqlite3.Connection]:
    conn = db.get_conn()
    try:
        yield conn
    finally:
        conn.close()


@app.get("/", summary="服务信息")
def root() -> dict:
    return {
        "service": "trail-race split timing API",
        "docs": "/docs",
        "station_count": service.station_count(),
    }


@app.get("/api/health", summary="健康检查")
def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/api/checkins",
    response_model=CheckinOut,
    status_code=201,
    summary="登记打卡（携带 corrects 即为更正）",
)
def post_checkin(
    payload: CheckinCreate, conn: sqlite3.Connection = Depends(get_db)
) -> CheckinOut:
    return service.create_checkin(conn, payload)


@app.post(
    "/api/checkins/batch",
    response_model=BatchCheckinOut,
    status_code=201,
    summary="批量登记打卡（1~100 条，同一事务，任一失败整体回滚）",
)
def post_checkins_batch(
    payload: BatchCheckinCreate, conn: sqlite3.Connection = Depends(get_db)
) -> BatchCheckinOut:
    return service.create_checkins_batch(conn, payload)


@app.post(
    "/api/checkins/preview-correction",
    response_model=PreviewCorrectionOut,
    summary="预演更正（试算更正前后结果，整体回滚，无副作用）",
)
def preview_correction(
    payload: CorrectionPreview, conn: sqlite3.Connection = Depends(get_db)
) -> PreviewCorrectionOut:
    return service.preview_correction(conn, payload)


@app.post(
    "/api/checkins/{event_no}/revoke",
    response_model=CheckinOut,
    summary="按事件号撤销打卡",
)
def revoke_checkin(
    event_no: str, conn: sqlite3.Connection = Depends(get_db)
) -> CheckinOut:
    return service.revoke_checkin(conn, event_no)


@app.get("/api/checkins", response_model=list[CheckinOut], summary="打卡列表")
def list_checkins(
    runner_id: Optional[str] = None, conn: sqlite3.Connection = Depends(get_db)
) -> list[CheckinOut]:
    return service.list_checkins(conn, runner_id)


@app.get(
    "/api/checkins/{event_no}",
    response_model=CheckinOut,
    summary="按事件号查询打卡",
)
def get_checkin(
    event_no: str, conn: sqlite3.Connection = Depends(get_db)
) -> CheckinOut:
    return service.get_checkin(conn, event_no)


@app.get(
    "/api/runners/{runner_id}/splits",
    response_model=SplitsOut,
    summary="查询选手分段结果",
)
def get_splits(
    runner_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> SplitsOut:
    return service.get_splits(conn, runner_id)


@app.post(
    "/api/publications",
    response_model=PublicationOut,
    status_code=201,
    summary="发布成绩（整版快照，版本号递增）",
)
def post_publication(
    conn: sqlite3.Connection = Depends(get_db)
) -> PublicationOut:
    return service.publish_results(conn)


@app.get(
    "/api/publications/latest",
    response_model=PublicationOut,
    summary="查询最新发布版本；尚未发布时返回 404",
)
def get_latest_publication(
    conn: sqlite3.Connection = Depends(get_db)
) -> PublicationOut:
    return service.get_latest_publication(conn)


@app.get(
    "/api/publications/{version}",
    response_model=PublicationOut,
    summary="查询指定版本的完整成绩；版本不存在返回 404",
)
def get_publication(
    version: int, conn: sqlite3.Connection = Depends(get_db)
) -> PublicationOut:
    return service.get_publication(conn, version)
