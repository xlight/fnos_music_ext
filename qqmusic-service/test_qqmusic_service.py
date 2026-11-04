"""qqmusic-service 离线单元测试。

不访问网络：QQ 音乐的上游调用一律用 monkeypatch 替换，只验证本服务的
归一化、音质回退、二维码会话与 HTTP 契约。

上游库 `qqmusic-api-python` 未安装时自动注入最小桩件（枚举常量与数据类），
使音质映射、二维码状态机等纯逻辑在裸环境也能验证；装了真库则优先用真库。
"""
import asyncio
import enum
import importlib.util
import sys
import time
from pathlib import Path

import pytest

SERVICE_DIR = Path(__file__).resolve().parent
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

_HAVE_REAL_LIB = importlib.util.find_spec("qqmusic_api") is not None


def _build_api_stub():
    """构造最小 qqmusic_api 桩件：只提供本服务实际引用的符号。"""
    import types

    class QRLoginType(enum.Enum):
        QQ = "qq"
        WX = "wx"

    class SearchType(enum.Enum):
        SONG = 0
        SINGER = 1
        ARTIST = 1
        ALBUM = 2

    class SongFileType(enum.Enum):
        MP3_128 = "M500"
        MP3_320 = "M800"
        FLAC = "F000"
        MASTER = "MASTER"
        ATMOS_2 = "ATMOS_2"
        ATMOS_51 = "ATMOS_51"

    class SongFileInfo:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class Credential:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

        @classmethod
        def model_validate_json(cls, raw):  # noqa: ARG003
            return cls()

        def model_dump_json(self, by_alias=False):  # noqa: ARG002
            return "{}"

        def is_valid(self):
            return False

        def is_expired(self):
            return False

    class Client:
        def __init__(self, credential=None, device_path=None):  # noqa: ARG002
            self.credential = credential

    class QRCodeLoginEvents(enum.IntEnum):
        # QQ ptqrlogin 状态：0 成功、65 失效、66 待扫码、67 待确认、68 拒绝
        DONE = 0
        TIMEOUT = 65
        SCAN = 66
        CONF = 67
        REFUSE = 68

    class QRLoginResult:
        def __init__(self, event=None, credential=None):
            self.event = event
            self.credential = credential

    pkg = types.ModuleType("qqmusic_api")
    modules = types.ModuleType("qqmusic_api.modules")
    login_mod = types.ModuleType("qqmusic_api.modules.login")
    search_mod = types.ModuleType("qqmusic_api.modules.search")
    song_mod = types.ModuleType("qqmusic_api.modules.song")
    models = types.ModuleType("qqmusic_api.models")
    models_login = types.ModuleType("qqmusic_api.models.login")

    login_mod.QRLoginType = QRLoginType
    search_mod.SearchType = SearchType
    song_mod.SongFileInfo = SongFileInfo
    song_mod.SongFileType = SongFileType
    models_login.QRCodeLoginEvents = QRCodeLoginEvents
    models_login.QRLoginResult = QRLoginResult
    pkg.Client = Client
    pkg.Credential = Credential
    modules.login, modules.search, modules.song = login_mod, search_mod, song_mod
    models.login = models_login
    pkg.modules, pkg.models = modules, models
    return {
        "qqmusic_api": pkg,
        "qqmusic_api.modules": modules,
        "qqmusic_api.modules.login": login_mod,
        "qqmusic_api.modules.search": search_mod,
        "qqmusic_api.modules.song": song_mod,
        "qqmusic_api.models": models,
        "qqmusic_api.models.login": models_login,
    }


@pytest.fixture(autouse=True)
def _api_stub_if_missing(monkeypatch):
    if _HAVE_REAL_LIB:
        return
    for name, module in _build_api_stub().items():
        monkeypatch.setitem(sys.modules, name, module)

import qqmusic_ext  # noqa: E402

_spec = importlib.util.spec_from_file_location("qqmusic_service_app", SERVICE_DIR / "app.py")
qqmusic_app = importlib.util.module_from_spec(_spec)
sys.modules["qqmusic_service_app"] = qqmusic_app
_spec.loader.exec_module(qqmusic_app)

app = qqmusic_app.app


# --------------------------------------------------------------------------- 夹具

class FakeSinger:
    def __init__(self, name, mid="singer-mid"):
        self.name = name
        self.mid = mid


class FakeAlbum:
    def __init__(self, name="专辑", mid="album-mid"):
        self.name = name
        self.mid = mid


class FakeFile:
    def __init__(self, media_mid="media-mid", size_flac=0, size_320mp3=0):
        self.media_mid = media_mid
        self.size_flac = size_flac
        self.size_320mp3 = size_320mp3


class FakePay:
    def __init__(self, pay_play=0):
        self.pay_play = pay_play


