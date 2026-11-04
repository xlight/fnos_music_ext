"""第五源 QQ 音乐（proxy 侧）单元测试。

不访问网络：服务响应一律用 httpx.MockTransport 伪造。覆盖配置解析、GUID/标签、
_source_enabled 白名单豁免、搜索映射、失败与空的区分、取链降档与钉链复用、
深分页水线以及 Gate 重置。
"""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

import proxy.app as pa
from proxy.app import (
    CONF,
    _ENV_WATCH_KEYS,
    _QQMUSIC_SEARCH_GATE,
    _QQMUSIC_URL_PIN,
    _SOURCE_CONF_KEYS,
    _deep_sources_available,
    _direct_provider_for_source,
    _env_watch_parse,
    _qqmusic_search_request,
    _source_enabled,
    build_online_track,
    fetch_qqmusic_search,
    get_qqmusic_client,
    online_guid_from_item,
    reset_source_search_gates,
    resolve_qqmusic_url,
    source_display_prefix,
    strip_source_tag,
)

MID = "0039MnYb0qxYhV"


@pytest.fixture(autouse=True)
def _clean_state():
    _QQMUSIC_URL_PIN.clear()
    reset_source_search_gates()
    yield
    _QQMUSIC_URL_PIN.clear()
    reset_source_search_gates()


def _search_item(**over):
    item = {
        "song_id": MID,
        "song_name": "晴天",
        "artist": "周杰伦",
        "album_name": "叶惠美",
        "album_pic_url": "https://img.test/qq.jpg",
        "duration": 269,
        "quality": "SQ",
    }
    item.update(over)
    return item


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8775")


# =========================================================================
# 配置
# =========================================================================
def test_conf_has_qqmusic_keys():
    assert CONF["qqmusic_url"].startswith("http")
    assert CONF["qqmusic_enabled"] is False
    assert CONF["qqmusic_quality"] == "lossless"
    assert isinstance(CONF["qqmusic_search_limit"], int)
    # P2 键保留但本轮不接行为
    assert CONF["qqmusic_playlists_enabled"] is False


def test_env_watch_registers_qqmusic_keys():
    for env_key in (
        "FNMUSIC_QQMUSIC_ENABLED",
        "FNMUSIC_QQMUSIC_URL",
        "FNMUSIC_QQMUSIC_QUALITY",
        "FNMUSIC_QQMUSIC_SEARCH_LIMIT",
    ):
        assert env_key in _ENV_WATCH_KEYS
    assert {
        "qqmusic_enabled",
        "qqmusic_url",
        "qqmusic_quality",
        "qqmusic_search_limit",
    } <= _SOURCE_CONF_KEYS


def test_env_watch_parse_qqmusic_kinds():
    assert _env_watch_parse("true", "bool") is True
    assert _env_watch_parse("lossless", "qqmusic_quality") == "lossless"
    # jymaster 是网易专有档，QQ 不接受
    assert _env_watch_parse("jymaster", "qqmusic_quality") is None
    assert _env_watch_parse("35", "qqmusic_search_limit") == 35
    assert _env_watch_parse("999", "qqmusic_search_limit") == 100
    assert _env_watch_parse("", "qqmusic_search_limit") == 20


def test_get_qqmusic_client_reuses_instance():
    stub = SimpleNamespace(state=SimpleNamespace())
    client = get_qqmusic_client(stub)
    assert str(client.base_url).rstrip("/") == CONF["qqmusic_url"].rstrip("/")
    assert get_qqmusic_client(stub) is client
    asyncio.run(client.aclose())


# =========================================================================
# GUID / 标签 / 白名单
# =========================================================================
def test_guid_and_tag():
    item = {"id": f"qqmusic:{MID}", "source": "qqmusic", "title": "晴天"}
    guid = online_guid_from_item(item)
    assert guid == f"online:qqmusic:{MID}"
    assert source_display_prefix("qqmusic") == "[QQ] "
    assert strip_source_tag("[QQ] 晴天") == "晴天"
    assert strip_source_tag("[QQ] [VIP] 晴天") == "晴天"


def test_build_online_track_marks_source():
    item = {"id": f"qqmusic:{MID}", "source": "qqmusic", "title": "晴天",
            "artist": "周杰伦", "album": "叶惠美", "duration_s": 269, "ext": "flac"}
    marked = build_online_track(item, mark_source=True)
    plain = build_online_track(item, mark_source=False)
    assert marked["title"].startswith("[QQ] ")
    assert not plain["title"].startswith("[")


def test_source_enabled_exempts_online_sources(monkeypatch):
    monkeypatch.setitem(CONF, "qqmusic_enabled", True)
    monkeypatch.setitem(CONF, "musicdl_enabled", False)
    monkeypatch.setitem(CONF, "online_sources", "KuwoMusicClient,MiguMusicClient")
    # QQ 豁免 musicdl 的源名清单，启用位不落到 musicdl_enabled
    assert _source_enabled(f"online:qqmusic:{MID}") is True
    assert _source_enabled("online:kuwo:x") is False

    monkeypatch.setitem(CONF, "qqmusic_enabled", False)
    assert _source_enabled(f"online:qqmusic:{MID}") is False


def test_direct_play_provider_excludes_qqmusic():
    assert _direct_provider_for_source("qqmusic") is None
    assert _direct_provider_for_source("netease") == "netease"
    assert _direct_provider_for_source("lx") == "lx"
    assert _direct_provider_for_source("kuwo") == "musicdl"


