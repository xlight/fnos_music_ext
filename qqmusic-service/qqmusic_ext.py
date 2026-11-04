"""QQ 音乐音源封装（基于 qqmusic-api-python，上游 L-1124/QQMusicApi）。

对 fnmusic-ext 暴露与 musicbox-service 对齐的数据形状：
以歌曲 mid 作为 song_id，字段沿用 song_name/artist/album_name/duration/
album_pic_url 等命名，让 proxy 的搜索与详情链路可直接复用。

登录态（Credential）持久化在数据目录的 credential.json，扫码成功后写盘；
设备信息同目录 device.json 保持固定，避免重启被判为新设备。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger("qqmusic_service.ext")

# --------------------------------------------------------------------------- 数据目录

_FALLBACK_DATA_DIR = Path(os.path.expanduser("~/.local/share")) / "qqmusic"


def data_dir() -> Path:
    """凭证与设备信息的持久化目录。

    优先 QQMUSIC_DATA_DIR（容器/原生部署显式注入），其次 XDG_DATA_HOME/qqmusic。
    """
    override = (os.environ.get("QQMUSIC_DATA_DIR") or "").strip()
    if override:
        return Path(override)
    xdg = (os.environ.get("XDG_DATA_HOME") or "").strip()
    if xdg:
        return Path(xdg) / "qqmusic"
    return _FALLBACK_DATA_DIR


def ensure_dirs() -> Path:
    d = data_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
    except OSError as exc:  # 只读环境下降级：仅内存态
        logger.warning("qqmusic data dir unavailable (%s): %s", d, exc)
    return d


def _credential_path() -> Path:
    return data_dir() / "credential.json"


def _device_path() -> Path:
    return data_dir() / "device.json"


# --------------------------------------------------------------------------- 客户端

_client: Any = None
_client_lock = asyncio.Lock()


def _import_api():
    """延迟导入：库缺失时服务仍能启动并以 503 暴露，而非 import 即崩。"""
    from qqmusic_api import Client, Credential  # noqa: PLC0415
    from qqmusic_api.modules.login import QRLoginType  # noqa: PLC0415
    from qqmusic_api.modules.search import SearchType  # noqa: PLC0415
    from qqmusic_api.modules.song import SongFileInfo, SongFileType  # noqa: PLC0415

    return Client, Credential, QRLoginType, SearchType, SongFileInfo, SongFileType


def load_credential() -> Any:
    """读盘凭证；缺失或损坏返回空凭证（未登录）。"""
    _, Credential, _, _, _, _ = _import_api()
    path = _credential_path()
    try:
        if path.exists():
            raw = path.read_text(encoding="utf-8")
            if raw.strip():
                return Credential.model_validate_json(raw)
    except Exception as exc:  # noqa: BLE001  凭证损坏不应阻塞启动
        logger.warning("load credential failed: %s", exc)
    return Credential()


def save_credential(credential: Any) -> None:
    """凭证写盘（0600）：登录成功与刷新后调用。"""
    path = _credential_path()
    try:
        ensure_dirs()
        payload = credential.model_dump_json(by_alias=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("save credential failed: %s", exc)


def clear_credential() -> None:
    try:
        path = _credential_path()
        if path.exists():
            path.unlink()
    except OSError as exc:
        logger.warning("clear credential failed: %s", exc)


async def get_client() -> Any:
    """常驻 Client 单例（持有连接池与设备身份）。"""
    global _client
    if _client is not None:
        return _client
    async with _client_lock:
        if _client is None:
            Client, _, _, _, _, _ = _import_api()
            ensure_dirs()
            _client = Client(
                credential=load_credential(),
                device_path=str(_device_path()),
            )
    return _client


def reset_client(reason: str = "") -> None:
    """丢弃常驻实例：登录态变更/上游异常重建时调用。"""
    global _client
    _client = None
    logger.info("qqmusic client reset: %s", reason)


def current_credential() -> Any:
    if _client is None:
        return load_credential()
    return _client.credential


def is_logged_in() -> bool:
    cred = current_credential()
    try:
        return bool(cred.is_valid())
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- 音质

# proxy 的 netease_quality 取值 → QQ 音乐文件类型（.s 前缀 + 扩展名）
QUALITY_MAP: dict[str, str] = {
    "hires": "MASTER",
    "lossless": "FLAC",
    "exhigh": "MP3_320",
    "higher": "MP3_320",
    "standard": "MP3_128",
}

# 由高到低的兜底阶梯：高音质无权限时逐级回退
QUALITY_LADDER: dict[str, list[str]] = {
    "high": ["lossless", "exhigh", "standard"],
    "balanced": ["exhigh", "standard"],
    "smooth": ["standard"],
}


def _file_type(name: str) -> Any:
    _, _, _, _, _, SongFileType = _import_api()
    return getattr(SongFileType, name, SongFileType.MP3_128)


def _is_lossless(name: str) -> bool:
    return name in ("FLAC", "MASTER", "ATMOS_2", "ATMOS_51")


CDN_BASE = "https://isure.stream.qqmusic.qq.com/"


# --------------------------------------------------------------------------- 元数据缓存

# mid -> {media_mid, song_type, ...}：取链需要 media_mid，搜索时顺带填充，
# 未命中再用详情接口补齐，避免每次取链都多一次详情往返。
_META_CACHE: dict[str, dict[str, Any]] = {}
_META_TTL_S = 3600.0


def _meta_put(mid: str, song_type: Any = None, media_mid: str = "") -> None:
    entry = _META_CACHE.get(mid) or {}
    if song_type is not None:
        entry["song_type"] = song_type
    if media_mid:
        entry["media_mid"] = media_mid
    entry["ts"] = time.monotonic()
    _META_CACHE[mid] = entry
    if len(_META_CACHE) > 4096:
        now = time.monotonic()
        for key in [k for k, v in _META_CACHE.items() if now - float(v.get("ts") or 0) > _META_TTL_S]:
            _META_CACHE.pop(key, None)
        if len(_META_CACHE) > 4096:
            for key in list(_META_CACHE)[: len(_META_CACHE) - 4096]:
                _META_CACHE.pop(key, None)


def _meta_get(mid: str) -> dict[str, Any]:
    entry = _META_CACHE.get(mid)
    if entry and time.monotonic() - float(entry.get("ts") or 0) <= _META_TTL_S:
        return entry
    return {}


# --------------------------------------------------------------------------- 归一化

def album_pic_url(album_mid: str) -> str:
    """专辑封面（500x500）；无 mid 返回空串由调用方回落占位图。"""
    mid = str(album_mid or "").strip()
    if not mid:
        return ""
    return f"https://y.qq.com/music/photo_new/T002R500x500M000{mid}.jpg"


def singer_pic_url(singer_mid: str) -> str:
    mid = str(singer_mid or "").strip()
    if not mid:
        return ""
    return f"https://y.qq.com/music/photo_new/T001R500x500M000{mid}.jpg"


def _pay_required(song: Any) -> bool:
    """是否需付费/会员权益才可播放。"""
    pay = getattr(song, "pay", None)
    if pay is None:
        return False
    return bool(getattr(pay, "pay_play", 0))


def song_to_item(song: Any) -> dict[str, Any]:
    """搜索/榜单条目 → 与 musicbox 对齐的行格式。

    song_id 用 mid（取链必需）；付费曲目不打 fee=1——proxy 的可播过滤会把
    fee 非 0/8 直接剔除，这里改用 needs_vip 标记，效果与网易 [VIP] 一致。
    """
    mid = str(getattr(song, "mid", "") or "")
    singers = list(getattr(song, "singer", None) or [])
    album = getattr(song, "album", None)
    file_info = getattr(song, "file", None)

    artist = "/".join(str(s.name or "") for s in singers if getattr(s, "name", ""))
    album_mid = str(getattr(album, "mid", "") or "")
    media_mid = str(getattr(file_info, "media_mid", "") or "")
    if mid and media_mid:
        _meta_put(mid, getattr(song, "type", None), media_mid)

    # 可用无损：file.size_flac > 0 视为具备 SQ
    size_flac = int(getattr(file_info, "size_flac", 0) or 0) if file_info else 0
    size_320 = int(getattr(file_info, "size_320mp3", 0) or 0) if file_info else 0

    return {
        "song_id": mid,
        "id": getattr(song, "id", 0),
        "song_mid": mid,
        "song_name": str(getattr(song, "name", "") or getattr(song, "title", "") or ""),
        "title": str(getattr(song, "name", "") or ""),
        "artist": artist,
        "album_name": str(getattr(album, "name", "") or ""),
        "album": str(getattr(album, "name", "") or ""),
        "album_pic_url": album_pic_url(album_mid),
        "duration": int(getattr(song, "interval", 0) or 0),
        "quality": "SQ" if size_flac else ("HQ" if size_320 else ""),
        "has_sq": bool(size_flac),
        "has_hr": False,
        "fee": 0,
        "needs_vip": _pay_required(song) and not is_logged_in(),
        "version": str(getattr(song, "subtitle", "") or ""),
        "mv_vid": str(getattr(getattr(song, "mv", None), "vid", "") or ""),
    }


# --------------------------------------------------------------------------- 搜索

async def search(keyword: str, limit: int = 20, page: int = 1) -> list[dict[str, Any]]:
    """歌曲搜索（分页由上游 page 参数原生支持）。"""
    if not keyword or not keyword.strip():
        return []
    client = await get_client()
    _, _, _, SearchType, _, _ = _import_api()
    try:
        resp = await client.search.search_by_type(
            keyword.strip(), SearchType.SONG, num=min(max(limit, 1), 100), page=max(page, 1)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("qqmusic search failed: %s", exc)
        raise
    songs = list(getattr(resp, "song", None) or [])
    return [song_to_item(s) for s in songs if str(getattr(s, "mid", "") or "")]


async def search_album(keyword: str, limit: int = 20, page: int = 1) -> list[dict[str, Any]]:
    client = await get_client()
    _, _, _, SearchType, _, _ = _import_api()
    resp = await client.search.search_by_type(
        keyword.strip(), SearchType.ALBUM, num=min(max(limit, 1), 100), page=max(page, 1)
    )
    out = []
    for a in list(getattr(resp, "album", None) or []):
        out.append({
            "album_id": str(getattr(a, "mid", "") or ""),
            "album_name": str(getattr(a, "name", "") or ""),
            "artist": str(getattr(getattr(a, "singer", None), "name", "") or ""),
            "album_pic_url": album_pic_url(str(getattr(a, "mid", "") or "")),
        })
    return out


async def search_artist(keyword: str, limit: int = 20, page: int = 1) -> list[dict[str, Any]]:
    client = await get_client()
    _, _, _, SearchType, _, _ = _import_api()
    resp = await client.search.search_by_type(
        keyword.strip(), SearchType.SINGER, num=min(max(limit, 1), 100), page=max(page, 1)
    )
    out = []
    for s in list(getattr(resp, "singer", None) or []):
        out.append({
            "artist_id": str(getattr(s, "mid", "") or ""),
            "artist_name": str(getattr(s, "name", "") or ""),
            "artist_pic_url": singer_pic_url(str(getattr(s, "mid", "") or "")),
        })
    return out


# --------------------------------------------------------------------------- 详情

async def song_detail(mid: str) -> dict[str, Any] | None:
    """单曲详情（mid 或数字 id 均可）。"""
    client = await get_client()
    try:
        resp = await client.song.get_detail(mid)
    except Exception as exc:  # noqa: BLE001
        logger.warning("qqmusic song detail failed (%s): %s", mid, exc)
        return None
    track = getattr(resp, "track", None)
    if track is None:
        return None
    item = song_to_item(track)
    # 详情补齐简介/发行时间等展示字段
    extras = getattr(resp, "extras", None) or {}
    if isinstance(extras, dict):
        item["trans_name"] = str(extras.get("transname") or "")
    for group_name, key in (("genre", "genre"), ("lan", "language"), ("pub_time", "pub_time")):
        rows = getattr(resp, group_name, None) or []
        values = [str(getattr(c, "value", "") or "") for c in rows if getattr(c, "value", "")]
        if values:
            item[key] = values[0]
    return item


async def batch_song_details(mids: list[str]) -> list[dict[str, Any]]:
    """批量详情：逐个拉取（上游无批量歌曲详情端点，控制并发避免限流）。"""
    out: list[dict[str, Any]] = []
    sem = asyncio.Semaphore(4)

    async def one(mid: str) -> None:
        async with sem:
            try:
                item = await song_detail(mid)
            except Exception:  # noqa: BLE001
                item = None
            if item:
                out.append(item)

    await asyncio.gather(*(one(m) for m in mids))
    order = {m: i for i, m in enumerate(mids)}
    out.sort(key=lambda it: order.get(str(it.get("song_id") or ""), len(mids)))
    return out


# --------------------------------------------------------------------------- 取链

async def _ensure_media_mid(mid: str) -> tuple[str, int]:
    """取链所需的 media_mid 与 song_type；缓存未命中时查详情。"""
    cached = _meta_get(mid)
    media_mid = str(cached.get("media_mid") or "")
    song_type = int(cached.get("song_type") or 0) if cached.get("song_type") is not None else 0
    if media_mid:
        return media_mid, song_type
    try:
        detail = await song_detail(mid)
    except Exception:  # noqa: BLE001
        detail = None
    if detail:
        # song_to_item 已写入缓存
        cached = _meta_get(mid)
        media_mid = str(cached.get("media_mid") or "")
        song_type = int(cached.get("song_type") or 0) if cached.get("song_type") is not None else song_type
    return media_mid, song_type


async def song_url(mid: str, quality: str = "lossless") -> dict[str, Any]:
    """取播放直链。

    返回 {"code":200,"url":...} 或 {"code":<上游码>,"url":""}；
    code 语义对齐 musicbox：200 成功、404 无资源、其余为上游拒绝。
    """
    client = await get_client()
    _, _, _, _, SongFileInfo, _ = _import_api()
    file_type_name = QUALITY_MAP.get(str(quality or "").strip().lower(), "MP3_128")
    ft = _file_type(file_type_name)

    media_mid, song_type = await _ensure_media_mid(mid)
    if not media_mid:
        return {"code": 404, "url": "", "quality": file_type_name, "reason": "no_media_mid"}

    info = [SongFileInfo(mid=mid, file_type=ft, song_type=song_type, media_mid=media_mid)]
    try:
        resp = await client.song.get_song_urls(info, ft)
    except Exception as exc:  # noqa: BLE001
        logger.warning("qqmusic song url failed (%s/%s): %s", mid, file_type_name, exc)
        return {"code": 502, "url": "", "quality": file_type_name, "reason": str(exc)}

    rows = list(getattr(resp, "data", None) or [])
    if not rows:
        return {"code": 404, "url": "", "quality": file_type_name}
    row = rows[0]
    # 注意：result 为 0 表示成功，不能用 `or` 兜底（0 是 falsy 会被吞成 -1）
    raw_result = getattr(row, "result", None)
    try:
        result = int(raw_result) if raw_result is not None else -1
    except (TypeError, ValueError):
        result = -1
    purl = str(getattr(row, "purl", "") or "")
    if result != 0 or not purl:
        return {
            "code": 404,
            "url": "",
            "quality": file_type_name,
            "upstream_result": result,
            "reason": "no_permission" if result in (104003, 104004) else "unavailable",
        }
    url = purl if purl.startswith(("http://", "https://")) else CDN_BASE + purl
    return {
        "code": 200,
        "url": url,
        "quality": file_type_name,
        "ext": ".flac" if _is_lossless(file_type_name) else ".mp3",
        "expiration": int(getattr(resp, "expiration", 0) or 0),
        "vkey": str(getattr(row, "vkey", "") or ""),
    }


async def resolve_url_with_ladder(mid: str, quality: str, mode: str) -> dict[str, Any]:
    """按音质偏好由高到低回退取链（高音质无权限时降级到可播档）。"""
    ladder = QUALITY_LADDER.get(str(mode or "").strip().lower()) or QUALITY_LADDER["balanced"]
    ordered: list[str] = []
    primary = str(quality or "").strip().lower()
    if primary in QUALITY_MAP:
        ordered.append(primary)
    for q in ladder:
        if q not in ordered:
            ordered.append(q)
    last: dict[str, Any] = {"code": 404, "url": "", "quality": primary or "standard"}
    for q in ordered:
        result = await song_url(mid, q)
        if result.get("code") == 200 and result.get("url"):
            return result
        last = result
    return last


# --------------------------------------------------------------------------- 歌词

async def song_lyric(mid: str, trans: bool = True) -> dict[str, str]:
    """歌词（原文 + 可选译文），字段对齐 musicbox 的 {lyric, tlyric}。"""
    client = await get_client()
    out = {"lyric": "", "tlyric": ""}
    try:
        resp = await client.lyric.get_lyric(mid, song_type=1, trans=bool(trans))
    except Exception as exc:  # noqa: BLE001
        logger.warning("qqmusic lyric failed (%s): %s", mid, exc)
        return out
    out["lyric"] = str(getattr(resp, "lyric", "") or "").strip()
    out["tlyric"] = str(getattr(resp, "trans", "") or "").strip()
    return out


# --------------------------------------------------------------------------- 推荐 / 榜单

def _extract_songs(resp: Any) -> list[Any]:
    """从各端点响应里取歌曲列表。

    上游不同端点用了 song / songs / data 等不同字段名（且部分为分页请求直接
    返回条目列表），统一收敛到这里，避免每处各写一套 getattr 猜测。
    """
    if resp is None:
        return []
    if isinstance(resp, list):
        return [x for x in resp if str(getattr(x, "mid", "") or "")]
    for attr in ("song", "songs", "data", "items", "list"):
        value = getattr(resp, attr, None)
        if value is None and isinstance(resp, dict):
            value = resp.get(attr)
        if isinstance(value, list):
            return [x for x in value if str(getattr(x, "mid", "") or "")]
    return []


async def recommend_songs(limit: int = 30) -> list[dict[str, Any]]:
    """每日推荐：登录用雷达推荐，匿名回退新歌推荐。"""
    client = await get_client()
    songs: list[Any] = []
    try:
        if is_logged_in():
            try:
                resp = await client.recommend.get_radar_recommend(page=1)
                songs = _extract_songs(resp)
            except Exception as exc:  # noqa: BLE001
                logger.warning("qqmusic radar recommend failed: %s", exc)
        if not songs:
            resp = await client.recommend.get_recommend_newsong()
            songs = _extract_songs(resp)
    except Exception as exc:  # noqa: BLE001
        logger.warning("qqmusic recommend failed: %s", exc)
        return []
    items = [song_to_item(s) for s in songs]
    return items[: max(limit, 1)]


async def toplist(top_id: int, limit: int = 60) -> list[dict[str, Any]]:
    """榜单曲目。"""
    client = await get_client()
    try:
        resp = await client.top.get_detail(top_id, num=min(max(limit, 1), 100), page=1)
    except Exception as exc:  # noqa: BLE001
        logger.warning("qqmusic toplist failed (%s): %s", top_id, exc)
        return []
    return [song_to_item(s) for s in _extract_songs(resp)][: max(limit, 1)]


# --------------------------------------------------------------------------- 扫码登录

# 二维码会话：id -> {qr, ts, type}。二维码有效期约 3 分钟，超时自动清理。
_QR_SESSIONS: dict[str, dict[str, Any]] = {}
_QR_TTL_S = 300.0
_QR_LOCK = asyncio.Lock()


def _qr_sweep() -> None:
    now = time.monotonic()
    for key in [k for k, v in _QR_SESSIONS.items() if now - float(v.get("ts") or 0) > _QR_TTL_S]:
        _QR_SESSIONS.pop(key, None)


async def create_qr(login_type: str = "qq") -> dict[str, Any]:
    """生成二维码会话，返回会话 id 与图片 MIME。

    图片二进制经 /api/v1/auth/login/qr.png?id=<id> 输出，前端直接 <img> 展示。
    """
    _, _, QRLoginType, _, _, _ = _import_api()
    client = await get_client()
    ltype = QRLoginType.WX if str(login_type or "").strip().lower() in ("wx", "weixin", "wechat") else QRLoginType.QQ
    async with _QR_LOCK:
        _qr_sweep()
        qr = await client.login.get_qrcode(ltype)
        qr_id = uuid.uuid4().hex
        _QR_SESSIONS[qr_id] = {"qr": qr, "ts": time.monotonic(), "type": ltype.value}
        return {
            "qr_id": qr_id,
            "mimetype": str(getattr(qr, "mimetype", "") or "image/png"),
            "login_type": ltype.value,
        }


def qr_image(qr_id: str) -> tuple[bytes, str] | None:
    """取二维码图片二进制与 MIME。"""
    _qr_sweep()
    entry = _QR_SESSIONS.get(qr_id)
    if not entry:
        return None
    qr = entry["qr"]
    return bytes(getattr(qr, "data", b"") or b""), str(getattr(qr, "mimetype", "") or "image/png")


# 对齐网易轮询语义，前端可直接复用既有状态机
QR_WAITING = 801
QR_SCANNED = 802
QR_DONE = 803
QR_EXPIRED = 800
QR_REFUSED = 804


async def check_qr(qr_id: str) -> dict[str, Any]:
    """轮询二维码状态；成功时落盘凭证并刷新 Client 登录态。"""
    from qqmusic_api.models.login import QRCodeLoginEvents  # noqa: PLC0415

    entry = _QR_SESSIONS.get(qr_id)
    if not entry:
        return {"code": QR_EXPIRED, "message": "二维码不存在或已过期", "logged_in": is_logged_in()}
    client = await get_client()
    try:
        result = await client.login.check_qrcode(entry["qr"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("qqmusic qr check failed: %s", exc)
        return {"code": QR_WAITING, "message": "轮询失败，稍后重试", "logged_in": False}

    event = getattr(result, "event", None)
    credential = getattr(result, "credential", None)
    if event == QRCodeLoginEvents.DONE and credential is not None:
        client.credential = credential
        save_credential(credential)
        _QR_SESSIONS.pop(qr_id, None)
        return {"code": QR_DONE, "message": "登录成功", "logged_in": True}
    # QQ ptqrlogin 状态：66 等待扫码、67 已扫码待确认、65 失效、68 拒绝、0 成功
    if event == QRCodeLoginEvents.SCAN:
        return {"code": QR_WAITING, "message": "等待扫码", "logged_in": False}
    if event == QRCodeLoginEvents.CONF:
        return {"code": QR_SCANNED, "message": "已扫码，请在手机上确认", "logged_in": False}
    if event == QRCodeLoginEvents.REFUSE:
        _QR_SESSIONS.pop(qr_id, None)
        return {"code": QR_REFUSED, "message": "已拒绝授权", "logged_in": False}
    if event == QRCodeLoginEvents.TIMEOUT:
        _QR_SESSIONS.pop(qr_id, None)
        return {"code": QR_EXPIRED, "message": "二维码已过期", "logged_in": False}
    return {"code": QR_WAITING, "message": "等待扫码", "logged_in": False}


async def auth_status() -> dict[str, Any]:
    """登录状态：含昵称等展示信息。"""
    logged = is_logged_in()
    info: dict[str, Any] = {"logged_in": logged}
    if not logged:
        return info
    cred = current_credential()
    info["musicid"] = getattr(cred, "musicid", 0)
    info["nickname"] = ""
    info["svip"] = False
    client = await get_client()
    try:
        resp = await client.user.get_vip_info()
        info["svip"] = bool(getattr(resp, "svip", 0))
        # 昵称在 userinfo 摘要里；不同上游版本字段形态不一，做防御式提取
        for holder in (getattr(resp, "userinfo", None), getattr(resp, "identity", None)):
            if isinstance(holder, dict):
                for key in ("nick", "nickname", "name", "nickName"):
                    value = str(holder.get(key) or "").strip()
                    if value:
                        info["nickname"] = value
                        break
            if info["nickname"]:
                break
    except Exception:  # noqa: BLE001  昵称/会员信息失败不影响登录态判定
        pass
    return info


async def logout() -> None:
    """退出登录：清盘凭证并丢弃 Client。"""
    try:
        client = await get_client()
        await client.login.logout()
    except Exception as exc:  # noqa: BLE001
        logger.warning("qqmusic logout upstream failed: %s", exc)
    clear_credential()
    reset_client("logout")


# ------------------------------------------------------------------ Cookie 登录
# 扫码之外的第二条路：直接粘贴浏览器里的 music.qq.com Cookie。
# 真正必需的只有 uin（QQ 号）与 qm_keyst / qqmusic_key（登录票据）；
# access_token / refresh_token 缺失只会失去自动续期能力，不影响当下取流。

# Cookie 登录没有 refresh_token，票据真实有效期由服务端决定。这里给一个保守的
# keyExpiresIn 只为让 is_expired() 不误判——否则每次请求都会触发一次注定失败的
# 刷新，白白拖慢取链。
_COOKIE_KEY_TTL_S = 30 * 24 * 3600


def parse_cookie(raw: str) -> dict[str, str]:
    """把浏览器里复制出来的 Cookie 串解析成键值表。"""
    jar: dict[str, str] = {}
    text = (raw or "").replace("\r", "").replace("\n", ";")
    if text.lstrip().lower().startswith("cookie:"):
        text = text.lstrip()[7:]  # 兼容整行 "Cookie: xxx" 粘贴
    for part in text.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        if not key:
            continue
        jar[key] = value.strip().strip('"').strip("'")
    return jar


def credential_from_cookie(raw: str) -> Any:
    """Cookie → 库的 Credential；字段不足时抛 ValueError。"""
    _, Credential, _, _, _, _ = _import_api()
    jar = parse_cookie(raw)
    uin = ""
    for key in ("uin", "p_uin", "wxuin", "str_musicid", "musicid"):
        value = jar.get(key, "").strip()
        if value:
            uin = value
            break
    if uin[:1] in ("o", "O") and uin[1:].isdigit():
        uin = uin[1:]  # 微信侧 uin 形如 o0123456789，去掉前缀才是 QQMusicID
    musickey = ""
    for key in ("qm_keyst", "qqmusic_key", "musickey"):
        value = jar.get(key, "").strip()
        if value:
            musickey = value
            break
    if not uin or not musickey:
        raise ValueError(
            "Cookie 里没找到 uin / qm_keyst（或 qqmusic_key），"
            "请在浏览器打开 music.qq.com 后复制完整 Cookie")
    if not (uin.isdigit() and int(uin) > 0):
        raise ValueError(f"Cookie 里的 uin 不是有效 QQ 号: {uin}")
    musicid = int(uin)
    return Credential.model_validate({
        "musicid": musicid,
        # o0123456789 去掉前缀后是 0123456789，前导零是补齐用的，必须归一，
        # 否则请求里带出去的 QQMusicID 与 musicid 不一致
        "str_musicid": str(musicid),
        "musickey": musickey,
        "musickeyCreateTime": int(time.time()),
        "keyExpiresIn": _COOKIE_KEY_TTL_S,
        # 库的 model_validator 也会按 W_X 前缀推断，这里显式给出更稳妥
        "loginType": 1 if musickey.startswith("W_X") else 2,
    })


async def login_with_cookie(raw: str) -> dict[str, Any]:
    """用 Cookie 登录：套到 Client 上、发一次真实请求校验、凭证落盘。"""
    credential = credential_from_cookie(raw)
    client = await get_client()
    client.credential = credential
    # get_vip_info 必须带登录态，用它验证票据是否真能用（顺带拿到 SVIP 状态）
    try:
        resp = await client.user.get_vip_info()
    except Exception as exc:  # noqa: BLE001
        reset_client("cookie login rejected")
        raise ValueError(f"Cookie 校验失败（票据无效或已过期）：{exc}") from exc
    save_credential(credential)
    info = await auth_status()
    info["svip"] = bool(getattr(resp, "svip", 0)) or bool(info.get("svip"))
    return info


async def refresh_if_needed() -> bool:
    """凭证过期时尝试刷新；返回刷新后是否仍可用。"""
    if not is_logged_in():
        return False
    cred = current_credential()
    try:
        if not cred.is_expired():
            return True
    except Exception:  # noqa: BLE001
        return True
    client = await get_client()
    try:
        new_cred = await client.login.refresh_credential()
    except Exception as exc:  # noqa: BLE001
        logger.warning("qqmusic credential refresh failed: %s", exc)
        return False
    client.credential = new_cred
    save_credential(new_cred)
    return True


async def close() -> None:
    """关闭底层连接池。"""
    global _client
    if _client is None:
        return
    try:
        close_fn = getattr(_client, "close", None)
        if close_fn is not None:
            result = close_fn()
            if asyncio.iscoroutine(result):
                await result
    except Exception as exc:  # noqa: BLE001
        logger.warning("qqmusic client close failed: %s", exc)
    _client = None


def credential_public() -> dict[str, Any]:
    """脱敏的凭证摘要（日志/排障用，不含密钥）。"""
    cred = current_credential()
    return {
        "musicid": getattr(cred, "musicid", 0),
        "login_type": getattr(cred, "login_type", 0),
        "expired_at": getattr(cred, "expired_at", 0),
    }
