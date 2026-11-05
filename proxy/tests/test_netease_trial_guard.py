"""网易试听判定单测（change add-netease-playback-fallback · 5B.3-5B.5）。

覆盖两条判定线：
- 字段判定（上游结构化字段优先）：空 url / code==404 / freeTrialInfo / 未登录且
  fee∉{0,8} → 丢；code -110 按 code/fee 分情况。
- 试听交叉校验（仅 freeTrialInfo 缺失时启用）：CL(size)×8/dt < 64 kbps → 判试听；
  全长直链 CL×8/dt == br → 放行。
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
MUSICBOX_DIR = ROOT / "musicbox-service"
NETEASEFREE_DIR = ROOT / "neteasefree-service"


def _load_module(name: str, path: Path, extra_path: Path | None = None):
    inserted = False
    if extra_path is not None and str(extra_path) not in sys.path:
        sys.path.insert(0, str(extra_path))
        inserted = True
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        if inserted and str(extra_path) in sys.path:
            sys.path.remove(str(extra_path))


# musicbox-service 的 netease_ext 是裸模块名（与 test_musicbox_service.py 共用）
netease_ext = sys.modules.get("netease_ext") or _load_module(
    "netease_ext", MUSICBOX_DIR / "netease_ext.py", MUSICBOX_DIR)
# neteasefree-service 的 netease_ext 同名但内容不同，用独立模块名加载
neteasefree_ext = _load_module(
    "neteasefree_ext", NETEASEFREE_DIR / "netease_ext.py", NETEASEFREE_DIR)


def _musicbox_api(monkeypatch, rows, logged_in: bool = False):
    class _Api:
        def songs_url(self, ids):
            return rows

    monkeypatch.setattr(netease_ext, "_get_api_locked", lambda: _Api())
    monkeypatch.setattr(netease_ext, "check_is_logged_in", lambda: logged_in)
    # 取链有进程内短缓存，跨用例会串味
    netease_ext._url_cache.clear()


def _neteasefree_api(monkeypatch, rows):
    monkeypatch.setattr(neteasefree_ext.api, "song_urls",
                        lambda ids, level: rows)


def _fake_nembox_config(monkeypatch):
    """NEMbox.config 的最小替身（取链会临时改写 music_quality）。"""
    import types

    class _Config:
        def __init__(self):
            self.config = {}

        def get(self, key):
            return self.config.get(key, {}).get("value")

    module = types.ModuleType("NEMbox.config")
    module.Config = _Config
    package = types.ModuleType("NEMbox")
    package.config = module
    monkeypatch.setitem(sys.modules, "NEMbox", package)
    monkeypatch.setitem(sys.modules, "NEMbox.config", module)


# =========================================================================
# 5B.3 HEAD 交叉校验单测
# =========================================================================
def test_cross_check_flags_low_bitrate_trial():
    """45s 试听片段（size=720813, br=128008，元数据 269s）→ 判试听。"""
    assert netease_ext.cross_check_trial_bitrate(720813, 269000, 128008) is True
    assert neteasefree_ext.cross_check_trial_bitrate(720813, 269000, 128008) is True


def test_cross_check_flags_trial_without_reported_bitrate():
    """没有 br 时只看 CL×8/dt：低于 64 kbps 仍判试听。"""
    assert netease_ext.cross_check_trial_bitrate(720813, 269000, None) is True


def test_cross_check_passes_full_length_fee8_stream():
    """fee=8 全长直链：CL×8/dt == br（320 kbps）→ 放行。"""
    duration_ms = 269000
    size = int(320000 * (duration_ms / 1000.0) / 8)
    assert netease_ext.cross_check_trial_bitrate(size, duration_ms, 320000) is False
    assert neteasefree_ext.cross_check_trial_bitrate(size, duration_ms, 320000) is False


def test_cross_check_neutral_without_size_or_duration():
    """size / dt 缺失 → 不判定（放行给字段结论）。"""
    assert netease_ext.cross_check_trial_bitrate(None, 269000, 128008) is False
    assert netease_ext.cross_check_trial_bitrate(720813, 0, 128008) is False
    assert netease_ext.cross_check_trial_bitrate(720813, None, 128008) is False


def test_cross_check_keeps_normal_quality_stream():
    """128 kbps 全长流（CL×8/dt == 128000）→ 放行。"""
    duration_ms = 200000
    size = int(128000 * (duration_ms / 1000.0) / 8)
    assert netease_ext.cross_check_trial_bitrate(size, duration_ms, 128000) is False


def test_filter_applies_cross_check_only_with_duration(monkeypatch):
    """给了元数据时长才做交叉校验；字段说可播但码率过低 → 丢。"""
    rows = [
        # 低码率试听（无 freeTrialInfo）：size×8/dt ≈ 21 kbps
        {"id": 1, "url": "http://cdn.test/a.mp3", "code": 200, "fee": 8,
         "size": 720813, "br": 128008},
        # 全长 320k 流
        {"id": 2, "url": "http://cdn.test/b.mp3", "code": 200, "fee": 8,
         "size": 320000 * 269 // 8, "br": 320000},
    ]
    _musicbox_api(monkeypatch, rows, logged_in=False)

    # 不给时长 → 只用字段结论，两条都留
    assert netease_ext.filter_playable_song_ids([1, 2]) == {1, 2}
    # 给了时长 → 低码率那条按试听丢，全长那条留
    assert netease_ext.filter_playable_song_ids([1, 2], {1: 269000, 2: 269000}) == {2}


def test_neteasefree_filter_applies_cross_check_only_with_duration(monkeypatch):
    """neteasefree 同样：给时长才做交叉校验。"""
    rows = [
        {"id": 1, "url": "http://cdn.test/a.mp3", "code": 200,
         "size": 720813, "br": 128008},
        {"id": 2, "url": "http://cdn.test/b.mp3", "code": 200,
         "size": 320000 * 269 // 8, "br": 320000},
    ]
    _neteasefree_api(monkeypatch, rows)

    assert neteasefree_ext.filter_playable_song_ids([1, 2]) == {1, 2}
    assert neteasefree_ext.filter_playable_song_ids([1, 2], {1: 269000, 2: 269000}) == {2}


# =========================================================================
# 5B.4 code -110：按 code / fee 分情况判定
# =========================================================================
def test_musicbox_code_minus_110_decided_by_fee_when_logged_out(monkeypatch):
    """未登录：code=-110 有直链时，fee∉{0,8} → 丢；fee∈{0,8} → 留。"""
    rows = [
        {"id": 1, "url": "", "code": -110, "fee": 1},
        {"id": 2, "url": "http://cdn.test/vip.mp3", "code": -110, "fee": 1},
        {"id": 3, "url": "http://cdn.test/free.mp3", "code": -110, "fee": 0},
        {"id": 4, "url": "http://cdn.test/low.mp3", "code": -110, "fee": 8},
    ]
    _musicbox_api(monkeypatch, rows, logged_in=False)

    playable = netease_ext.filter_playable_song_ids([1, 2, 3, 4])

    assert playable == {3, 4}


def test_musicbox_code_minus_110_kept_when_logged_in(monkeypatch):
    """已登录：code=-110 且有直链、无 freeTrialInfo → 留（账号有权益）。"""
    rows = [
        {"id": 5, "url": "http://cdn.test/vip.mp3", "code": -110, "fee": 1},
    ]
    _musicbox_api(monkeypatch, rows, logged_in=True)

    assert netease_ext.filter_playable_song_ids([5]) == {5}


def test_musicbox_code_404_and_empty_url_dropped(monkeypatch):
    """code==404 / 空 url → 无论 fee 都丢。"""
    rows = [
        {"id": 6, "url": "http://cdn.test/x.mp3", "code": 404, "fee": 0},
        {"id": 7, "url": "", "code": 200, "fee": 0},
    ]
    _musicbox_api(monkeypatch, rows, logged_in=False)

    assert netease_ext.filter_playable_song_ids([6, 7]) == set()


def test_neteasefree_code_minus_110_kept_without_fee_filter(monkeypatch):
    """neteasefree：后端自带 VIP，不按 fee 过滤；code=-110 有直链 → 留。"""
    rows = [
        {"id": 1, "url": "http://cdn.test/vip.mp3", "code": -110, "fee": 1},
        {"id": 2, "url": "", "code": -110, "fee": 1},
    ]
    _neteasefree_api(monkeypatch, rows)

    assert neteasefree_ext.filter_playable_song_ids([1, 2]) == {1}


# =========================================================================
# 5B.5 freeTrialInfo 存在时，交叉校验不覆盖字段判定
# =========================================================================
def test_free_trial_info_field_decision_wins_over_cross_check(monkeypatch):
    """上游给了 freeTrialInfo → 字段判定直接丢；交叉校验不参与、也不覆盖。"""
    row = {"id": 1, "url": "http://cdn.test/trial.mp3", "code": 200, "fee": 1,
           "freeTrialInfo": {"start": 0, "end": 30},
           "size": 10_760_000, "br": 320000}
    _musicbox_api(monkeypatch, [row], logged_in=True)

    assert netease_ext.filter_playable_song_ids([1]) == set()
    # 同一行若按码率看像全长 → 交叉校验也不判试听（字段结论已定）
    assert netease_ext.cross_check_trial_bitrate(row["size"], 269000, row["br"]) is False


def test_neteasefree_free_trial_info_dropped(monkeypatch):
    """neteasefree：带 freeTrialInfo 的行一律丢（与码率无关）。"""
    rows = [
        {"id": 1, "url": "http://cdn.test/trial.mp3", "code": 200,
         "freeTrialInfo": {"start": 0, "end": 30}, "size": 10_760_000, "br": 320000},
    ]
    _neteasefree_api(monkeypatch, rows)

    assert neteasefree_ext.filter_playable_song_ids([1]) == set()


# =========================================================================
# 播放链路的试听守卫（CT201 实测暴露：VIP 未登录时上游回 30s 试听片段，
# code=200 + url 非空 → 代理当全曲交付，用户听到"歌突然结束"）
# =========================================================================
def test_musicbox_get_song_url_rejects_trial(monkeypatch):
    """musicbox 取链：freeTrialInfo 明示试听 → 判不可播（url=None）。"""
    row = {"id": 1, "url": "http://cdn.test/trial.mp3", "code": 200,
           "freeTrialInfo": {"start": 0, "end": 30}, "size": 480813, "br": 128012}
    _musicbox_api(monkeypatch, [row], logged_in=False)
    _fake_nembox_config(monkeypatch)

    item = netease_ext.get_song_url(1, "exhigh")
    assert item is not None
    assert item.get("url") is None
    assert item.get("code") == 404
    assert item.get("trial_rejected") is True


def test_musicbox_playback_resolve_rejects_trial(monkeypatch):
    """musicbox 播放解析（服务实际走的路径）：试听片段判不可播。

    未登录账号取 VIP 歌时上游回 code=200 + url + freeTrialInfo（30s 片段），
    不拦就会把试听当全曲交付（CT201 实测）。
    """
    playback = _load_module("musicbox_playback", MUSICBOX_DIR / "playback.py")
    trial_row = {"id": 7, "url": "http://cdn.test/trial.mp3", "code": 200,
                 "freeTrialInfo": {"start": 0, "end": 30}, "size": 480813, "br": 128012}

    async def _fake_request(song_id, quality, generation):
        return dict(trial_row)

    monkeypatch.setattr(playback, "_request", _fake_request)

    import asyncio

    item = asyncio.run(playback._resolve(7, "exhigh", 0, "playback", 0))
    assert item.get("url") is None
    assert item.get("code") == 404
    assert item.get("trial_rejected") is True


def test_neteasefree_get_song_url_rejects_trial(monkeypatch):
    """neteasefree 取链：freeTrialInfo 明示试听 → 判不可播（url=None）。"""
    row = {"id": 1, "url": "http://cdn.test/trial.mp3", "code": 200,
           "freeTrialInfo": {"start": 0, "end": 30}, "size": 480813, "br": 128012}

    class _Api:
        def song_url(self, song_id, level):
            return row

    monkeypatch.setattr(neteasefree_ext.api, "song_url",
                        lambda song_id, level: row)

    item = neteasefree_ext.get_song_url(1, "exhigh")
    assert item is not None
    assert item.get("url") is None
    assert item.get("code") == 404
    assert item.get("trial_rejected") is True


def test_full_length_row_still_resolves(monkeypatch):
    """无 freeTrialInfo 的全长直链照常放行（守卫不能误杀全曲）。"""
    row = {"id": 1, "url": "http://cdn.test/full.mp3", "code": 200,
           "fee": 8, "size": 11_000_000, "br": 320000}
    _musicbox_api(monkeypatch, [row], logged_in=True)
    _fake_nembox_config(monkeypatch)

    item = netease_ext.get_song_url(1, "exhigh")
    assert item is not None
    assert item.get("url") == "http://cdn.test/full.mp3"
    assert item.get("code") == 200
    assert "trial_rejected" not in item


@pytest.mark.anyio
async def test_proxy_resolve_rejects_trial_response(monkeypatch):
    """代理侧兜底：上游回 code=200 + url + freeTrialInfo → 判不可播（交回退）。"""
    import httpx

    from proxy.app import CONF, _NETEASE_URL_PIN, _resolve_netease_url_uncached

    _NETEASE_URL_PIN.pop("991122", None)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        q = str(request.url.params.get("quality"))
        seen.append(q)
        return httpx.Response(200, json={"ok": True, "data": {
            "code": 200, "url": "http://cdn.test/trial.mp3",
            "freeTrialInfo": {"start": 0, "end": 30},
            "size": 480813, "br": 128012, "type": "mp3"}})

    monkeypatch.setitem(CONF, "play_quality", "exhigh")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url="http://127.0.0.1:8770") as client:
        result = await _resolve_netease_url_uncached(client, "991122", fresh=True)

    assert result is None
    assert seen, "至少请求过一档音质"
    _NETEASE_URL_PIN.pop("991122", None)