def test_all_source_client_providers_defined():
    """每个音源的客户端 provider 必须存在。

    回归：曾有提交误删 get_neteasefree_client，仅靠 import 发现不了，
    要到运行期调健康检查/取链才报 NameError。
    """
    for name in (
        "get_musicdl_client",
        "get_musicbox_client",
        "get_neteasefree_client",
        "get_qqmusic_client",
        "get_lx_client",
    ):
        assert callable(getattr(pa, name, None)), f"缺少音源客户端 provider: {name}"


# =========================================================================
# 搜索
# =========================================================================
@pytest.mark.anyio
async def test_search_mapping():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/search"
        return httpx.Response(200, json={"ok": True, "code": 200, "data": [_search_item()]})

    client = _client(handler)
    items = await fetch_qqmusic_search(client, "晴天", 20)
    await client.aclose()

    assert isinstance(items, list) and len(items) == 1
    first = items[0]
    assert first["id"] == f"qqmusic:{MID}"
    assert first["source"] == "qqmusic"
    assert first["title"] == "晴天"
    assert first["artist"] == "周杰伦"
    assert first["duration_s"] == 269.0
    assert first["ext"] == "flac"


@pytest.mark.anyio
async def test_search_offset_maps_to_param():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"ok": True, "data": []})

    client = _client(handler)
    await _qqmusic_search_request(client, "x", 20, offset=40)
    await client.aclose()
    assert seen["offset"] == "40"


@pytest.mark.anyio
async def test_search_failure_is_none_but_empty_is_list():
    """深分页失败必须是 None（瞬时失败），不能被当成“已取尽”的空列表。"""
    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"ok": False, "code": 502})

    def failed_payload(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "code": 502, "data": []})

    def empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "code": 200, "data": []})

    for handler in (boom, failed_payload):
        client = _client(handler)
        assert await _qqmusic_search_request(client, "x", 20, offset=200) is None
        await client.aclose()

    client = _client(empty)
    assert await _qqmusic_search_request(client, "x", 20, offset=200) == []
    await client.aclose()


@pytest.mark.anyio
async def test_search_drops_bad_rows():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "data": [
            {"song_id": "", "song_name": "无 mid"},
            {"song_id": "m2", "song_name": ""},
            _search_item(),
        ]})

    client = _client(handler)
    items = await fetch_qqmusic_search(client, "晴天", 20)
    await client.aclose()
    assert [it["id"] for it in items] == [f"qqmusic:{MID}"]


# =========================================================================
# 取链：降档 / 钉链 / 失败
# =========================================================================
@pytest.mark.anyio
async def test_resolve_url_uses_pin():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        return httpx.Response(200, json={"ok": True, "data": {
            "code": 200, "url": "https://cdn.qq.test/a.flac", "quality": "FLAC",
            "ext": ".flac", "expiration": 300,
        }})

    client = _client(handler)
    first = await resolve_qqmusic_url(client, MID, quality="lossless")
    second = await resolve_qqmusic_url(client, MID, quality="lossless")
    await client.aclose()

    assert first[0] == "https://cdn.qq.test/a.flac"
    assert first[1]["ext"] == "flac"
    assert second[0] == first[0]
    assert len(calls) == 1  # 第二次命中钉链，未再请求服务


@pytest.mark.anyio
async def test_resolve_url_fresh_bypasses_pin():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"ok": True, "data": {
            "code": 200, "url": f"https://cdn.qq.test/{len(calls)}.mp3",
            "quality": "MP3_128", "ext": ".mp3", "expiration": 300,
        }})

    client = _client(handler)
    await resolve_qqmusic_url(client, MID)
    url, _meta = await resolve_qqmusic_url(client, MID, fresh=True)
    await client.aclose()
    assert url.endswith("/2.mp3")
    assert len(calls) == 2


@pytest.mark.anyio
async def test_resolve_url_degrades_and_fails():
    """服务侧降档后仍无直链时返回 None，不缓存空链。"""
    def no_permission(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "data": {"code": 404, "url": "", "reason": "no_permission"}})

    client = _client(no_permission)
    assert await resolve_qqmusic_url(client, MID, quality="hires") is None
    await client.aclose()
    assert _QQMUSIC_URL_PIN.get(MID) is None


# =========================================================================
# 深分页水线
# =========================================================================
def test_deep_sources_available_includes_qqmusic(monkeypatch):
    monkeypatch.setitem(CONF, "netease_enabled", False)
    monkeypatch.setitem(CONF, "neteasefree_enabled", False)
    monkeypatch.setitem(CONF, "lx_enabled", False)
    monkeypatch.setitem(CONF, "qqmusic_enabled", True)
    monkeypatch.setitem(CONF, "search_deep_max_pages", 10)

    entry = {"deep_round": 1, "exhausted": set()}
    assert _deep_sources_available(entry) is True

    entry["exhausted"].add("qqmusic")
    assert _deep_sources_available(entry) is False

    # 达到页码封顶后不再取页
    entry2 = {"deep_round": 10, "exhausted": set()}
    assert _deep_sources_available(entry2) is False


# =========================================================================
# Gate 重置
# =========================================================================
@pytest.mark.anyio
async def test_reset_source_search_gates_covers_qqmusic():
    """切源重置必须覆盖 QQ Gate，否则旧词会在新会话里串台。"""
    before = _QQMUSIC_SEARCH_GATE._epoch
    reset_source_search_gates()
    assert _QQMUSIC_SEARCH_GATE._epoch == before + 1