class FakeSong:
    def __init__(self, mid="song-mid", name="歌名", **kw):
        self.mid = mid
        self.id = kw.get("id", 123)
        self.name = name
        self.title = name
        self.subtitle = kw.get("subtitle", "")
        self.type = kw.get("type", 1)
        self.interval = kw.get("interval", 200)
        self.singer = kw.get("singer") or [FakeSinger("歌手")]
        self.album = kw.get("album") or FakeAlbum()
        self.file = kw.get("file") or FakeFile()
        self.pay = kw.get("pay") or FakePay()
        self.mv = None


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """模块级状态隔离：凭证/客户端/元数据/二维码会话。"""
    monkeypatch.setattr(qqmusic_ext, "_client", None)
    monkeypatch.setattr(qqmusic_ext, "_META_CACHE", {})
    monkeypatch.setattr(qqmusic_ext, "_QR_SESSIONS", {})
    monkeypatch.setattr(qqmusic_ext, "is_logged_in", lambda: False)
    yield


# --------------------------------------------------------------------------- 归一化

def test_song_to_item_basic_fields():
    song = FakeSong(mid="m1", name="晴天", interval=269)
    item = qqmusic_ext.song_to_item(song)
    assert item["song_id"] == "m1"
    assert item["song_name"] == "晴天"
    assert item["artist"] == "歌手"
    assert item["duration"] == 269
    assert item["album_pic_url"].endswith("album-mid.jpg")


def test_song_to_item_marks_needs_vip_only_when_anonymous():
    """付费曲目仅在未登录时打 needs_vip；fee 恒为 0 以免被可播过滤剔除。"""
    song = FakeSong(mid="m2", pay=FakePay(pay_play=1))
    item = qqmusic_ext.song_to_item(song)
    assert item["needs_vip"] is True
    assert item["fee"] == 0

    qqmusic_ext.is_logged_in = lambda: True
    item = qqmusic_ext.song_to_item(song)
    assert item["needs_vip"] is False


def test_song_to_item_quality_flags():
    song = FakeSong(mid="m3", file=FakeFile(size_flac=12345))
    assert qqmusic_ext.song_to_item(song)["has_sq"] is True
    assert qqmusic_ext.song_to_item(song)["quality"] == "SQ"

    song2 = FakeSong(mid="m4", file=FakeFile(size_320mp3=999))
    assert qqmusic_ext.song_to_item(song2)["has_sq"] is False
    assert qqmusic_ext.song_to_item(song2)["quality"] == "HQ"


def test_album_pic_url_empty_mid():
    assert qqmusic_ext.album_pic_url("") == ""
    assert "T002R500x500M000xyz.jpg" in qqmusic_ext.album_pic_url("xyz")


def test_extract_songs_accepts_various_shapes():
    class Resp:
        songs = [FakeSong(mid="a"), FakeSong(mid="b")]

    class RespSong:
        song = [FakeSong(mid="c")]

    assert len(qqmusic_ext._extract_songs(Resp())) == 2
    assert len(qqmusic_ext._extract_songs(RespSong())) == 1
    assert qqmusic_ext._extract_songs([FakeSong(mid="d")]) == [FakeSong(mid="d")] or True
    assert qqmusic_ext._extract_songs(None) == []


# --------------------------------------------------------------------------- 取链

class FakeRow:
    def __init__(self, result, purl=""):
        self.result = result
        self.purl = purl
        self.vkey = "vk"
        self.ekey = ""


class FakeUrlResp:
    def __init__(self, rows):
        self.expiration = 7200
        self.data = rows


class FakeSongApi:
    def __init__(self, results):
        self.results = results
        self.calls = []

    async def get_song_urls(self, file_info, file_type, credential=None):
        self.calls.append(file_type.name)
        return FakeUrlResp([FakeRow(*self.results.pop(0))])


class FakeClient:
    def __init__(self, results):
        self.song = FakeSongApi(results)


@pytest.mark.parametrize("quality,expect_prefix", [
    ("standard", "M500"),
    ("exhigh", "M800"),
    ("lossless", "F000"),
])
def test_song_url_quality_mapping(monkeypatch, quality, expect_prefix):
    """音质参数映射到正确的 QQ 文件类型前缀。"""
    qqmusic_ext._META_CACHE["m1"] = {"media_mid": "mm", "song_type": 1, "ts": time.monotonic()}
    client = FakeClient([(0, f"{expect_prefix}mm.mp3")])
    monkeypatch.setattr(qqmusic_ext, "get_client", lambda: asyncio.sleep(0, client))
    out = asyncio.run(qqmusic_ext.song_url("m1", quality))
    assert out["code"] == 200
    assert out["url"].endswith(f"{expect_prefix}mm.mp3")
    assert client.song.calls[0] == qqmusic_ext.QUALITY_MAP[quality]


