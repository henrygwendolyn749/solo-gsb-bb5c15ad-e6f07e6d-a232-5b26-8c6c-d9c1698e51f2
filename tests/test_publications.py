"""成绩发布与版本查询测试。夹具 client 见 conftest.py。"""
from __future__ import annotations

import threading


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


def by_runner(publication):
    return {r["runner_id"]: r for r in publication["results"]}


# ---------- 发布与查询 ----------


def test_latest_404_before_any_publication(client):
    r = client.get("/api/publications/latest")
    assert r.status_code == 404


def test_get_version_404(client):
    assert client.get("/api/publications/1").status_code == 404
    assert client.get("/api/publications/999").status_code == 404


def test_publish_snapshots_all_runners_including_pending(client):
    r = client.post("/api/publications")
    assert r.status_code == 201
    data = r.json()
    assert data["version"] == 1
    assert data["station_count"] == 6
    # 种子数据中有 R001–R006 六名选手，按选手号排序
    assert [x["runner_id"] for x in data["results"]] == [
        f"R00{k}" for k in range(1, 7)
    ]
    runners = by_runner(data)
    # 完整选手
    assert runners["R001"]["complete"] is True
    assert runners["R001"]["total_seconds"] == 3 * 3600 + 30 * 60
    # 未完赛选手同样保留 pending 分段
    assert runners["R003"]["complete"] is False
    assert runners["R003"]["total_seconds"] is None
    pending = [s for s in runners["R003"]["segments"] if s["status"] == "pending"]
    assert {(s["from_station"], s["to_station"]) for s in pending} == {(3, 4), (4, 5)}
    # 快照与实时分段查询逐选手一致
    for runner_id in runners:
        live = client.get(f"/api/runners/{runner_id}/splits").json()
        assert runners[runner_id] == live


def test_get_version_and_latest_return_same_snapshot(client):
    first = client.post("/api/publications").json()
    assert first["version"] == 1

    # 发布后再打卡：R005 撤销了第 2 站，补传后实时成绩改变
    post(client, "P-R005-2", "R005", 2, "2026-09-25T08:56:00")
    second = client.post("/api/publications").json()
    assert second["version"] == 2

    # 按版本查询
    assert client.get("/api/publications/1").json() == first
    assert client.get("/api/publications/2").json() == second

    # 最新版
    latest = client.get("/api/publications/latest")
    assert latest.status_code == 200
    assert latest.json() == second

    # 第二版反映新打卡：R005 原本待定的分段 1→2、2→3 恢复有效
    v2_r005 = by_runner(second)["R005"]
    assert v2_r005["complete"] is False  # 仍缺 4/5/6 站
    segs = {(s["from_station"], s["to_station"]): s for s in v2_r005["segments"]}
    assert segs[(1, 2)]["status"] == "ok"
    assert segs[(1, 2)]["duration_seconds"] == 41 * 60
    assert segs[(2, 3)]["status"] == "ok"
    assert segs[(2, 3)]["duration_seconds"] == 44 * 60
    assert segs[(3, 4)]["status"] == "pending"


# ---------- 已发布版本不可变 ----------


def test_later_checkin_revoke_do_not_change_published_version(client):
    v1 = client.post("/api/publications").json()

    # 后续撤销一条打卡并再补传，只影响实时成绩
    assert client.post("/api/checkins/S-R001-3/revoke").status_code == 200
    post(client, "P-R001-3", "R001", 3, "2026-09-25T09:20:00")

    live = client.get("/api/runners/R001/splits").json()
    assert live["complete"] is True
    assert {s["event_no"] for s in live["stations"] if s["station_seq"] == 3} == {
        "P-R001-3"
    }

    # 已发布的第一版内容不变
    assert client.get("/api/publications/1").json() == v1
    v1_r001 = by_runner(v1)["R001"]
    assert {s["event_no"] for s in v1_r001["stations"] if s["station_seq"] == 3} == {
        "S-R001-3"
    }
    # latest 仍是第一版（未重新发布）
    assert client.get("/api/publications/latest").json() == v1


