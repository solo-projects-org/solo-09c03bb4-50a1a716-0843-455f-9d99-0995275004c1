"""源地址绑定保护的端到端测试。

覆盖：
- 新库首次成功（含无匹配）在同一事务内绑定源地址，失败不绑定
- 已绑定后换地址 -> 409，不访问源、不写运行历史；改回原地址照常续采
- 旧库有数据但未绑定 -> 409；CONFIRMED_OAI_ENDPOINT 与当前地址相同才放行
"""
from __future__ import annotations

import pytest


def _binding(api):
    return api.get("/api/v1/source-binding").json()


def _harvest(api, payload, expected=200):
    resp = api.post("/api/v1/harvest", json=payload)
    assert resp.status_code == expected, resp.text
    return resp


def test_new_db_first_success_binds_endpoint_in_same_commit(client):
    api, db_mod, _oai = client
    from app import main as main_mod

    endpoint = main_mod.OAI_ENDPOINT
    assert _binding(api)["bound_endpoint"] is None

    result = _harvest(api, {"from": "2026-10-01", "until": "2026-10-15"}).json()
    assert result["source_endpoint"] == endpoint.rstrip("/")

    binding = _binding(api)
    assert binding["bound_endpoint"] == endpoint.rstrip("/")
    assert binding["current_endpoint"] == endpoint.rstrip("/")
    assert binding["has_local_data"] is True
    assert binding["bound_at"]

    # 绑定、书目、进度在同一事务落库
    conn = db_mod.get_connection()
    try:
        assert db_mod.get_bound_endpoint(conn) == endpoint.rstrip("/")
        assert db_mod.get_last_until(conn) == "2026-10-15"
        assert conn.execute("SELECT COUNT(*) FROM bib_records").fetchone()[0] == 15
    finally:
        conn.close()


def test_no_match_window_also_binds_new_db(client):
    api, db_mod, _oai = client
    from app import main as main_mod

    _harvest(api, {"from": "2027-01-01", "until": "2027-01-31"})

    conn = db_mod.get_connection()
    try:
        # 无匹配也要绑定
        assert db_mod.get_bound_endpoint(conn) == main_mod.OAI_ENDPOINT.rstrip("/")
        assert db_mod.get_last_until(conn) == "2027-01-31"
        assert conn.execute("SELECT COUNT(*) FROM bib_records").fetchone()[0] == 0
    finally:
        conn.close()


def test_failed_harvest_does_not_bind(client, monkeypatch):
    api, db_mod, oai_mod = client

    real_consume = oai_mod.TOKENS.consume

    def always_invalid(token):
        real_consume(token)
        return None

    monkeypatch.setattr(oai_mod.TOKENS, "consume", always_invalid)

    resp = _harvest(
        api, {"from": "2026-10-01", "until": "2026-10-27"}, expected=502
    )
    assert resp.json()["detail"]["oai_error"] == "badResumptionToken"

    conn = db_mod.get_connection()
    try:
        # 失败不绑定、不推进、不进半批书目
        assert db_mod.get_bound_endpoint(conn) is None
        assert db_mod.get_last_until(conn) is None
        assert conn.execute("SELECT COUNT(*) FROM bib_records").fetchone()[0] == 0
    finally:
        conn.close()


def test_bound_db_rejects_different_endpoint_with_409(client, monkeypatch):
    api, db_mod, _oai = client
    from app import main as main_mod

    _harvest(api, {"from": "2026-10-01", "until": "2026-10-15"})
    runs_before = len(api.get("/api/v1/runs").json())

    # 模拟重部署：保留数据卷，OAI_ENDPOINT 误指演练源
    drill = "http://drill.example.test:8080/oai"
    monkeypatch.setattr(main_mod, "OAI_ENDPOINT", drill)

    resp = _harvest(api, {"until": "2026-10-27"}, expected=409)
    detail = resp.json()["detail"]
    assert drill in detail
    assert "已绑定" in detail

    # 不访问源、不写运行历史
    assert len(api.get("/api/v1/runs").json()) == runs_before
    conn = db_mod.get_connection()
    try:
        assert db_mod.get_last_until(conn) == "2026-10-15"  # 进度原样保留
        assert conn.execute("SELECT COUNT(*) FROM bib_records").fetchone()[0] == 15
    finally:
        conn.close()

    # 原目录与进度仍可查询
    assert len(api.get("/api/v1/catalog?status=all").json()) == 15
    assert api.get("/api/v1/progress").json()["last_until"] == "2026-10-15"