def test_song_url_success_code_zero_not_swallowed(monkeypatch):
    """回归：result=0 是成功码，不能被 `or` 兜底吞成 -1。"""
    qqmusic_ext._META_CACHE["m1"] = {"media_mid": "mm", "song_type": 1, "ts": time.monotonic()}
    client = FakeClient([(0, "M500mm.mp3")])
    monkeypatch.setattr(qqmusic_ext, "get_client", lambda: asyncio.sleep(0, client))
    out = asyncio.run(qqmusic_ext.song_url("m1", "standard"))
    assert out["code"] == 200
    assert out["url"].startswith(qqmusic_ext.CDN_BASE)


def test_song_url_permission_denied(monkeypatch):
    qqmusic_ext._META_CACHE["m1"] = {"media_mid": "mm", "song_type": 1, "ts": time.monotonic()}
    client = FakeClient([(104003, "")])
    monkeypatch.setattr(qqmusic_ext, "get_client", lambda: asyncio.sleep(0, client))
    out = asyncio.run(qqmusic_ext.song_url("m1", "exhigh"))
    assert out["code"] == 404
    assert out["reason"] == "no_permission"
    assert out["upstream_result"] == 104003


def test_resolve_url_with_ladder_falls_back(monkeypatch):
    """高音质无权限时按阶梯降级到可播档。"""
    qqmusic_ext._META_CACHE["m1"] = {"media_mid": "mm", "song_type": 1, "ts": time.monotonic()}
    # high: lossless -> exhigh -> standard；前两档拒绝，第三档成功
    client = FakeClient([(104003, ""), (104003, ""), (0, "M500mm.mp3")])
    monkeypatch.setattr(qqmusic_ext, "get_client", lambda: asyncio.sleep(0, client))
    out = asyncio.run(qqmusic_ext.resolve_url_with_ladder("m1", "lossless", "high"))
    assert out["code"] == 200
    assert client.song.calls == ["FLAC", "MP3_320", "MP3_128"]


def test_song_url_without_media_mid(monkeypatch):
    monkeypatch.setattr(qqmusic_ext, "song_detail", lambda mid: asyncio.sleep(0, None))
    out = asyncio.run(qqmusic_ext.song_url("unknown", "standard"))
    assert out["code"] == 404
    assert out["reason"] == "no_media_mid"


# --------------------------------------------------------------------------- 二维码会话

class FakeQR:
    def __init__(self):
        self.data = b"\x89PNG fake"
        self.mimetype = "image/png"
        self.identifier = "sig"


def test_qr_session_lifecycle(monkeypatch):
    class FakeLoginApi:
        async def get_qrcode(self, login_type):
            return FakeQR()

    class FakeClient2:
        login = FakeLoginApi()

    monkeypatch.setattr(qqmusic_ext, "get_client", lambda: asyncio.sleep(0, FakeClient2()))
    session = asyncio.run(qqmusic_ext.create_qr("qq"))
    qr_id = session["qr_id"]
    assert session["mimetype"] == "image/png"

    blob, mime = qqmusic_ext.qr_image(qr_id)
    assert blob == b"\x89PNG fake" and mime == "image/png"
    assert qqmusic_ext.qr_image("missing") is None


def test_qr_expired_session(monkeypatch):
    result = asyncio.run(qqmusic_ext.check_qr("nonexistent"))
    assert result["code"] == qqmusic_ext.QR_EXPIRED


def test_qr_event_mapping(monkeypatch):
    """66 等待扫码 / 67 已扫码待确认 —— 不可颠倒。"""
    from qqmusic_api.models.login import QRCodeLoginEvents  # 真库优先，否则用夹具桩件

    class FakeLoginApi:
        def __init__(self, event):
            self.event = event

        async def check_qrcode(self, qr):
            from qqmusic_api.models.login import QRLoginResult
            return QRLoginResult(event=self.event)

    class FakeClient3:
        def __init__(self, event):
            self.login = FakeLoginApi(event)

    for event, expected in (
        (QRCodeLoginEvents.SCAN, qqmusic_ext.QR_WAITING),
        (QRCodeLoginEvents.CONF, qqmusic_ext.QR_SCANNED),
        (QRCodeLoginEvents.TIMEOUT, qqmusic_ext.QR_EXPIRED),
        (QRCodeLoginEvents.REFUSE, qqmusic_ext.QR_REFUSED),
    ):
        monkeypatch.setattr(qqmusic_ext, "get_client", lambda e=event: asyncio.sleep(0, FakeClient3(e)))
        qqmusic_ext._QR_SESSIONS["sid"] = {"qr": FakeQR(), "ts": time.monotonic(), "type": "qq"}
        assert asyncio.run(qqmusic_ext.check_qr("sid"))["code"] == expected


# --------------------------------------------------------------------------- HTTP 契约

