"""接口与分段逻辑测试。每个用例使用独立的临时数据库。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("STATION_COUNT", "6")
    with TestClient(app) as c:  # 触发 lifespan：建表 + 写入种子数据
        yield c


def post(client, event_no, runner, station, occurred_at, corrects=None):
    body = {
        "event_no": event_no,
        "runner_id": runner,
        "station_seq": station,
        "occurred_at": occurred_at,
    }
    if corrects is not None:
        body["corrects"] = corrects
    return client.post("/api/checkins", json=body)


def preview(client, event_no, runner, station, occurred_at, corrects=None):
    body = {
        "event_no": event_no,
        "runner_id": runner,
        "station_seq": station,
        "occurred_at": occurred_at,
    }
    if corrects is not None:
        body["corrects"] = corrects
    return client.post("/api/checkins/preview-correction", json=body)


def make_item(event_no, runner, station, occurred_at, corrects=None):
    body = {
        "event_no": event_no,
        "runner_id": runner,
        "station_seq": station,
        "occurred_at": occurred_at,
    }
    if corrects is not None:
        body["corrects"] = corrects
    return body


def batch(client, items):
    return client.post("/api/checkins/batch", json={"checkins": items})


def segment(splits, frm, to):
    return next(
        s for s in splits["segments"]
        if s["from_station"] == frm and s["to_station"] == to
    )


# ---------- 种子数据场景 ----------

def test_seed_complete_runner(client):
    r = client.get("/api/runners/R001/splits")
    assert r.status_code == 200
    data = r.json()
    assert data["complete"] is True
    assert len(data["segments"]) == 5
    assert all(s["status"] == "ok" for s in data["segments"])
    # 08:00 -> 11:30 = 3.5 小时
    assert data["total_seconds"] == 3 * 3600 + 30 * 60
    assert data["total"] == "3:30:00"
    assert segment(data, 1, 2)["duration_seconds"] == 32 * 60


def test_seed_out_of_order_backfill(client):
    # R002 的打卡是乱序写入的，但按站点顺序重算后分段完整
    data = client.get("/api/runners/R002/splits").json()
    assert data["complete"] is True
    assert segment(data, 1, 2)["duration_seconds"] == 40 * 60
    assert segment(data, 2, 3)["duration_seconds"] == 30 * 60


def test_seed_missing_station_pending(client):
    data = client.get("/api/runners/R003/splits").json()
    assert data["complete"] is False
    assert data["total_seconds"] is None
    s34 = segment(data, 3, 4)
    s45 = segment(data, 4, 5)
    assert s34["status"] == "pending" and "站点 4" in s34["reason"]
    assert s45["status"] == "pending" and "站点 4" in s45["reason"]
    assert segment(data, 2, 3)["status"] == "ok"


def test_seed_correction_applied(client):
    # R004 第 2 站的错误打卡 12:30 已被更正为 08:40
    data = client.get("/api/runners/R004/splits").json()
    assert data["complete"] is True
    assert segment(data, 1, 2)["duration_seconds"] == 38 * 60
    origin = client.get("/api/checkins/S-R004-2X").json()
    assert origin["status"] == "superseded"


def test_seed_revoked_checkin(client):
    data = client.get("/api/runners/R005/splits").json()
    assert segment(data, 1, 2)["status"] == "pending"
    assert segment(data, 2, 3)["status"] == "pending"
    assert client.get("/api/checkins/S-R005-2").json()["status"] == "revoked"


def test_seed_time_regression_pending(client):
    data = client.get("/api/runners/R006/splits").json()
    s23 = segment(data, 2, 3)
    assert s23["status"] == "pending"
    assert "时间倒退" in s23["reason"]


# ---------- 交互场景 ----------

def test_out_of_order_backfill_fills_gap(client):
    assert post(client, "T-1", "T001", 1, "2026-09-25T08:00:00").status_code == 201
    assert post(client, "T-3", "T001", 3, "2026-09-25T09:00:00").status_code == 201
    data = client.get("/api/runners/T001/splits").json()
    assert segment(data, 1, 2)["status"] == "pending"
    assert segment(data, 2, 3)["status"] == "pending"
    # 乱序补传第 2 站后，缺段被填补
    assert post(client, "T-2", "T001", 2, "2026-09-25T08:30:00").status_code == 201
    data = client.get("/api/runners/T001/splits").json()
    assert segment(data, 1, 2)["duration_seconds"] == 30 * 60
    assert segment(data, 2, 3)["duration_seconds"] == 30 * 60


def test_same_station_takes_earliest(client):
    post(client, "D-1", "T002", 1, "2026-09-25T08:00:00")
    post(client, "D-2a", "T002", 2, "2026-09-25T09:00:00")
    post(client, "D-2b", "T002", 2, "2026-09-25T08:40:00")  # 更早的同站打卡
    data = client.get("/api/runners/T002/splits").json()
    assert segment(data, 1, 2)["duration_seconds"] == 40 * 60
    st2 = next(s for s in data["stations"] if s["station_seq"] == 2)
    assert st2["event_no"] == "D-2b"


def test_correction_updates_results(client):
    post(client, "C-1", "T003", 1, "2026-09-25T08:00:00")
    post(client, "C-2X", "T003", 2, "2026-09-25T12:00:00")  # 错误时间
    post(client, "C-3", "T003", 3, "2026-09-25T09:00:00")
    data = client.get("/api/runners/T003/splits").json()
    assert segment(data, 2, 3)["status"] == "pending"  # 时间倒退
    # 更正第 2 站后，受影响分段恢复
    r = post(client, "C-2", "T003", 2, "2026-09-25T08:30:00", corrects="C-2X")
    assert r.status_code == 201
    data = client.get("/api/runners/T003/splits").json()
    assert segment(data, 1, 2)["duration_seconds"] == 30 * 60
    assert segment(data, 2, 3)["duration_seconds"] == 30 * 60
    assert client.get("/api/checkins/C-2X").json()["status"] == "superseded"


def test_revoke_updates_results(client):
    post(client, "V-1", "T004", 1, "2026-09-25T08:00:00")
    post(client, "V-2", "T004", 2, "2026-09-25T08:30:00")
    data = client.get("/api/runners/T004/splits").json()
    assert segment(data, 1, 2)["status"] == "ok"
    r = client.post("/api/checkins/V-2/revoke")
    assert r.status_code == 200
    assert r.json()["status"] == "revoked"
    data = client.get("/api/runners/T004/splits").json()
    assert segment(data, 1, 2)["status"] == "pending"
    assert "站点 2" in segment(data, 1, 2)["reason"]


# ---------- 校验与错误 ----------

def test_duplicate_event_no_conflict(client):
    assert post(client, "X-1", "T005", 1, "2026-09-25T08:00:00").status_code == 201
    r = post(client, "X-1", "T005", 1, "2026-09-25T08:05:00")
    assert r.status_code == 409


def test_corrects_must_target_active_event(client):
    post(client, "Y-1", "T006", 1, "2026-09-25T08:00:00")
    client.post("/api/checkins/Y-1/revoke")
    assert post(client, "Y-2", "T006", 1, "2026-09-25T08:01:00", corrects="Y-1").status_code == 409
    assert post(client, "Y-3", "T006", 1, "2026-09-25T08:01:00", corrects="NOPE").status_code == 404


def test_revoke_twice_conflict(client):
    post(client, "Z-1", "T007", 1, "2026-09-25T08:00:00")
    assert client.post("/api/checkins/Z-1/revoke").status_code == 200
    assert client.post("/api/checkins/Z-1/revoke").status_code == 409
    assert client.post("/api/checkins/NOPE/revoke").status_code == 404


def test_station_out_of_range(client):
    assert post(client, "W-1", "T008", 7, "2026-09-25T08:00:00").status_code == 422
    assert post(client, "W-2", "T008", 0, "2026-09-25T08:00:00").status_code == 422


def test_unknown_runner_404(client):
    assert client.get("/api/runners/NOBODY/splits").status_code == 404


def test_timezone_offsets_normalized(client):
    post(client, "TZ-1", "T009", 1, "2026-09-25T16:00:00+08:00")
    post(client, "TZ-2", "T009", 2, "2026-09-25T08:40:00+00:00")
    data = client.get("/api/runners/T009/splits").json()
    assert segment(data, 1, 2)["duration_seconds"] == 40 * 60


# ---------- 更正预演 ----------

def test_preview_correction_returns_before_after_and_changes(client):
    post(client, "P-1", "TP1", 1, "2026-09-25T08:00:00")
    post(client, "P-2X", "TP1", 2, "2026-09-25T12:00:00")  # 错误时间，导致 2→3 时间倒退
    post(client, "P-3", "TP1", 3, "2026-09-25T09:00:00")
    post(client, "P-4", "TP1", 4, "2026-09-25T09:40:00")
    post(client, "P-5", "TP1", 5, "2026-09-25T10:20:00")
    post(client, "P-6", "TP1", 6, "2026-09-25T11:00:00")

    r = preview(client, "P-2", "TP1", 2, "2026-09-25T08:30:00", corrects="P-2X")
    assert r.status_code == 200
    data = r.json()
    assert data["event_no"] == "P-2"
    assert data["corrects"] == "P-2X"
    assert [p["runner_id"] for p in data["affected_runners"]] == ["TP1"]

    runner = data["affected_runners"][0]
    assert runner["before"]["complete"] is False
    assert runner["before"]["total_seconds"] is None
    assert runner["after"]["complete"] is True
    assert runner["after"]["total_seconds"] == 3 * 3600
    assert runner["after"]["total"] == "3:00:00"

    # 仅列出发生变化的分段：1→2 与 2→3
    changed = {(c["from_station"], c["to_station"]): c for c in runner["changed_segments"]}
    assert set(changed) == {(1, 2), (2, 3)}
    assert changed[(1, 2)]["before"]["duration_seconds"] == 4 * 3600
    assert changed[(1, 2)]["after"]["duration_seconds"] == 30 * 60
    assert changed[(2, 3)]["before"]["status"] == "pending"
    assert "时间倒退" in changed[(2, 3)]["before"]["reason"]
    assert changed[(2, 3)]["after"]["status"] == "ok"
    assert changed[(2, 3)]["after"]["duration_seconds"] == 30 * 60


def test_preview_rolls_back_and_same_request_can_be_submitted(client):
    post(client, "Q-1", "TP2", 1, "2026-09-25T08:00:00")
    post(client, "Q-2X", "TP2", 2, "2026-09-25T12:00:00")
    before_splits = client.get("/api/runners/TP2/splits").json()

    r = preview(client, "Q-2", "TP2", 2, "2026-09-25T08:30:00", corrects="Q-2X")
    assert r.status_code == 200

    # 预演整体回滚：事件号未被占用，原事件仍为 active，分段结果不变
    assert client.get("/api/checkins/Q-2").status_code == 404
    assert client.get("/api/checkins/Q-2X").json()["status"] == "active"
    assert client.get("/api/runners/TP2/splits").json() == before_splits

    # 相同请求随后可正式提交，且结果与预演的 after 一致
    assert post(client, "Q-2", "TP2", 2, "2026-09-25T08:30:00", corrects="Q-2X").status_code == 201
    assert client.get("/api/checkins/Q-2X").json()["status"] == "superseded"
    splits = client.get("/api/runners/TP2/splits").json()
    after = r.json()["affected_runners"][0]["after"]
    assert splits["complete"] == after["complete"]
    assert splits["total_seconds"] == after["total_seconds"]


def test_preview_cross_runner_covers_both_runners(client):
    # TR1 的第 2 站打卡实际属于 TR2：更正跨选手，预演同时覆盖两人
    post(client, "X-1", "TR1", 1, "2026-09-25T08:00:00")
    post(client, "X-2", "TR1", 2, "2026-09-25T08:40:00")
    post(client, "X-3", "TR1", 3, "2026-09-25T09:20:00")
    post(client, "X-4", "TR1", 4, "2026-09-25T10:00:00")
    post(client, "X-5", "TR1", 5, "2026-09-25T10:40:00")
    post(client, "X-6", "TR1", 6, "2026-09-25T11:20:00")
    post(client, "Y-1", "TR2", 1, "2026-09-25T08:05:00")
    post(client, "Y-3", "TR2", 3, "2026-09-25T09:25:00")
    post(client, "Y-4", "TR2", 4, "2026-09-25T10:05:00")
    post(client, "Y-5", "TR2", 5, "2026-09-25T10:45:00")
    post(client, "Y-6", "TR2", 6, "2026-09-25T11:25:00")

    r = preview(client, "Y-2", "TR2", 2, "2026-09-25T08:40:00", corrects="X-2")
    assert r.status_code == 200
    runners = {p["runner_id"]: p for p in r.json()["affected_runners"]}
    assert set(runners) == {"TR1", "TR2"}

    # 原选手 TR1 失去第 2 站：1→2、2→3 变为待定
    tr1 = runners["TR1"]
    assert tr1["before"]["complete"] is True
    assert tr1["after"]["complete"] is False
    assert tr1["after"]["total_seconds"] is None
    changed1 = {(c["from_station"], c["to_station"]) for c in tr1["changed_segments"]}
    assert changed1 == {(1, 2), (2, 3)}

    # 新选手 TR2 获得第 2 站：1→2、2→3 由待定变为有效
    tr2 = runners["TR2"]
    assert tr2["before"]["complete"] is False
    assert tr2["after"]["complete"] is True
    assert tr2["after"]["total_seconds"] == 3 * 3600 + 20 * 60
    changed2 = {(c["from_station"], c["to_station"]): c for c in tr2["changed_segments"]}
    assert set(changed2) == {(1, 2), (2, 3)}
    assert changed2[(1, 2)]["after"]["duration_seconds"] == 35 * 60
    assert changed2[(2, 3)]["after"]["duration_seconds"] == 45 * 60

    # 预演无副作用：两名选手的成绩与打卡状态均未改变
    assert client.get("/api/checkins/X-2").json()["status"] == "active"
    assert client.get("/api/checkins/Y-2").status_code == 404
    assert client.get("/api/runners/TR1/splits").json()["complete"] is True


def test_preview_requires_corrects(client):
    post(client, "M-1", "TP3", 1, "2026-09-25T08:00:00")
    r = preview(client, "M-2", "TP3", 1, "2026-09-25T08:05:00")  # 缺 corrects
    assert r.status_code == 422


def test_preview_reuses_formal_validation(client):
    post(client, "N-1", "TP4", 1, "2026-09-25T08:00:00")
    # 站点超范围
    assert preview(client, "N-2", "TP4", 7, "2026-09-25T08:05:00", corrects="N-1").status_code == 422
    # 事件号已存在
    assert preview(client, "N-1", "TP4", 1, "2026-09-25T08:05:00", corrects="N-1").status_code == 409
    # 被更正事件不存在
    assert preview(client, "N-3", "TP4", 1, "2026-09-25T08:05:00", corrects="NOPE").status_code == 404
    # 被更正事件已撤销
    client.post("/api/checkins/N-1/revoke")
    assert preview(client, "N-4", "TP4", 1, "2026-09-25T08:05:00", corrects="N-1").status_code == 409
    # 校验失败同样不落库
    assert client.get("/api/checkins/N-2").status_code == 404
    assert client.get("/api/checkins/N-4").status_code == 404


# ---------- 批量打卡 ----------

def test_batch_success_returns_event_nos_and_runner_results(client):
    items = [
        make_item("B-1", "TB1", 1, "2026-09-25T08:00:00"),
        make_item("B-2", "TB1", 2, "2026-09-25T08:30:00"),
        make_item("B-3", "TB1", 3, "2026-09-25T09:00:00"),
        make_item("B-4", "TB1", 4, "2026-09-25T09:30:00"),
        make_item("B-5", "TB1", 5, "2026-09-25T10:00:00"),
        make_item("B-6", "TB1", 6, "2026-09-25T11:00:00"),
    ]
    r = batch(client, items)
    assert r.status_code == 201
    data = r.json()
    # 各条事件号按提交顺序返回
    assert [it["event_no"] for it in data["results"]] == [f"B-{k}" for k in range(1, 7)]
    assert [it["index"] for it in data["results"]] == list(range(6))
    # 受影响选手的最终完成状态与总用时
    assert data["affected_runners"] == [
        {"runner_id": "TB1", "complete": True, "total_seconds": 3 * 3600, "total": "3:00:00"}
    ]
    # 与分段查询结果一致
    splits = client.get("/api/runners/TB1/splits").json()
    assert splits["complete"] is True and splits["total_seconds"] == 3 * 3600


def test_batch_in_batch_correction(client):
    # 批内后续记录更正本批前面刚写入的事件
    items = [
        make_item("BC-1", "TB2", 1, "2026-09-25T08:00:00"),
        make_item("BC-2X", "TB2", 2, "2026-09-25T12:00:00"),  # 错误时间
        make_item("BC-3", "TB2", 3, "2026-09-25T09:00:00"),
        make_item("BC-2", "TB2", 2, "2026-09-25T08:30:00", corrects="BC-2X"),
    ]
    r = batch(client, items)
    assert r.status_code == 201
    assert [it["event_no"] for it in r.json()["results"]] == ["BC-1", "BC-2X", "BC-3", "BC-2"]
    # 原事件被取代，最终结果按更正后计算
    assert client.get("/api/checkins/BC-2X").json()["status"] == "superseded"
    assert client.get("/api/checkins/BC-2").json()["status"] == "active"
    data = client.get("/api/runners/TB2/splits").json()
    assert segment(data, 1, 2)["duration_seconds"] == 30 * 60
    assert segment(data, 2, 3)["duration_seconds"] == 30 * 60


def test_batch_corrects_existing_event(client):
    post(client, "BE-1", "TB3", 1, "2026-09-25T08:00:00")
    r = batch(client, [make_item("BE-2", "TB3", 1, "2026-09-25T08:05:00", corrects="BE-1")])
    assert r.status_code == 201
    assert client.get("/api/checkins/BE-1").json()["status"] == "superseded"


def test_batch_cross_runner_correction_lists_both_runners(client):
    # 批内更正跨选手：BR1 的第 2 站实际属于 BR2，受影响选手包含两人
    items = [
        make_item("BX-1", "BR1", 1, "2026-09-25T08:00:00"),
        make_item("BX-2", "BR1", 2, "2026-09-25T08:40:00"),
        make_item("BY-1", "BR2", 1, "2026-09-25T08:05:00"),
        make_item("BY-2", "BR2", 2, "2026-09-25T08:40:00", corrects="BX-2"),
    ]
    r = batch(client, items)
    assert r.status_code == 201
    runners = {p["runner_id"]: p for p in r.json()["affected_runners"]}
    assert set(runners) == {"BR1", "BR2"}
    assert runners["BR1"]["complete"] is False
    assert runners["BR1"]["total_seconds"] is None
    assert runners["BR2"]["complete"] is False  # 仍缺后续站点
    data = client.get("/api/runners/BR2/splits").json()
    assert segment(data, 1, 2)["duration_seconds"] == 35 * 60


def test_batch_forward_reference_rejected_and_rolled_back(client):
    items = [
        make_item("BF-1", "TB4", 1, "2026-09-25T08:00:00", corrects="BF-2"),  # 前向引用
        make_item("BF-2", "TB4", 2, "2026-09-25T08:30:00"),
    ]
    r = batch(client, items)
    assert r.status_code == 422
    failure = r.json()["detail"]["failures"][0]
    assert failure["index"] == 0
    assert failure["event_no"] == "BF-1"
    assert "前向引用" in failure["reason"]
    # 整批回滚：两条记录均未写入
    assert client.get("/api/checkins/BF-1").status_code == 404
    assert client.get("/api/checkins/BF-2").status_code == 404


def test_batch_self_correction_rejected(client):
    r = batch(client, [make_item("BS-1", "TB5", 1, "2026-09-25T08:00:00", corrects="BS-1")])
    assert r.status_code == 422
    failure = r.json()["detail"]["failures"][0]
    assert failure["index"] == 0
    assert "自身" in failure["reason"]
    assert client.get("/api/checkins/BS-1").status_code == 404


def test_batch_duplicate_event_no_in_batch(client):
    items = [
        make_item("BD-1", "TB6", 1, "2026-09-25T08:00:00"),
        make_item("BD-2", "TB6", 2, "2026-09-25T08:30:00"),
        make_item("BD-1", "TB6", 3, "2026-09-25T09:00:00"),  # 与本批下标 0 重复
    ]
    r = batch(client, items)
    assert r.status_code == 409
    failure = r.json()["detail"]["failures"][0]
    assert failure["index"] == 2
    assert failure["event_no"] == "BD-1"
    assert "重复" in failure["reason"]
    # 整批回滚：前两条也不落库
    assert client.get("/api/checkins/BD-1").status_code == 404
    assert client.get("/api/checkins/BD-2").status_code == 404


def test_batch_duplicate_with_existing_event(client):
    post(client, "BK-1", "TB7", 1, "2026-09-25T08:00:00")
    items = [
        make_item("BK-2", "TB7", 2, "2026-09-25T08:30:00"),
        make_item("BK-1", "TB7", 3, "2026-09-25T09:00:00"),  # 与库中已有事件重复
    ]
    r = batch(client, items)
    assert r.status_code == 409
    assert r.json()["detail"]["failures"][0]["index"] == 1
    assert client.get("/api/checkins/BK-2").status_code == 404  # 已回滚
    assert client.get("/api/checkins/BK-1").json()["status"] == "active"


def test_batch_invalid_correction_target(client):
    post(client, "BV-1", "TB8", 1, "2026-09-25T08:00:00")
    client.post("/api/checkins/BV-1/revoke")
    # 被更正事件不存在
    r = batch(client, [make_item("BV-2", "TB8", 1, "2026-09-25T08:05:00", corrects="NOPE")])
    assert r.status_code == 404
    assert r.json()["detail"]["failures"][0]["index"] == 0
    # 被更正事件已撤销
    r = batch(client, [make_item("BV-3", "TB8", 1, "2026-09-25T08:05:00", corrects="BV-1")])
    assert r.status_code == 409
    assert r.json()["detail"]["failures"][0]["index"] == 0
    assert client.get("/api/checkins/BV-2").status_code == 404
    assert client.get("/api/checkins/BV-3").status_code == 404


def test_batch_failure_rolls_back_status_changes(client):
    # 第一条是合法更正（改变已有事件状态），第二条失败：状态变更也必须回滚
    post(client, "BR-1", "TB9", 1, "2026-09-25T08:00:00")
    items = [
        make_item("BR-2", "TB9", 1, "2026-09-25T08:05:00", corrects="BR-1"),
        make_item("BR-3", "TB9", 7, "2026-09-25T09:00:00"),  # 站点超范围
    ]
    r = batch(client, items)
    assert r.status_code == 422
    failure = r.json()["detail"]["failures"][0]
    assert failure["index"] == 1
    assert failure["event_no"] == "BR-3"
    # 新增记录与状态变更全部回滚
    assert client.get("/api/checkins/BR-2").status_code == 404
    assert client.get("/api/checkins/BR-1").json()["status"] == "active"


def test_batch_size_limits(client):
    assert batch(client, []).status_code == 422
    items = [
        make_item(f"BL-{k}", "TB10", 1, "2026-09-25T08:00:00") for k in range(101)
    ]
    assert batch(client, items).status_code == 422
    # 恰好 100 条可以受理
    items = [
        make_item(f"BL-{k}", "TB10", 1, f"2026-09-25T08:{k // 60:02d}:{k % 60:02d}")
        for k in range(100)
    ]
    r = batch(client, items)
    assert r.status_code == 201
    assert len(r.json()["results"]) == 100


def test_batch_incomplete_runner_result(client):
    # 缺站时受影响选手 complete=false，总用时为空
    items = [
        make_item("BI-1", "TB11", 1, "2026-09-25T08:00:00"),
        make_item("BI-2", "TB11", 2, "2026-09-25T08:30:00"),
    ]
    r = batch(client, items)
    assert r.status_code == 201
    assert r.json()["affected_runners"] == [
        {"runner_id": "TB11", "complete": False, "total_seconds": None, "total": None}
    ]
