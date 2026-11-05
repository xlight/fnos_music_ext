"""NetEase 跨源回退（change: add-netease-playback-fallback）单测。

覆盖三组语义：
- 回退搜索辅助函数：结果/无结果/超时/并发单飞/与 `_recover_source` 互不 supersede/
  `_retained_track` 命中（任务 1.6）。
- stream_track 集成：NetEase 失败 → 搜索 → 307 → 播放回退曲目（任务 2.8），
  元数据三态（任务 3.3）。
- 回退矩阵：307/404/禁用/超时/并发/条目命中/预算 re-arm/Range 门控/防链式/
  deadline 只延长一次（任务 4.1-4.10）。
"""
import asyncio
import importlib
import time

import httpx
import pytest
from starlette.requests import Request

from proxy.app import CONF, app

NETEASE_GUID = "online:netease:123"
FALLBACK_GUID = "online:kuwo:1"


def _item(raw_id: str = "kuwo:1", **over) -> dict:
    item = {
        "id": raw_id,
        "source": raw_id.split(":")[0] if ":" in raw_id else "kuwo",
        "title": "晴天",
        "artist": "周杰伦",
        "album": "叶惠美",
        "duration_s": 269.0,
        "ext": "mp3",
    }
    item.update(over)
    return item


def _request(guid: str = NETEASE_GUID, *, query: str = "", headers: dict | None = None,
             method: str = "GET") -> Request:
    qs = f"guid={guid}" + (f"&{query}" if query else "")
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({
        "type": "http", "app": app, "method": method,
        "path": "/music/api/v1/track/stream",
        "query_string": qs.encode(), "headers": raw,
    })


