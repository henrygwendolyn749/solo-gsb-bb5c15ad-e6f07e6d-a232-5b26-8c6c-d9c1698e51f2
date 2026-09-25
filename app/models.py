"""请求 / 响应模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class CheckinCreate(BaseModel):
    event_no: str = Field(..., min_length=1, max_length=64, description="唯一事件号")
    runner_id: str = Field(..., min_length=1, max_length=64, description="选手号")
    station_seq: int = Field(..., ge=1, description="站点序号（从 1 开始）")
    occurred_at: datetime = Field(..., description="打卡发生时间，ISO 8601")
    corrects: Optional[str] = Field(
        default=None, description="要更正的原事件号；填写后原事件被本事件取代"
    )


class CorrectionPreview(CheckinCreate):
    """更正预演请求：字段与打卡一致，但 corrects 必填且须指向有效事件。"""

    corrects: str = Field(
        ..., min_length=1, max_length=64, description="要更正的原事件号（必填，须为有效事件）"
    )


class CheckinOut(BaseModel):
    event_no: str
    runner_id: str
    station_seq: int
    occurred_at: datetime
    status: str = Field(..., description="active=有效，superseded=已被更正，revoked=已撤销")
    corrects: Optional[str]
    created_at: datetime


class StationTime(BaseModel):
    station_seq: int
    event_no: str
    occurred_at: datetime


class Segment(BaseModel):
    from_station: int
    to_station: int
    status: Literal["ok", "pending"]
    duration_seconds: Optional[int] = Field(default=None, description="分段用时（秒）")
    duration: Optional[str] = Field(default=None, description="分段用时，H:MM:SS")
    reason: Optional[str] = Field(default=None, description="待定原因")


class SplitsOut(BaseModel):
    runner_id: str
    station_count: int
    complete: bool = Field(..., description="所有分段均有效时为 true")
    total_seconds: Optional[int] = Field(default=None, description="总用时（秒），仅 complete 时给出")
    total: Optional[str] = Field(default=None, description="总用时，H:MM:SS")
    stations: list[StationTime] = Field(..., description="各站点采用的有效打卡（同站取最早）")
    segments: list[Segment]


class RunnerSummary(BaseModel):
    """某一时点上选手的完成状态与总用时。"""

    complete: bool = Field(..., description="所有分段均有效时为 true")
    total_seconds: Optional[int] = Field(default=None, description="总用时（秒），仅 complete 时给出")
    total: Optional[str] = Field(default=None, description="总用时，H:MM:SS")


class BatchCheckinCreate(BaseModel):
    """批量打卡请求：1~100 条，按顺序在同一事务内处理。"""

    checkins: list[CheckinCreate] = Field(
        ...,
        min_length=1,
        max_length=100,
        description="按顺序处理的打卡列表（1~100 条）；后续记录可用 corrects "
        "更正本批前面刚写入且仍有效的事件",
    )


class BatchItemOut(BaseModel):
    """批量打卡中单条记录的受理结果。"""

    index: int = Field(..., description="在请求数组中的下标（从 0 开始）")
    event_no: str = Field(..., description="该条打卡的事件号")


class BatchRunnerResult(RunnerSummary):
    """批量提交后某受影响选手的最终完成状态与总用时。"""

    runner_id: str


class BatchCheckinOut(BaseModel):
    """批量打卡响应：仅在整批全部成功时返回。"""

    results: list[BatchItemOut] = Field(..., description="各条打卡的事件号（按提交顺序）")
    affected_runners: list[BatchRunnerResult] = Field(
        ...,
        description="受影响选手（含跨选手更正的原选手）按现有分段规则计算的最终完成状态与总用时",
    )


class SegmentChange(BaseModel):
    """同一分段在更正前后的对照。"""

    from_station: int
    to_station: int
    before: Segment
    after: Segment


class RunnerPreview(BaseModel):
    """单个受影响选手的更正预演结果。"""

    runner_id: str
    before: RunnerSummary = Field(..., description="更正前的完成状态与总用时")
    after: RunnerSummary = Field(..., description="更正后的完成状态与总用时")
    changed_segments: list[SegmentChange] = Field(..., description="仅列出发生变化的分段")


class PreviewCorrectionOut(BaseModel):
    """更正预演响应：整体回滚，不产生任何副作用。"""

    event_no: str
    runner_id: str
    station_seq: int
    occurred_at: datetime
    corrects: str
    affected_runners: list[RunnerPreview] = Field(
        ..., description="受影响选手；更正跨选手时包含原选手与新选手"
    )


class PublicationOut(BaseModel):
    """一个已发布版本的完整成绩快照。"""

    version: int = Field(..., description="发布版本号，从 1 单调递增")
    published_at: datetime = Field(..., description="发布时间")
    station_count: int = Field(..., description="发布时的赛道站点总数")
    results: list[SplitsOut] = Field(
        ...,
        description="该版本全体有打卡记录选手的完整分段结果（按选手号排序）；"
        "未完赛选手同样保留 pending 分段",
    )