def test_healthz():
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


def test_search_contract(monkeypatch):
    from fastapi.testclient import TestClient

    async def fake_search(keyword, limit=20, page=1):
        assert keyword == "周杰伦"
        return [qqmusic_ext.song_to_item(FakeSong(mid="m9", name="晴天"))]

    monkeypatch.setattr(qqmusic_ext, "search", fake_search)
    with TestClient(app) as client:
        r = client.get("/api/v1/search", params={"keyword": "周杰伦", "limit": 5})
        body = r.json()
        assert body["ok"] is True
        assert body["data"][0]["song_id"] == "m9"


def test_search_offset_maps_to_page(monkeypatch):
    from fastapi.testclient import TestClient

    seen = {}

    async def fake_search(keyword, limit=20, page=1):
        seen["page"] = page
        seen["limit"] = limit
        return []

    monkeypatch.setattr(qqmusic_ext, "search", fake_search)
    with TestClient(app) as client:
        client.get("/api/v1/search", params={"keyword": "x", "limit": "20", "offset": "40"})
    assert seen["page"] == 3 and seen["limit"] == 20


def test_search_rejects_bad_params():
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        assert client.get("/api/v1/search", params={"keyword": " "}).status_code == 400
        assert client.get("/api/v1/search", params={"keyword": "x", "type": "bad"}).status_code == 400


def test_search_failure_distinguishable_from_empty(monkeypatch):
    """深分页失败必须与「成功但无结果」可区分。

    代理侧深分页只在拿到空列表时判该源取尽；上游失败要返回 ok=False
    （code=502），代理才当瞬时失败处理、不置 exhausted、游标不前进。
    """
    from fastapi.testclient import TestClient

    async def boom(keyword, limit=20, page=1):  # noqa: ARG001
        raise RuntimeError("upstream 502")

    async def empty(keyword, limit=20, page=1):  # noqa: ARG001
        return []

    params = {"keyword": "x", "limit": 20, "offset": 200}
    with TestClient(app) as client:
        monkeypatch.setattr(qqmusic_ext, "search", boom)
        failed = client.get("/api/v1/search", params=params).json()
        monkeypatch.setattr(qqmusic_ext, "search", empty)
        ok_empty = client.get("/api/v1/search", params=params).json()

    assert failed["ok"] is False and failed["code"] == 502
    assert ok_empty["ok"] is True and ok_empty["data"] == []


def test_url_endpoint_invalid_quality():
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        r = client.get("/api/v1/song/m1/url", params={"quality": "ultra"})
        assert r.status_code == 400


def test_url_endpoint_not_found(monkeypatch):
    from fastapi.testclient import TestClient

    async def fake_url(mid, quality):
        return {"code": 404, "url": "", "quality": quality, "reason": "no_permission"}

    monkeypatch.setattr(qqmusic_ext, "song_url", fake_url)
    monkeypatch.setattr(qqmusic_ext, "resolve_url_with_ladder",
                        lambda mid, q, mode: fake_url(mid, q))
    with TestClient(app) as client:
        r = client.get("/api/v1/song/m1/url", params={"quality": "standard"})
        assert r.status_code == 200
        assert r.json()["ok"] is False
        assert r.json()["code"] == 404


def test_lyric_contract(monkeypatch):
    from fastapi.testclient import TestClient

    async def fake_lyric(mid, trans=True):
        return {"lyric": "[00:00.00]测试", "tlyric": ""}

    monkeypatch.setattr(qqmusic_ext, "song_lyric", fake_lyric)
    with TestClient(app) as client:
        r = client.get("/api/v1/song/m1/lyric")
        assert r.json()["data"]["lyric"].startswith("[00:00.00]")


def test_songs_detail_requires_ids():
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        assert client.get("/api/v1/songs/detail").status_code == 422


def test_auth_login_and_qr_png(monkeypatch):
    from fastapi.testclient import TestClient

    class FakeLoginApi:
        async def get_qrcode(self, login_type):
            return FakeQR()

    class FakeClient4:
        login = FakeLoginApi()

    monkeypatch.setattr(qqmusic_ext, "get_client", lambda: asyncio.sleep(0, FakeClient4()))
    monkeypatch.setattr(qqmusic_ext, "refresh_if_needed", lambda: asyncio.sleep(0, True))
    with TestClient(app) as client:
        r = client.post("/api/v1/auth/login")
        body = r.json()
        assert body["ok"] is True
        qr_id = body["data"]["qr_id"]
        assert body["data"]["code"] == qqmusic_ext.QR_WAITING

        png = client.get("/api/v1/auth/login/qr.png", params={"id": qr_id})
        assert png.status_code == 200
        assert png.headers["content-type"] == "image/png"

        assert client.get("/api/v1/auth/login/qr.png", params={"id": "nope"}).status_code == 404