def _opened(payload: bytes = b"fallback-audio", ext: str = "mp3") -> tuple:
    """构造 `_open_online_stream` 的返回元组（resp, owned, ext, info, chunks, first）。"""
    resp = httpx.Response(200, content=payload,
                          headers={"content-type": "audio/mpeg",
                                   "content-length": str(len(payload))})
    info = {"title": "晴天", "artist": "周杰伦", "duration_s": 269.0, "ext": ext}
    return (resp, None, ext, info, None, b"")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    p = importlib.import_module("proxy.app")
    p._SEARCH_CACHE.clear()
    p._NETEASE_URL_PIN.clear()
    p.reset_source_search_gates()
    monkeypatch.setitem(CONF, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setenv("FNMUSIC_COMPLETION_LOG_FILE", str(tmp_path / "completion_log.json"))
    monkeypatch.setitem(CONF, "library_dir", str(tmp_path / "library"))
    monkeypatch.setitem(CONF, "fav_dir", str(tmp_path / "fav"))
    monkeypatch.setitem(CONF, "music_db", str(tmp_path / "music.db"))
    monkeypatch.setitem(CONF, "musicdl_enabled", True)
    monkeypatch.setitem(CONF, "netease_enabled", True)
    monkeypatch.setitem(CONF, "neteasefree_enabled", False)
    monkeypatch.setitem(CONF, "lx_enabled", False)
    monkeypatch.setitem(CONF, "netease_direct_play", False)
    monkeypatch.setitem(CONF, "musicdl_direct_play", False)
    monkeypatch.setitem(CONF, "fallback_enabled", True)   # 本文件测"开启时"的补全路径
    monkeypatch.setitem(CONF, "online_sources", "KuwoMusicClient")
    monkeypatch.setitem(CONF, "online_limit", 30)
    monkeypatch.setitem(CONF, "fallback_timeout", 6.0)

    async def _no_local(*a, **kw):
        return None

    async def _no_lyric(*a, **kw):
        return None

    monkeypatch.setattr(p, "resolve_local_play", _no_local)
    monkeypatch.setattr(p, "find_cache_file", lambda *a, **kw: None)
    monkeypatch.setattr(p, "maybe_auto_cache_first", lambda *a, **kw: False)
    monkeypatch.setattr(p, "_register_full_fetch", lambda *a, **kw: False)
    monkeypatch.setattr(p, "_auto_lyric_after_finalize", _no_lyric)
    monkeypatch.setattr(p.localmatch, "clear_served", lambda *a, **kw: None)
    yield
    p._SEARCH_CACHE.clear()
    p._NETEASE_URL_PIN.clear()
    p.reset_source_search_gates()


def _patch_fetch(monkeypatch, result=None, calls=None, delay: float = 0.0):
    """替换 fetch_musicdl_search（绕过门控，聚焦回退语义）。"""
    p = importlib.import_module("proxy.app")

    async def _fetch(client, keyword, limit, sources=None, grace_s=0.0):
        if calls is not None:
            calls.append(keyword)
        if delay:
            await asyncio.sleep(delay)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(p, "fetch_musicdl_search", _fetch)
    return _fetch


# =========================================================================
# 1.6 回退搜索辅助函数
# =========================================================================
@pytest.mark.anyio
async def test_fallback_search_returns_first_musicdl_guid(monkeypatch):
    p = importlib.import_module("proxy.app")
    calls: list[str] = []
    _patch_fetch(monkeypatch, {"items": [_item(), _item("kuwo:2")]}, calls)
    req = _request()
    retained: dict = {}

    guid = await p._completion_search(req, NETEASE_GUID, "晴天", "周杰伦",
                                            entry=retained)

    assert guid == FALLBACK_GUID
    assert calls == ["晴天 周杰伦"]
    # 1.8：缓存 key 带凭据隔离
    cache_key = f"netease-fallback:{p._credential_scope(req)}:晴天:周杰伦"
    entry = p._SEARCH_CACHE[cache_key]
    assert entry["credentials"] == p._credential_scope(req)
    assert entry["keyword"] == "晴天 周杰伦"
    assert entry["ttl"] == p._FALLBACK_CACHE_TTL_S == 300.0
    assert entry["scope"] == p._credential_scope(req)
    # 1.5：冷却时间戳写在**原 retained entry** 上，不写进新建的 fallback entry
    assert "fallback:netease" not in entry
    assert "fallback:netease" in retained


@pytest.mark.anyio
async def test_fallback_scope_is_sharded_by_keyword(monkeypatch):
    """1.2：scope 按 keyword 分片——不同曲目互不 supersede，同曲目共享单飞。"""
    p = importlib.import_module("proxy.app")
    req = _request()

    scope_a = p._fallback_scope(req, "晴天 周杰伦")
    scope_b = p._fallback_scope(req, "夜曲 周杰伦")
    scope_a2 = p._fallback_scope(req, "晴天 周杰伦")

    assert scope_a == scope_a2
    assert scope_a != scope_b
    assert scope_a.startswith(p._credential_scope(req) + ":play:fallback:")
    # 与 _recover_source 的 ":play" scope 不同
    assert scope_a != p._credential_scope(req) + ":play"


# =========================================================================
# 条目挑选：不能直接取第一条（CT201 实测网易「天龙八部之宿敌」回退命中
# 63s 现场版，而同名录音室版在第 5 条）
# =========================================================================
def test_fallback_pick_prefers_exact_title_over_live_version():
    """同名录音室版优先于排在前面的现场版/伴奏版。"""
    p = importlib.import_module("proxy.app")
    items = [
        _item("kuwo:1", title="天龙八部之宿敌 (2026安泊猜想巡回演唱会成都站现场)",
              artist="许嵩", duration_s=63.0),
        _item("kuwo:2", title="天龙八部之宿敌 (消音伴奏)", artist="许嵩", duration_s=15.0),
        _item("kuwo:3", title="天龙八部之宿敌", artist="许嵩", duration_s=264.0),
    ]
    picked = p._fallback_pick_item(items, "天龙八部之宿敌", "许嵩", 263.0)
    assert picked["id"] == "kuwo:3"


def test_fallback_pick_uses_containment_when_duration_matches():
    """标题互相包含且时长贴近 → 命中（源站常给标题加后缀）。"""
    p = importlib.import_module("proxy.app")
    items = [
        _item("kuwo:9", title="玻璃 demo (DJ Nexus烬羽版)", artist="Gareth.T", duration_s=194.0),
        _item("kuwo:8", title="玻璃", artist="Gareth.T", duration_s=194.0),
    ]
    picked = p._fallback_pick_item(items, "玻璃", "Gareth.T", 194.0)
    assert picked["id"] == "kuwo:8"


def test_fallback_pick_keeps_first_item_when_nothing_matches():
    """没有同名/贴近的版本 → 退回第一条（旧行为，不做版本过滤）。"""
    p = importlib.import_module("proxy.app")
    items = [
        _item("kuwo:7", title="完全不同的歌", duration_s=200.0),
        _item("kuwo:6", title="另一首", duration_s=100.0),
    ]
    picked = p._fallback_pick_item(items, "晴天", "周杰伦", 269.0)
    assert picked["id"] == "kuwo:7"


def test_fallback_pick_rejects_other_artist():
    """同名但歌手不符 → 不选（同名翻唱不算命中）。"""
    p = importlib.import_module("proxy.app")
    items = [
        _item("kuwo:5", title="晴天", artist="其他歌手", duration_s=269.0),
        _item("kuwo:4", title="晴天", artist="周杰伦", duration_s=269.0),
    ]
    picked = p._fallback_pick_item(items, "晴天", "周杰伦", 269.0)
    assert picked["id"] == "kuwo:4"


def test_fallback_pick_prefers_known_duration_over_unknown():
    """元数据时长为空的条目排在有真实时长的同名条目之后。

    CT201 实测：sgogo 的同名条目是 120s 试听且 duration_s=0，
    gequke 的同名条目有 235s → 必须选后者。
    """
    p = importlib.import_module("proxy.app")
    items = [
        _item("sgogo:1", title="天龙八部之宿敌", artist="许嵩", duration_s=0.0),
        _item("gequke:1", title="天龙八部之宿敌", artist="许嵩", duration_s=235.0),
    ]
    picked = p._fallback_pick_item(items, "天龙八部之宿敌", "许嵩", 263.0)
    assert picked["id"] == "gequke:1"


def test_fallback_pick_drops_preview_like_short_item():
    """不到原时长一半的条目判试听片段，剔除。"""
    p = importlib.import_module("proxy.app")
    items = [
        _item("kuwo:20", title="天龙八部之宿敌 (现场版)", artist="许嵩", duration_s=63.0),
        _item("kuwo:21", title="天龙八部之宿敌", artist="许嵩", duration_s=264.0),
    ]
    picked = p._fallback_pick_item(items, "天龙八部之宿敌", "许嵩", 263.0)
    assert picked["id"] == "kuwo:21"


@pytest.mark.anyio
async def test_fallback_search_returns_matching_title_item(monkeypatch):
    """回退搜索返回挑选后的条目（不是第一条）。"""
    p = importlib.import_module("proxy.app")
    items = [
        _item("kuwo:11", title="天龙八部之宿敌 (Live)", artist="许嵩", duration_s=221.0),
        _item("kuwo:12", title="天龙八部之宿敌", artist="许嵩", duration_s=264.0),
    ]
    _patch_fetch(monkeypatch, {"items": items})
    req = _request()

    guid = await p._completion_search(req, NETEASE_GUID, "天龙八部之宿敌", "许嵩",
                                            duration_s=263.0)
    assert guid == "online:kuwo:12"


@pytest.mark.anyio
async def test_fallback_search_passes_grace_override(monkeypatch):
    """回退搜索带 grace_s：服务端默认 2s 宽限只等得到第一个响应源。

    CT201 实测：默认宽限下回退只拿到 1 条 120s 试听，同名全长版在
    Kuwo/咪咕（7-10s 才交结果）。
    """
    p = importlib.import_module("proxy.app")
    graces: list[float] = []

    async def _fetch(client, keyword, limit, sources=None, grace_s=0.0):
        graces.append(grace_s)
        return {"items": [_item()]}

    monkeypatch.setattr(p, "fetch_musicdl_search", _fetch)

    await p._completion_search(_request(), NETEASE_GUID, "晴天", "周杰伦")
    assert graces == [8.0]

    monkeypatch.setitem(CONF, "fallback_grace_s", 0.0)
    await p._completion_search(_request(), NETEASE_GUID, "夜曲", "周杰伦")
    assert graces == [8.0, 0.0]


@pytest.mark.anyio
async def test_fallback_search_uses_configured_limit(monkeypatch):
    """回退搜索用 FNMUSIC_FALLBACK_LIMIT（默认 5），不是交互搜索的 online_limit。

    musicdl 的 search_probe 逐条探活：条数越多越慢（CT201 实测 limit=5 约 5s、
    limit=30 约 12s），回退撞穿预算就等于没有回退。
    """
    p = importlib.import_module("proxy.app")
    limits: list[int] = []

    async def _fetch(client, keyword, limit, sources=None, grace_s=0.0):
        limits.append(limit)
        return {"items": [_item()]}

    monkeypatch.setattr(p, "fetch_musicdl_search", _fetch)
    req = _request()

    await p._completion_search(req, NETEASE_GUID, "晴天", "周杰伦")
    assert limits == [5]

    monkeypatch.setitem(CONF, "fallback_limit", 12)
    await p._completion_search(req, NETEASE_GUID, "夜曲", "周杰伦")
    assert limits == [5, 12]


@pytest.mark.anyio
async def test_fallback_search_reads_cache_before_searching(monkeypatch):
    """1.7：缓存命中 → 不再发起上游搜索。"""
    p = importlib.import_module("proxy.app")
    calls: list[str] = []
    _patch_fetch(monkeypatch, {"items": [_item()]}, calls)
    req = _request()

    first = await p._completion_search(req, NETEASE_GUID, "晴天", "周杰伦")
    second = await p._completion_search(req, NETEASE_GUID, "晴天", "周杰伦")

    assert first == second == FALLBACK_GUID
    assert calls == ["晴天 周杰伦"]  # 第二次命中缓存，未再搜


@pytest.mark.anyio
async def test_fallback_cache_isolated_per_credential_scope(monkeypatch):
    """1.8：不同凭据 scope 的回退缓存互不覆盖。"""
    p = importlib.import_module("proxy.app")
    calls: list[str] = []
    _patch_fetch(monkeypatch, {"items": [_item()]}, calls)
    req_a = _request()
    req_b = _request(headers={"cookie": "session=someone-else"})

    await p._completion_search(req_a, NETEASE_GUID, "晴天", "周杰伦")
    await p._completion_search(req_b, NETEASE_GUID, "晴天", "周杰伦")

    assert len(calls) == 2
    keys = [k for k in p._SEARCH_CACHE if k.startswith("netease-fallback:")]
    assert len(keys) == 2


@pytest.mark.anyio
async def test_fallback_search_respects_cooldown(monkeypatch):
    """1.5：冷却窗口内不再发起回退搜索。"""
    p = importlib.import_module("proxy.app")
    calls: list[str] = []
    _patch_fetch(monkeypatch, {"items": [_item()]}, calls)
    req = _request()
    retained = {"fallback:netease": time.monotonic()}

    assert await p._completion_search(req, NETEASE_GUID, "晴天", "周杰伦",
                                            entry=retained) is None
    assert calls == []


@pytest.mark.anyio
async def test_fallback_search_budget_clamped_to_remaining(monkeypatch):
    """2.8：搜索预算夹在剩余首播时间之内。"""
    p = importlib.import_module("proxy.app")
    calls: list[str] = []
    _patch_fetch(monkeypatch, {"items": [_item()]}, calls, delay=5.0)
    req = _request()

    started = time.monotonic()
    assert await p._completion_search(req, NETEASE_GUID, "晴天", "周杰伦",
                                            budget=0.5) is None
    assert time.monotonic() - started < 2.0


@pytest.mark.anyio
async def test_fallback_search_without_result_returns_none(monkeypatch):
    p = importlib.import_module("proxy.app")
    _patch_fetch(monkeypatch, {"items": []})
    req = _request()

    assert await p._completion_search(req, NETEASE_GUID, "晴天", "周杰伦") is None


@pytest.mark.anyio
async def test_fallback_search_timeout_returns_none(monkeypatch):
    p = importlib.import_module("proxy.app")
    monkeypatch.setitem(CONF, "fallback_timeout", 1.0)
    _patch_fetch(monkeypatch, {"items": [_item()]}, delay=5.0)
    req = _request()

    started = time.monotonic()
    assert await p._completion_search(req, NETEASE_GUID, "晴天", "周杰伦") is None
    assert time.monotonic() - started < 3.0


@pytest.mark.anyio
async def test_fallback_search_single_flight_queries_upstream_once(monkeypatch):
    """并发两次同 scope 同 keyword → 上游只搜一次（复用 _MUSICDL_SEARCH_GATE）。"""
    p = importlib.import_module("proxy.app")
    hits: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.path)
        await asyncio.sleep(0.2)
        return httpx.Response(200, json={"items": [_item()]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url="http://musicdl.test") as client:
        monkeypatch.setattr(p, "get_musicdl_client", lambda a: client)
        req = _request()
        results = await asyncio.gather(
            p._completion_search(req, NETEASE_GUID, "晴天", "周杰伦"),
            p._completion_search(req, NETEASE_GUID, "晴天", "周杰伦"),
        )

    assert hits == ["/search"]
    assert results == [FALLBACK_GUID, FALLBACK_GUID]


@pytest.mark.anyio
async def test_fallback_search_and_recover_source_do_not_supersede(monkeypatch):
    """回退 scope（:play:fallback）与 _recover_source scope（:play）互不 supersede。"""
    p = importlib.import_module("proxy.app")
    hits: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        keyword = request.url.params.get("keyword")
        hits.append(keyword)
        await asyncio.sleep(0.1)
        if keyword == "晴天 周杰伦":
            return httpx.Response(200, json={"items": [_item("kuwo:1")]})
        return httpx.Response(200, json={"items": [_item("kuwo:9", title="夜曲")]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url="http://musicdl.test") as client:
        monkeypatch.setattr(p, "get_musicdl_client", lambda a: client)
        req = _request()
        entry = {"keyword": "夜曲", "ts": time.time()}
        fallback, recovered = await asyncio.gather(
            p._completion_search(req, NETEASE_GUID, "晴天", "周杰伦"),
            p._recover_source(req, "online:kuwo:9", entry),
        )

    assert fallback == FALLBACK_GUID
    assert recovered is True
    assert fallback is not p._SUPERSEDED
    assert sorted(hits) == sorted(["晴天 周杰伦", "夜曲"])


@pytest.mark.anyio
async def test_fallback_entry_is_retained_after_write(monkeypatch):
    """回退条目写入后 _retained_track 能按 credentials 口径取到。"""
    p = importlib.import_module("proxy.app")
    _patch_fetch(monkeypatch, {"items": [_item()]})
    req = _request()

    guid = await p._completion_search(req, NETEASE_GUID, "晴天", "周杰伦")
    item, entry = p._retained_track(req, guid)

    assert guid == FALLBACK_GUID
    assert item is not None and item["id"] == "kuwo:1"
    assert entry is not None
    assert entry["credentials"] == p._credential_scope(req)


# =========================================================================
# 4.1-4.10 回退矩阵（stream_track 集成）
# =========================================================================
@pytest.mark.anyio
async def test_stream_fallback_307_when_musicdl_has_result(monkeypatch):
    """4.1 resolve 失败 + musicdl 有结果 → 307。"""
    p = importlib.import_module("proxy.app")
    attempts: list[str] = []
    _patch_fetch(monkeypatch, {"items": [_item()]})
    monkeypatch.setattr(p, "_online_meta_for_request",
                        lambda *a: {"title": "晴天", "artist": "周杰伦"})

    async def fake_open(request, guid, range_header, **kwargs):
        attempts.append(guid)
        if guid == NETEASE_GUID:
            return None
        return _opened()

    monkeypatch.setattr(p, "_open_online_stream", fake_open)

    response = await p.stream_track(_request())

    assert response.status_code == 307
    assert "guid=online%3Akuwo%3A1" in response.headers["location"]
    assert "_ext_rendition=1" in response.headers["location"]
    assert attempts == [NETEASE_GUID, FALLBACK_GUID]


@pytest.mark.anyio
async def test_stream_fallback_404_without_result(monkeypatch):
    """4.2 resolve 失败 + 无结果 → 404。"""
    p = importlib.import_module("proxy.app")
    _patch_fetch(monkeypatch, {"items": []})
    monkeypatch.setattr(p, "_online_meta_for_request",
                        lambda *a: {"title": "晴天", "artist": "周杰伦"})

    async def fake_open(request, guid, range_header, **kwargs):
        return None

    monkeypatch.setattr(p, "_open_online_stream", fake_open)

    response = await p.stream_track(_request())

    assert response.status_code == 404
    assert response.body and b"online source unavailable" in response.body


@pytest.mark.anyio
async def test_stream_fallback_skipped_when_musicdl_disabled(monkeypatch):
    """4.3 musicdl disabled → 不搜索，直接 404。"""
    p = importlib.import_module("proxy.app")
    monkeypatch.setitem(CONF, "musicdl_enabled", False)
    calls: list[str] = []
    _patch_fetch(monkeypatch, {"items": [_item()]}, calls)
    monkeypatch.setattr(p, "_online_meta_for_request",
                        lambda *a: {"title": "晴天", "artist": "周杰伦"})

    async def fake_open(request, guid, range_header, **kwargs):
        return None

    monkeypatch.setattr(p, "_open_online_stream", fake_open)

    response = await p.stream_track(_request())

    assert response.status_code == 404
    assert calls == []


@pytest.mark.anyio
async def test_stream_fallback_not_triggered_on_resolve_timeout(monkeypatch):
    """4.4 resolve 超时 → 不触发回退（超时路径走 refresh 重试，交给死钉恢复）。"""
    p = importlib.import_module("proxy.app")
    calls: list[str] = []
    _patch_fetch(monkeypatch, {"items": [_item()]}, calls)
    monkeypatch.setattr(p, "_online_meta_for_request",
                        lambda *a: {"title": "晴天", "artist": "周杰伦"})

    async def fake_open(request, guid, range_header, **kwargs):
        raise httpx.ReadTimeout("resolve timeout")

    monkeypatch.setattr(p, "_open_online_stream", fake_open)

    response = await p.stream_track(_request())

    assert response.status_code == 404
    assert calls == []


@pytest.mark.anyio
async def test_stream_fallback_rearms_budget_after_slow_resolve(monkeypatch):
    """4.7 resolve 吃掉大部分启动预算后回退仍生效（deadline 一次性 re-arm）。"""
    p = importlib.import_module("proxy.app")
    monkeypatch.setattr(p, "_STREAM_STARTUP_S", 1.2)
    attempts: list[str] = []
    _patch_fetch(monkeypatch, {"items": [_item()]})
    monkeypatch.setattr(p, "_online_meta_for_request",
                        lambda *a: {"title": "晴天", "artist": "周杰伦"})

    async def fake_open(request, guid, range_header, **kwargs):
        attempts.append(guid)
        if guid == NETEASE_GUID:
            await asyncio.sleep(0.9)   # 吃掉 9s（按 1:10 缩放），只剩 ~0.3s
            return None
        return _opened()

    monkeypatch.setattr(p, "_open_online_stream", fake_open)

    started = time.monotonic()
    response = await p.stream_track(_request())
    elapsed = time.monotonic() - started

    assert response.status_code == 307
    assert attempts == [NETEASE_GUID, FALLBACK_GUID]
    assert elapsed >= 0.9


@pytest.mark.anyio
async def test_stream_fallback_not_triggered_for_midstream_range(monkeypatch):
    """4.8 Range: bytes=100- → 不触发回退（跨源偏移不通用）。"""
    p = importlib.import_module("proxy.app")
    calls: list[str] = []
    _patch_fetch(monkeypatch, {"items": [_item()]}, calls)
    monkeypatch.setattr(p, "_online_meta_for_request",
                        lambda *a: {"title": "晴天", "artist": "周杰伦"})

    async def fake_open(request, guid, range_header, **kwargs):
        return None

    monkeypatch.setattr(p, "_open_online_stream", fake_open)

    response = await p.stream_track(_request(headers={"range": "bytes=100-"}))

    assert response.status_code == 404
    assert calls == []


@pytest.mark.anyio
async def test_stream_fallback_not_triggered_for_ext_rendition(monkeypatch):
    """4.9 _ext_rendition=1 → 不再二次回退（防链式跳转）。"""
    p = importlib.import_module("proxy.app")
    calls: list[str] = []
    _patch_fetch(monkeypatch, {"items": [_item()]}, calls)
    monkeypatch.setattr(p, "_online_meta_for_request",
                        lambda *a: {"title": "晴天", "artist": "周杰伦"})

    async def fake_open(request, guid, range_header, **kwargs):
        return None

    monkeypatch.setattr(p, "_open_online_stream", fake_open)

    response = await p.stream_track(_request(query="_ext_rendition=1"))

    assert response.status_code == 404
    assert calls == []


@pytest.mark.anyio
async def test_stream_fallback_extends_deadline_only_once(monkeypatch):
    """4.10 回退 re-arm 一次后，其余候选全败 → deadline 不再延长、只搜一次。"""
    p = importlib.import_module("proxy.app")
    monkeypatch.setattr(p, "_STREAM_STARTUP_S", 0.6)
    monkeypatch.setitem(p.CONF, "stream_total_max_s", 30.0)
    calls: list[str] = []
    attempts: list[str] = []
    _patch_fetch(monkeypatch, {"items": [_item()]}, calls)
    monkeypatch.setattr(p, "_online_meta_for_request",
                        lambda *a: {"title": "晴天", "artist": "周杰伦"})

    alternative = _item("netease:456", title="晴天", duration_s=269.0)
    monkeypatch.setattr(p, "_retained_track",
                        lambda *a: (_item("netease:123", _alternatives=[alternative]), None))

    async def fake_open(request, guid, range_header, **kwargs):
        attempts.append(guid)
        if guid == NETEASE_GUID:
            await asyncio.sleep(0.45)   # 吃掉启动预算 → 触发 re-arm
        return None

    monkeypatch.setattr(p, "_open_online_stream", fake_open)

    response = await p.stream_track(_request())

    assert response.status_code == 404
    # 只搜一次：第二个网易候选不得再次触发回退（否则就是第二次 re-arm）
    assert calls == ["晴天 周杰伦"]
    # re-arm 生效：启动预算耗尽后，替代候选与回退候选仍被尝试
    assert attempts == [NETEASE_GUID, "online:netease:456", FALLBACK_GUID]


@pytest.mark.anyio
async def test_stream_fallback_searches_once_per_request(monkeypatch):
    """2.9 搜索"发起"即置位 → 一次请求最多搜一次（空结果也不重复搜）。"""
    p = importlib.import_module("proxy.app")
    calls: list[str] = []
    _patch_fetch(monkeypatch, {"items": []}, calls)
    monkeypatch.setattr(p, "_online_meta_for_request",
                        lambda *a: {"title": "晴天", "artist": "周杰伦"})

    async def fake_open(request, guid, range_header, **kwargs):
        return None

    monkeypatch.setattr(p, "_open_online_stream", fake_open)

    response = await p.stream_track(_request())

    assert response.status_code == 404
    assert calls == ["晴天 周杰伦"]


@pytest.mark.anyio
async def test_two_tracks_fallback_searches_do_not_supersede(monkeypatch):
    """1.9/spec：同一用户的两个不同曲目同时回退 → 互不 supersede，各得各的结果。"""
    p = importlib.import_module("proxy.app")
    hits: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        keyword = request.url.params.get("keyword")
        hits.append(keyword)
        await asyncio.sleep(0.1)
        if keyword == "晴天 周杰伦":
            return httpx.Response(200, json={"items": [_item("kuwo:1")]})
        return httpx.Response(200, json={"items": [_item("kuwo:9", title="夜曲")]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url="http://musicdl.test") as client:
        monkeypatch.setattr(p, "get_musicdl_client", lambda a: client)
        req = _request()
        first, second = await asyncio.gather(
            p._completion_search(req, NETEASE_GUID, "晴天", "周杰伦"),
            p._completion_search(req, NETEASE_GUID, "夜曲", "周杰伦"),
        )

    assert first == FALLBACK_GUID
    assert second == "online:kuwo:9"
    assert sorted(hits) == sorted(["晴天 周杰伦", "夜曲 周杰伦"])


# =========================================================================
# 2.8 端到端：失败 → 搜索 → 307 → 播放回退曲目
# =========================================================================
@pytest.mark.anyio
async def test_stream_fallback_redirect_then_play_fallback_track(monkeypatch):
    """2.8 NetEase 播放失败 → 搜索 → 307 → 回退曲目可播放。"""
    p = importlib.import_module("proxy.app")
    _patch_fetch(monkeypatch, {"items": [_item()]})
    monkeypatch.setattr(p, "_online_meta_for_request",
                        lambda *a: {"title": "晴天", "artist": "周杰伦"})

    async def fake_open(request, guid, range_header, **kwargs):
        if guid == NETEASE_GUID:
            return None
        return _opened(b"fallback-audio")

    monkeypatch.setattr(p, "_open_online_stream", fake_open)

    first = await p.stream_track(_request())
    assert first.status_code == 307
    location = first.headers["location"]
    assert "guid=online%3Akuwo%3A1" in location
    assert "_ext_rendition=1" in location

    second = await p.stream_track(_request(FALLBACK_GUID, query="_ext_rendition=1",
                                           headers={"range": "bytes=1-"}))
    assert second.status_code == 200
    body = b"".join([chunk async for chunk in second.body_iterator])
    assert body == b"fallback-audio"


# =========================================================================
# 3.3 元数据三态
# =========================================================================
@pytest.mark.anyio
async def test_stream_fallback_uses_metadata_title_artist(monkeypatch):
    """3.3 有 title/artist 时回退正常（关键词 = title + artist）。"""
    p = importlib.import_module("proxy.app")
    calls: list[str] = []
    _patch_fetch(monkeypatch, {"items": [_item()]}, calls)
    monkeypatch.setattr(p, "_online_meta_for_request",
                        lambda *a: {"title": "七里香", "artist": "周杰伦"})

    async def fake_open(request, guid, range_header, **kwargs):
        return None if guid == NETEASE_GUID else _opened()

    monkeypatch.setattr(p, "_open_online_stream", fake_open)

    response = await p.stream_track(_request())

    assert response.status_code == 307
    assert calls == ["七里香 周杰伦"]


@pytest.mark.anyio
async def test_stream_fallback_skipped_when_metadata_missing(monkeypatch):
    """3.3 元数据缺失时跳过回退（不盲搜）。"""
    p = importlib.import_module("proxy.app")
    calls: list[str] = []
    _patch_fetch(monkeypatch, {"items": [_item()]}, calls)
    monkeypatch.setattr(p, "_online_meta_for_request", lambda *a: None)

    async def fake_fetch_info(*a, **kw):
        return None

    monkeypatch.setattr(p, "_fetch_online_info", fake_fetch_info)

    async def fake_open(request, guid, range_header, **kwargs):
        return None

    monkeypatch.setattr(p, "_open_online_stream", fake_open)

    response = await p.stream_track(_request())

    assert response.status_code == 404
    assert calls == []


@pytest.mark.anyio
async def test_stream_fallback_disabled_by_switch(monkeypatch):
    """任务 1.4：总开关关闭时不发起补全搜索、返回 404，并打出 switch_off 诊断。"""
    p = importlib.import_module("proxy.app")
    monkeypatch.setitem(CONF, "fallback_enabled", False)
    calls: list[str] = []
    _patch_fetch(monkeypatch, {"items": [_item()]}, calls)
    monkeypatch.setattr(p, "_online_meta_for_request", lambda *a: {"title": "晴天", "artist": "周杰伦"})
    diags: list[tuple] = []
    monkeypatch.setattr(p, "fn_diag", lambda logger, event, **fields: diags.append((event, fields)))

    async def fake_open(request, guid, range_header, **kwargs):
        return None

    monkeypatch.setattr(p, "_open_online_stream", fake_open)

    response = await p.stream_track(_request())

    assert response.status_code == 404
    assert calls == []                                  # 未发起任何补全搜索
    assert ("completion_skip", {"guid": NETEASE_GUID, "reason": "switch_off",
                                "provider": "netease", "song_id": "123"}) in diags
    # 旧名同义别名并行一轮（字段与值相同）
    assert ("netease_fallback_skip", {"guid": NETEASE_GUID, "reason": "switch_off",
                                      "provider": "netease", "song_id": "123"}) in diags


@pytest.mark.anyio
async def test_stream_fallback_writes_completion_record(monkeypatch, tmp_path):
    """任务 1.4/1.6：补全命中要写账本记录（hit + 触发原因），且跳过分支也要写 skip 记录。"""
    p = importlib.import_module("proxy.app")
    ledger = tmp_path / "completion_log.json"
    monkeypatch.setenv("FNMUSIC_COMPLETION_LOG_FILE", str(ledger))
    monkeypatch.setattr(p.completion_log, "reset_for_tests", lambda: None)

    calls: list[str] = []
    _patch_fetch(monkeypatch, {"items": [_item()]}, calls)
    monkeypatch.setattr(p, "_online_meta_for_request",
                        lambda *a: {"title": "晴天", "artist": "周杰伦", "duration_s": 269.0})

    async def fake_open(request, guid, range_header, **kwargs):
        return None if guid == NETEASE_GUID else _opened()

    monkeypatch.setattr(p, "_open_online_stream", fake_open)
    response = await p.stream_track(_request())
    assert response.status_code == 307
    await asyncio.sleep(0.05)  # 等后台落盘任务完成

    import json as _json
    from proxy import completion_log as cl
    s = cl.summary(read_only=True)
    assert s["counts"]["hit"] >= 1
    rec = s["latest"]
    assert rec["event"] == "completion_hit" and rec["outcome"] == "hit"
    assert rec["provider"] == "netease" and rec["guid"] == NETEASE_GUID
    assert rec["fallback_guid"].startswith("online:kuwo:")
    assert rec["title"] == "晴天" and rec["artist"] == "周杰伦"     # 歌名/歌手入账
    assert isinstance(rec.get("elapsed_ms"), int)                  # 耗时入账

    # 关闭开关再播一次 → 应有 switch_off 的 skip 记录，且未发起搜索
    monkeypatch.setitem(CONF, "fallback_enabled", False)
    calls.clear()
    response = await p.stream_track(_request())
    assert response.status_code == 404 and calls == []
    await asyncio.sleep(0.05)
    s = cl.summary(read_only=True)
    assert s["counts"]["skip"] >= 1
    assert any(r["reason"] == "switch_off" for r in s["records"])


@pytest.mark.anyio
async def test_stream_fallback_metadata_from_neteasefree_client(monkeypatch):
    """3.3 neteasefree 为活跃客户端时 info 解析同样可用。"""
    p = importlib.import_module("proxy.app")
    calls: list[str] = []
    _patch_fetch(monkeypatch, {"items": [_item()]}, calls)
    monkeypatch.setattr(p, "_online_meta_for_request", lambda *a: None)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/info"):
            return httpx.Response(200, json={"ok": True, "data": {
                "name": "晴天", "dt": 269000,
                "ar": [{"name": "周杰伦"}], "al": {"name": "叶惠美", "picUrl": ""},
            }})
        raise AssertionError(f"unexpected upstream call: {request.url.path}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url="http://nf.test") as nf_client:
        monkeypatch.setattr(p, "_active_netease_client", lambda a: nf_client)

        async def fake_open(request, guid, range_header, **kwargs):
            return None if guid == NETEASE_GUID else _opened()

        monkeypatch.setattr(p, "_open_online_stream", fake_open)

        response = await p.stream_track(_request())

    assert response.status_code == 307
    assert calls == ["晴天 周杰伦"]


# =========================================================================
# 音源适配器注册表（change add-full-track-completion · 任务 1.1）
# =========================================================================
def test_completion_adapter_registry_resolves_netease():
    """网易注册为第一个音源适配器；未注册的音源不参与补全。"""
    p = importlib.import_module("proxy.app")
    adapter = p._completion_adapter_for(NETEASE_GUID)
    assert adapter is not None
    assert adapter.source == "netease"
    assert p._completion_adapter_for("online:kuwo:1") is None
    assert p._completion_adapter_for("online:bogus:1") is None


def test_completion_adapter_unavailability_reason_pin_dropped(monkeypatch):
    """pin 弃钉（pin[1]==0）时适配器给出 pin_dropped 原因。"""
    p = importlib.import_module("proxy.app")
    adapter = p._completion_adapter_for(NETEASE_GUID)
    assert adapter.unavailability_reason(NETEASE_GUID) is None
    p._NETEASE_URL_PIN["123"] = ("http://x", 0.0)
    try:
        assert adapter.unavailability_reason(NETEASE_GUID) == "pin_dropped"
    finally:
        p._NETEASE_URL_PIN.pop("123", None)
