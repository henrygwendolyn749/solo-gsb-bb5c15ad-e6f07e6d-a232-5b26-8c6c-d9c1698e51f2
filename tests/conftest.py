"""pytest 共享夹具：每个用例使用独立的临时数据库。"""
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