def test_switch_back_to_bound_endpoint_resumes_normally(client, monkeypatch):
    api, _db, _oai = client
    from app import main as main_mod

    original = main_mod.OAI_ENDPOINT
    _harvest(api, {"from": "2026-10-01", "until": "2026-10-15"})

    monkeypatch.setattr(main_mod, "OAI_ENDPOINT", "http://drill.example.test/oai")
    _harvest(api, {"until": "2026-10-27"}, expected=409)

    # 改回原地址后照常续采（from 留空取上次截止日，含当天）
    monkeypatch.setattr(main_mod, "OAI_ENDPOINT", original)
    result = _harvest(api, {"until": "2026-10-27"}).json()
    assert result["from_date"] == "2026-10-15"
    assert result["fetched"] == 13
    assert api.get("/api/v1/progress").json()["last_until"] == "2026-10-27"


def test_legacy_unbound_db_rejects_without_confirmed_endpoint(
    client, monkeypatch
):
    api, db_mod, _oai = client
    from app import main as main_mod

    # 手工造一个“旧库”：有书目和进度，但没有绑定记录
    conn = db_mod.get_connection()
    try:
        db_mod.upsert_bib_record(
            conn,
            identifier="oai:legacy.lib:rec-001",
            datestamp="2026-09-01",
            fields={"title": "遗留书目"},
            raw_dc="{}",
        )
        db_mod.advance_progress(conn, "2026-09-30")
        conn.commit()
    finally:
        conn.close()

    monkeypatch.delenv("CONFIRMED_OAI_ENDPOINT", raising=False)
    resp = _harvest(api, {"until": "2026-10-27"}, expected=409)
    assert "尚未绑定" in resp.json()["detail"]
    assert "CONFIRMED_OAI_ENDPOINT" in resp.json()["detail"]

    # 仍然不写运行历史
    assert api.get("/api/v1/runs").json() == []
    assert _binding(api)["has_local_data"] is True


def test_legacy_unbound_db_confirmed_endpoint_must_match(client, monkeypatch):
    api, db_mod, _oai = client
    from app import binding, main as main_mod

    conn = db_mod.get_connection()
    try:
        db_mod.advance_progress(conn, "2026-09-30")
        conn.commit()
    finally:
        conn.close()

    current = main_mod.OAI_ENDPOINT

    # 核实地址与当前地址不符 -> 409
    monkeypatch.setenv("CONFIRMED_OAI_ENDPOINT", "http://other.example.test/oai")
    resp = _harvest(api, {"until": "2026-10-27"}, expected=409)
    detail = resp.json()["detail"]
    assert "不符" in detail
    assert binding.get_confirmed_endpoint() == "http://other.example.test/oai"
    assert db_mod.get_bound_endpoint(db_mod.get_connection()) is None

    # 核实地址与当前地址相同 -> 放行，成功事务内补绑定
    monkeypatch.setenv("CONFIRMED_OAI_ENDPOINT", current)
    result = _harvest(api, {"until": "2026-10-27"}).json()
    assert result["status"] == "success"

    conn = db_mod.get_connection()
    try:
        assert db_mod.get_bound_endpoint(conn) == current.rstrip("/")
    finally:
        conn.close()

    # 补绑定后再换地址仍被拒
    monkeypatch.setattr(main_mod, "OAI_ENDPOINT", "http://drill.example.test/oai")
    _harvest(api, {"until": "2026-10-28"}, expected=409)


def test_409_happens_before_any_source_access(client, monkeypatch):
    api, _db, _oai = client
    from app import main as main_mod

    _harvest(api, {"from": "2026-10-01", "until": "2026-10-15"})

    # 指向一个不可路由/无效的地址：若准入生效则根本不会发起请求
    monkeypatch.setattr(main_mod, "OAI_ENDPOINT", "http://127.0.0.1:1/oai")
    resp = _harvest(api, {"until": "2026-10-27"}, expected=409)
    assert resp.json()["detail"]


def test_trailing_slash_endpoints_treated_as_equal(client, monkeypatch):
    api, _db, _oai = client
    from app import main as main_mod

    original = main_mod.OAI_ENDPOINT
    _harvest(api, {"from": "2026-10-01", "until": "2026-10-15"})

    # 仅末尾斜杠差异不应触发 409
    monkeypatch.setattr(main_mod, "OAI_ENDPOINT", original + "/")
    result = _harvest(api, {"until": "2026-10-27"}).json()
    assert result["source_endpoint"] == original.rstrip("/")
    assert _binding(api)["bound_endpoint"] == original.rstrip("/")


@pytest.mark.parametrize("payload", [
    {"until": "2026-13-99"},
    {"from": "2026-10-20", "until": "2026-10-01"},
])
def test_date_validation_still_works(client, payload):
    api, _db, _oai = client
    assert api.post("/api/v1/harvest", json=payload).status_code == 422
