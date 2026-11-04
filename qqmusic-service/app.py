"""HTTP wrapper for QQ 音乐音源（qqmusic-api-python），供 fnmusic-ext 调用。

默认监听 127.0.0.1:8775；接口形状与 musicbox-service 对齐，proxy 可复用
同一套搜索/取链/歌词适配逻辑。扫码登录后才能取到 320k/无损等需要权益的音质。
"""
from __future__ import annotations

import io
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Path, Query, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

import qqmusic_ext

# 与其他服务对齐：INFO 需显式 handler，否则只剩 lastResort 的 WARNING+
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("qqmusic_service.app")
try:  # 本地文件日志（logs/qqmusic.log，按天滚动保留 3 天）；失败静默降级
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
    from proxy.fnlog import setup_logging as _setup_logging

    _setup_logging("qqmusic_service.app", "qqmusic")
except Exception:  # noqa: BLE001  无 proxy/ 上下文（如独立调试）时仅 stdout
    pass

qqmusic_ext.ensure_dirs()

SEARCH_TYPES = {"song", "album", "artist"}
QUALITY_WHITELIST = {"exhigh", "higher", "standard", "lossless", "hires"}


def _lib_missing(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail=f"QQ 音乐依赖不可用（qqmusic-api-python 未安装或版本过低）: {exc}",
    )


@asynccontextmanager
async def lifespan(application):
    try:
        yield
    finally:
        await qqmusic_ext.close()


app = FastAPI(title="fnmusic-qqmusic", version="1.0.0", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError):
    return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"detail": exc.errors()})


@app.get("/healthz")
def healthz():
    return {
        "status": "ok",
        "source": "https://github.com/L-1124/QQMusicApi",
        "logged_in": qqmusic_ext.is_logged_in(),
    }