def test_later_correction_does_not_change_published_version(client):
    # 新选手先记录一个错误的第 2 站时间并发布
    post(client, "F-1", "F001", 1, "2026-09-25T08:00:00")
    post(client, "F-2X", "F001", 2, "2026-09-25T12:00:00")
    post(client, "F-3", "F001", 3, "2026-09-25T09:00:00")
    v1 = client.post("/api/publications").json()
    assert by_runner(v1)["F001"]["complete"] is False

    # 发布后更正错误打卡
    r = post(
        client, "F-2", "F001", 2, "2026-09-25T08:30:00", corrects="F-2X"
    )
    assert r.status_code == 201

    # 实时成绩已更新：1→2、2→3 恢复有效（仍缺后续站点，未完赛）
    live = client.get("/api/runners/F001/splits").json()
    assert live["complete"] is False
    segs = {(s["from_station"], s["to_station"]): s for s in live["segments"]}
    assert segs[(1, 2)]["status"] == "ok"
    assert segs[(2, 3)]["status"] == "ok"
    assert client.get("/api/publications/1").json() == v1
    v1_f001 = by_runner(v1)["F001"]
    v1_segs = {(s["from_station"], s["to_station"]): s for s in v1_f001["segments"]}
    # 发布时 1→2 按错误时间为 4 小时，2→3 时间倒退待定；快照保持不变
    assert v1_f001["complete"] is False
    assert v1_segs[(1, 2)]["duration_seconds"] == 4 * 3600
    assert v1_segs[(2, 3)]["status"] == "pending"
    assert client.get("/api/checkins/F-2X").json()["status"] == "superseded"


def test_versions_monotonic_across_many_publications(client):
    seen = []
    for _ in range(3):
        seen.append(client.post("/api/publications").json()["version"])
    assert seen == [1, 2, 3]


# ---------- 并发一致性 ----------


def test_concurrent_checkin_cannot_mix_data_into_one_version(client):
    """发布事务先取写锁：未提交的打卡会阻塞发布，提交后完整进入下一版。

    用第二个连接直接持有 checkins 的写锁（模拟一个进行中、尚未提交的
    打卡 / 更正 / 撤销事务），期间发起发布：发布必须等待写锁，而不能
    在对方事务中途读取；对方提交后发布读到的已是完整提交后的状态。
    """
    import os
    import sqlite3

    # 基线版本：不包含 CC1
    v1 = client.post("/api/publications").json()
    assert "CC1" not in by_runner(v1)

    blocker = sqlite3.connect(os.environ["DB_PATH"])
    blocker.execute("PRAGMA busy_timeout = 5000")
    blocker.isolation_level = None
    blocker.execute("BEGIN IMMEDIATE")
    blocker.execute(
        "INSERT INTO checkins"
        " (event_no, runner_id, station_seq, occurred_at, status, corrects, created_at)"
        " VALUES ('C-1', 'CC1', 1, '2026-09-25T08:00:00', 'active', NULL,"
        " '2026-09-25T07:59:59')"
    )

    done = threading.Event()
    result: dict = {}

    def publish_while_locked():
        result["response"] = client.post("/api/publications")
        done.set()

    t = threading.Thread(target=publish_while_locked)
    t.start()
    # 打卡事务未提交：发布拿不到写锁，必须阻塞等待（而非读到半截状态）
    assert not done.wait(timeout=1.0)

    blocker.execute("COMMIT")  # 打卡整体提交后，发布才继续
    blocker.close()
    assert done.wait(timeout=10)
    t.join(10)

    assert result["response"].status_code == 201
    v2 = result["response"].json()
    assert v2["version"] == v1["version"] + 1
    entry = by_runner(v2)["CC1"]
    assert len(entry["segments"]) == 5  # 完整快照，非半截数据
    assert entry["segments"][0]["from_station"] == 1
    # 旧版本不受影响
    assert client.get("/api/publications/1").json() == v1
    assert client.get("/api/publications/latest").json() == v2
