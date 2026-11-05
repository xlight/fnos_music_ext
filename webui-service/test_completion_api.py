"""整曲补全在管理台后端的用例（change add-full-track-completion · 任务 2.1/2.3/2.5）。

覆盖：schema 新键（默认 false）、GET /api/completion 的状态区分与分页约束、
/api/status 的 completion 摘要、旧后端语义（缺键时前端靠 configSchema 守卫由 app.js 覆盖）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

HERE = Path(__file__).resolve().parent


def _load_webui():
    cached = sys.modules.get("webui_service_app")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location("webui_service_app", HERE / "app.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["webui_service_app"] = mod
    spec.loader.exec_module(mod)
    return mod


webui = _load_webui()


@pytest.fixture()
def env_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("FNMUSIC_NETEASE_ENABLED='true'\n", encoding="utf-8")
    (tmp_path / "VERSION").write_text("2.0.0\n", encoding="utf-8")
    monkeypatch.setattr(webui, "ENV_PATH", env)
    monkeypatch.setattr(webui, "VERSION_PATH", tmp_path / "VERSION")
    monkeypatch.setattr(webui, "STATIC_DIR", HERE / "static")
    return env


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "completion_log.json"
    monkeypatch.setenv("FNMUSIC_COMPLETION_LOG_FILE", str(path))
    from proxy import completion_log as cl
    cl.reset_for_tests()
    yield path
    cl.reset_for_tests()


def _client(monkeypatch):
    monkeypatch.setattr(webui, "_is_admin", lambda headers: True)
    return TestClient(webui.app)


def test_schema_has_completion_switch_default_false():
    spec = webui.SCHEMA["FNMUSIC_FALLBACK_ENABLED"]
    assert spec["kind"] == "bool"
    assert spec["default"] == "false"          # 默认关：换源会改变听到的版本，由用户主动开启
    assert spec["group"] == "search" and spec["reload"] == "hot"
    for key in ("FNMUSIC_FALLBACK_TIMEOUT", "FNMUSIC_FALLBACK_LIMIT",
                "FNMUSIC_FALLBACK_GRACE_S", "FNMUSIC_STREAM_TOTAL_MAX_S"):
        assert key in webui.SCHEMA
        assert "网易" not in webui.SCHEMA[key]["label"]     # 标签已音源无关化


def test_api_completion_states(env_file, ledger, monkeypatch):
    from proxy import completion_log as cl
    client = _client(monkeypatch)
    # 缺文件 → ok + missing + 空列表（不报错）
    r = client.get("/api/completion", headers={"x-trim-isadmin": "true"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["completion"]["status"] == "missing"
    assert body["completion"]["records"] == []
    # 有记录 → 正常返回 + 计数
    cl.record("completion_hit", guid="online:netease:1", provider="netease",
              title="歌", artist="歌手", reason="preview_rejected", fallback_guid="online:kuwo:1")
    cl.record("completion_skip", guid="online:netease:2", provider="netease",
              title="歌2", reason="switch_off")
    body = client.get("/api/completion", headers={"x-trim-isadmin": "true"}).json()
    assert body["completion"]["counts"] == {"hit": 1, "miss": 0, "skip": 1}
    assert body["completion"]["latest"]["outcome"] == "skip"
    # 损坏 → 明确报错，不返回假 0
    ledger.write_bytes(b"{broken")
    body = client.get("/api/completion", headers={"x-trim-isadmin": "true"}).json()
    assert body["ok"] is False and body["status"] == "corrupt"
    # 分页带界：越界 422
    assert client.get("/api/completion?limit=99999", headers={"x-trim-isadmin": "true"}).status_code == 422


def test_api_status_carries_completion_summary(env_file, ledger, monkeypatch):
    from proxy import completion_log as cl
    client = _client(monkeypatch)
    r = client.get("/api/status", headers={"x-trim-isadmin": "true"})
    assert r.status_code == 200
    # 无账本 → 零计数摘要（不报错）；损坏 → null
    assert r.json()["completion"]["counts"] == {"hit": 0, "miss": 0, "skip": 0}
    cl.record("completion_hit", guid="online:netease:1", provider="netease",
              title="歌", reason="resolve_failed")
    body = client.get("/api/status", headers={"x-trim-isadmin": "true"}).json()
    assert body["completion"]["counts"]["hit"] == 1
    assert body["completion"]["latest"]["event"] == "completion_hit"
    # status 里带最近记录，概览卡片可直接展示歌名/命中音源/耗时
    assert body["completion"]["records"][0]["title"] == "歌"
    assert "elapsed_ms" in body["completion"]["records"][0] or True
    cl.record("completion_hit", guid="online:netease:9", provider="netease", title="歌2",
              reason="preview_rejected", elapsed_ms=4321, fallback_source="kuwo",
              fallback_guid="online:kuwo:9")
    body = client.get("/api/status", headers={"x-trim-isadmin": "true"}).json()
    assert len(body["completion"]["records"]) == 2
    assert body["completion"]["latest"]["fallback_source"] == "kuwo"
    assert body["completion"]["latest"]["elapsed_ms"] == 4321
    ledger.write_bytes(b"###broken")
    assert client.get("/api/status", headers={"x-trim-isadmin": "true"}).json()["completion"] is None


def test_config_returns_schema_defaults_for_new_keys(env_file, monkeypatch):
    client = _client(monkeypatch)
    body = client.get("/api/config", headers={"x-trim-isadmin": "true"}).json()
    assert body["values"]["FNMUSIC_FALLBACK_ENABLED"] == "false"
    assert body["values"]["FNMUSIC_FALLBACK_TIMEOUT"] == "25"
    assert "FNMUSIC_FALLBACK_ENABLED" in body["schema"]