@app.get("/api/v1/search")
async def search(
    keyword: str = Query(...),
    type: str = Query("song"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    if not keyword.strip():
        raise HTTPException(status_code=400, detail="keyword cannot be empty")
    if type not in SEARCH_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid type {type!r}")
    # 上游按页分页（每页 limit 条），offset 换算为页码
    page = (offset // limit) + 1 if limit else 1
    try:
        if type == "album":
            items = await qqmusic_ext.search_album(keyword, limit=limit, page=page)
        elif type == "artist":
            items = await qqmusic_ext.search_artist(keyword, limit=limit, page=page)
        else:
            items = await qqmusic_ext.search(keyword, limit=limit, page=page)
    except ImportError as exc:
        raise _lib_missing(exc) from exc
    except Exception as exc:  # noqa: BLE001  上游异常降级为空结果，不阻塞整体搜索
        logger.warning("qqmusic search failed: %s", exc)
        return {"ok": False, "code": 502, "error": str(exc), "data": []}
    return {"ok": True, "code": 200, "data": items}


@app.get("/api/v1/song/{song_id}/url")
async def song_url(
    song_id: str = Path(..., min_length=1),
    quality: str = Query("lossless"),
    mode: str = Query("high"),
    fresh: int = Query(0),
):
    """取播放直链；quality 指定首选档，mode 决定回退阶梯。

    fresh=1 仅用于旁路缓存（此处每次都向上游要新链，保留参数以对齐契约）。
    """
    q = str(quality or "").strip().lower()
    if q not in QUALITY_WHITELIST:
        raise HTTPException(status_code=400, detail=f"Invalid quality {quality!r}")
    try:
        item = await qqmusic_ext.resolve_url_with_ladder(song_id, q, mode)
    except ImportError as exc:
        raise _lib_missing(exc) from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning("qqmusic url failed (%s): %s", song_id, exc)
        return {"ok": False, "code": 502, "error": str(exc)}
    if item.get("code") != 200 or not item.get("url"):
        return {"ok": False, "code": 404, "data": item}
    return {"ok": True, "data": item}


@app.get("/api/v1/song/{song_id}/info")
async def song_info(song_id: str = Path(..., min_length=1)):
    try:
        item = await qqmusic_ext.song_detail(song_id)
    except ImportError as exc:
        raise _lib_missing(exc) from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning("qqmusic info failed (%s): %s", song_id, exc)
        return {"ok": False, "code": 502, "error": str(exc)}
    if item is None:
        return {"ok": False, "code": 404, "error": "song not found"}
    return {"ok": True, "data": item}


@app.get("/api/v1/songs/detail")
async def songs_detail(ids: str = Query(None)):
    if not ids or not ids.strip():
        raise HTTPException(status_code=422, detail="ids parameter is required")
    mids = [t.strip() for t in ids.split(",") if t.strip()]
    if not 1 <= len(mids) <= 100:
        raise HTTPException(status_code=422, detail="ids count must be 1..100")
    try:
        rows = await qqmusic_ext.batch_song_details(mids)
    except ImportError as exc:
        raise _lib_missing(exc) from exc
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "data": rows}


@app.get("/api/v1/song/{song_id}/lyric")
async def song_lyric(song_id: str = Path(..., min_length=1)):
    try:
        pair = await qqmusic_ext.song_lyric(song_id)
    except ImportError as exc:
        raise _lib_missing(exc) from exc
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "data": pair}


@app.get("/api/v1/recommend/songs")
async def recommend_songs(limit: int = Query(30, ge=10, le=60)):
    try:
        rows = await qqmusic_ext.recommend_songs(limit=limit)
    except ImportError as exc:
        raise _lib_missing(exc) from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning("qqmusic recommend failed: %s", exc)
        return {"ok": False, "code": 502, "error": str(exc), "data": []}
    return {"ok": True, "data": rows, "logged_in": qqmusic_ext.is_logged_in()}


@app.get("/api/v1/toplist")
async def toplist(index: int = Query(4), limit: int = Query(60, ge=1, le=100)):
    """榜单曲目；index 为 QQ 音乐榜单 ID（默认 4=流行指数榜）。"""
    try:
        rows = await qqmusic_ext.toplist(index, limit=limit)
    except ImportError as exc:
        raise _lib_missing(exc) from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning("qqmusic toplist failed: %s", exc)
        return {"ok": False, "code": 502, "error": str(exc), "data": []}
    return {"ok": True, "data": rows, "index": index}


# --------------------------------------------------------------- 扫码登录 --

@app.get("/api/v1/auth/status")
async def auth_status():
    try:
        info = await qqmusic_ext.auth_status()
    except ImportError as exc:
        raise _lib_missing(exc) from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning("qqmusic auth status failed: %s", exc)
        return {"ok": True, "logged_in": False, "error": str(exc)}
    return {"ok": True, **info}


@app.post("/api/v1/auth/login")
async def auth_login(login_type: str = Query("qq")):
    """生成二维码会话；返回 qr_id，图片经 /api/v1/auth/login/qr.png?id= 获取。"""
    try:
        await qqmusic_ext.refresh_if_needed()
        session = await qqmusic_ext.create_qr(login_type)
    except ImportError as exc:
        raise _lib_missing(exc) from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning("qqmusic qr create failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"二维码生成失败: {exc}") from exc
    return {"ok": True, "data": {**session, "code": qqmusic_ext.QR_WAITING}}


@app.get("/api/v1/auth/login/qr.png")
@app.get("/api/v1/auth/qr.png")
async def auth_login_qr(id: str = Query(...)):
    if not id.strip():
        raise HTTPException(status_code=400, detail="id cannot be empty")
    blob = qqmusic_ext.qr_image(id.strip())
    if not blob or not blob[0]:
        raise HTTPException(status_code=404, detail="二维码不存在或已过期")
    data, mimetype = blob
    return Response(content=data, media_type=mimetype or "image/png")


@app.get("/api/v1/auth/login/check")
async def auth_login_check(id: str = Query(...)):
    if not id.strip():
        raise HTTPException(status_code=400, detail="id cannot be empty")
    try:
        result = await qqmusic_ext.check_qr(id.strip())
    except ImportError as exc:
        raise _lib_missing(exc) from exc
    return {"ok": True, "data": result}


@app.post("/api/v1/auth/cookie")
async def auth_cookie(payload: dict = Body(...)):
    """用浏览器 Cookie 登录（扫码不可用时的兜底）。"""
    raw = str((payload or {}).get("cookie") or "")
    if not raw.strip():
        raise HTTPException(status_code=400, detail="cookie 不能为空")
    try:
        info = await qqmusic_ext.login_with_cookie(raw)
    except ImportError as exc:
        raise _lib_missing(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning("qqmusic cookie login failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"Cookie 登录失败: {exc}") from exc
    return {"ok": True, "data": info}


@app.post("/api/v1/auth/logout")
async def auth_logout():
    try:
        await qqmusic_ext.logout()
    except ImportError as exc:
        raise _lib_missing(exc) from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning("qqmusic logout failed: %s", exc)
    return {"ok": True, "logged_in": False}


@app.get("/api/v1/public-playlists/capabilities")
def public_playlist_capabilities():
    """公共歌单能力声明：QQ 音乐暂不接入公共歌单，显式声明避免前端误探测。"""
    return {
        "ok": True,
        "provider": "qqmusic",
        "sources": [{
            "id": "qq",
            "name": "QQ音乐",
            "playback_available": True,
            "channels": [],
            "logged_in": qqmusic_ext.is_logged_in(),
        }],
    }


@app.get("/api/v1/debug/credential")
def debug_credential():
    return {"ok": True, "data": qqmusic_ext.credential_public(), "data_dir": str(qqmusic_ext.data_dir())}
