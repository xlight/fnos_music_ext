"""整曲补全配置的热重载解析契约（change add-full-track-completion · 任务 1.3/1.4）。

覆盖五个新键的 _env_watch_parse：取值域必须与 WebUI schema 一致——
尤其 FALLBACK_GRACE_S 允许 0（不得被 seconds 夹成 1），FALLBACK_ENABLED 空值恢复默认。
"""
import pytest

from proxy.app import CONF, _env_watch_parse, _ENV_WATCH_KEYS


COMPLETION_KEYS = (
    "FNMUSIC_FALLBACK_TIMEOUT",
    "FNMUSIC_FALLBACK_LIMIT",
    "FNMUSIC_FALLBACK_GRACE_S",
    "FNMUSIC_STREAM_TOTAL_MAX_S",
    "FNMUSIC_FALLBACK_ENABLED",
)


def test_completion_keys_registered_with_dedicated_kinds():
    """五个键都登记进热重载白名单，且 kind 与 schema 取值域一一对应。"""
    for key in COMPLETION_KEYS:
        assert key in _ENV_WATCH_KEYS, key
    assert _ENV_WATCH_KEYS["FNMUSIC_FALLBACK_GRACE_S"][1] == "fallback_grace_s"
    assert _ENV_WATCH_KEYS["FNMUSIC_FALLBACK_ENABLED"][1] == "default_bool"
    assert _ENV_WATCH_KEYS["FNMUSIC_FALLBACK_ENABLED"][0] == "fallback_enabled"


def test_fallback_grace_s_allows_zero_and_clamps_to_schema_range():
    """GRACE_S=0 必须解析为 0（schema min=0）；范围 0-15 与 schema 一致。"""
    assert _env_watch_parse("0", "fallback_grace_s") == 0.0
    assert _env_watch_parse("8", "fallback_grace_s") == 8.0
    assert _env_watch_parse("15", "fallback_grace_s") == 15.0
    assert _env_watch_parse("99", "fallback_grace_s") == 15.0      # clamp 到 max
    assert _env_watch_parse("-3", "fallback_grace_s") == 0.0       # clamp 到 min
    assert _env_watch_parse("abc", "fallback_grace_s") is None
    assert _env_watch_parse("", "fallback_grace_s") is None


def test_fallback_enabled_empty_value_restores_default_not_false():
    """开关空值 = 未设置 = 恢复默认（true），绝不能当成 false。"""
    assert _env_watch_parse("", "default_bool") is None           # None → 热重载跳过，保持默认
    assert _env_watch_parse("true", "default_bool") is True
    assert _env_watch_parse("false", "default_bool") is False
    assert _env_watch_parse("0", "default_bool") is False
    assert _env_watch_parse("yes", "default_bool") is True
    assert _env_watch_parse("garbage", "default_bool") is False


def test_completion_numeric_keys_match_schema_ranges():
    """其余三个数值键的解析范围与 schema 一致。"""
    assert _env_watch_parse("18", "seconds") == 18.0
    assert _env_watch_parse("0", "seconds") == 1.0                # seconds 下限 1（与 schema min=1 一致）
    assert _env_watch_parse("5", "fallback_limit") == 5
    assert _env_watch_parse("0", "fallback_limit") == 1
    assert _env_watch_parse("31", "fallback_limit") == 30
    assert _env_watch_parse("20", "total_budget_s") == 20.0
    assert _env_watch_parse("4", "total_budget_s") == 5.0
    assert _env_watch_parse("301", "total_budget_s") == 300.0


def test_fallback_enabled_default_false(monkeypatch):
    """CONF 默认关闭（"换源播放"是可改变听到版本的行为，默认应由用户主动开启）。"""
    assert CONF["fallback_enabled"] is False
    monkeypatch.setitem(CONF, "fallback_enabled", True)
    assert CONF["fallback_enabled"] is True
