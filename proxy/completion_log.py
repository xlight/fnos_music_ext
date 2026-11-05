"""整曲补全记录账本（change add-full-track-completion · 任务 1.6）。

照抄 proxy/llm_usage.py 的范式与不变量：
- 状态文件 completion_log.json（0644 原子写）落在 <FNMUSIC_HOME>（路径解析同 llm_usage）；
  容器里的 WebUI 以 appuser(uid=1000) 只读同一份文件（宿主仓库目录 0770 root:Administrators
  已挡住其他用户，0644 不扩大暴露面）；
- **有界列表**：最近 MAX_RECORDS 条、头插、覆盖写 → 天然不增长，不需要轮转；
- **原子写**：临时文件 → fsync → chmod 0644 → os.replace（不出现半截文件）；
- **损坏隔离**：解析失败绝不覆盖原文件——写者把它 rename 成
  completion_log.json.corrupt-<ts> 后从新账本继续，status 照报（记录不中断）；
  读者 read_only=True 只报告 status="corrupt"，绝不写盘；
- **状态显式**：ok / missing / corrupt / permission_denied / read_failed / save_failed
  都有明确返回，绝不返回假 0；
- **写盘失败绝不抛出**：补全记录是旁路观测，不能让首播路径抛错；
- **脱敏**：字符串字段（曲名/歌手等外部数据）写账本前过 fnlog.redact()。

字段：time(ISO8601+08:00) / event / provider / source_guid / title / artist /
outcome(hit|miss|skip) / reason / elapsed_ms / fallback_source / fallback_guid / scope。
event 与 outcome 的映射固定：completion_hit⟺hit、completion_failed⟺miss、
completion_skip⟺skip。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import re
import tempfile
import threading
import time
from typing import Any

try:  # 与 proxy/app.py 同款的双入口兼容
    from .fnlog import redact
except ImportError:  # pragma: no cover
    from fnlog import redact  # type: ignore

MAX_RECORDS = 200
_TZ_SHANGHAI = timezone(timedelta(hours=8))
_LOCK = threading.Lock()

# 内存账本缓存（单写者进程内有效；跨进程以磁盘为准，读者一律 fresh 读盘）
_MEM_LEDGER: dict | None = None
_LAST_PATH: str | None = None
_STATUS = "ok"
_STATUS_MSG = "正常"

_EVENT_OUTCOME = {
    "completion_hit": "hit",
    "completion_failed": "miss",
    "completion_skip": "skip",
}


def _state_path() -> str:
    env = (os.environ.get("FNMUSIC_COMPLETION_LOG_FILE") or "").strip()
    if env:
        return os.path.normpath(env)
    home = (os.environ.get("FNMUSIC_HOME") or "").strip()
    if not home:
        home = (os.environ.get("WEBUI_REPO_DIR") or "").strip()
    if not home:
        home = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    return os.path.normpath(os.path.join(home, "completion_log.json"))


def _new_ledger() -> dict:
    return {"version": 1, "records": []}


def _now_iso() -> str:
    return datetime.now(_TZ_SHANGHAI).isoformat(timespec="seconds")


def _sanitize(value: Any) -> Any:
    """字符串字段过 fnlog.redact() 脱敏；其余原样。"""
    if isinstance(value, str):
        return redact(value)
    return value


def _parse(content: bytes) -> dict | None:
    try:
        data = json.loads(content.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        return None
    return data


def _atomic_save(path: str, ledger: dict) -> str:
    """原子保存：临时文件 → fsync → chmod 0644 → replace。返回状态名。绝不抛出。"""
    global _STATUS, _STATUS_MSG
    parent = os.path.dirname(path) or "."
    tmp_path: str | None = None
    try:
        os.makedirs(parent, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=parent, delete=False,
                                          encoding="utf-8") as tmp:
            tmp_path = tmp.name
            json.dump(ledger, tmp, ensure_ascii=False, indent=2)
            tmp.flush()
            try:
                os.fsync(tmp.fileno())
            except OSError:
                pass
        try:
            # 显式 0644：容器内 WebUI（appuser, uid=1000）只读同一份文件
            os.chmod(tmp_path, 0o644)
        except OSError:
            pass
        os.replace(tmp_path, path)
        tmp_path = None
        _STATUS, _STATUS_MSG = "ok", "正常"
        return "ok"
    except PermissionError:
        _STATUS, _STATUS_MSG = "permission_denied", "账本文件无写入权限"
        return "permission_denied"
    except OSError as err:
        _STATUS, _STATUS_MSG = "save_failed", f"账本保存失败: {err}"
        return "save_failed"
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _load_for_write(path: str) -> tuple[dict, str]:
    """写者读盘：损坏 → 隔离原文件（rename .corrupt-<ts>）后从新账本继续。"""
    global _MEM_LEDGER, _LAST_PATH, _STATUS, _STATUS_MSG
    if _MEM_LEDGER is not None and path == _LAST_PATH and _STATUS == "ok":
        return _MEM_LEDGER, "ok"
    try:
        with open(path, "rb") as f:
            content = f.read()
    except FileNotFoundError:
        _MEM_LEDGER, _LAST_PATH, _STATUS, _STATUS_MSG = _new_ledger(), path, "missing", "账本文件尚不存在"
        return _MEM_LEDGER, "missing"
    except PermissionError:
        _STATUS, _STATUS_MSG = "permission_denied", "无权限读取账本文件"
        return (_MEM_LEDGER or _new_ledger()), "permission_denied"
    except OSError as err:
        _STATUS, _STATUS_MSG = "read_failed", f"读取账本失败: {err}"
        return (_MEM_LEDGER or _new_ledger()), "read_failed"
    ledger = _parse(content)
    if ledger is None:
        # 损坏隔离：绝不覆盖原内容，rename 成 .corrupt-<ts> 后从新账本继续
        quarantined = False
        try:
            os.replace(path, f"{path}.corrupt-{int(time.time())}")
            quarantined = True
        except OSError:
            pass
        _MEM_LEDGER, _LAST_PATH = _new_ledger(), path
        _STATUS = "corrupt"
        _STATUS_MSG = "账本文件损坏，已隔离未覆盖" + ("（已 rename .corrupt-*）" if quarantined else "")
        return _MEM_LEDGER, "corrupt"
    _MEM_LEDGER, _LAST_PATH, _STATUS, _STATUS_MSG = ledger, path, "ok", "正常"
    return ledger, "ok"


def record(event: str, **fields: Any) -> str:
    """追加一条补全记录（头插、有界）。绝不抛出；返回落盘状态名。"""
    global _MEM_LEDGER
    outcome = _EVENT_OUTCOME.get(event)
    if outcome is None:
        return "ignored"
    with _LOCK:
        path = _state_path()
        ledger, _ = _load_for_write(path)
        rec = {
            "time": _now_iso(),
            "event": event,
            "outcome": outcome,
        }
        for key, value in fields.items():
            if value is None:
                continue
            rec[key] = _sanitize(value)
        ledger.setdefault("records", []).insert(0, rec)
        del ledger["records"][MAX_RECORDS:]
        _MEM_LEDGER = ledger
        return _atomic_save(path, ledger)


def summary(limit: int = 30, offset: int = 0, *, read_only: bool = True) -> dict:
    """读账本摘要：records（分页）/ total / counts / latest / status。

    read_only=True 时每次 fresh 读盘（跨进程实时可见），绝不写盘、绝不缓存。
    """
    limit = max(1, min(100, int(limit or 30)))
    offset = max(0, int(offset or 0))
    path = _state_path()
    if read_only:
        try:
            with open(path, "rb") as f:
                content = f.read()
        except FileNotFoundError:
            return {"ok": True, "status": "missing", "records": [], "total": 0,
                    "counts": {"hit": 0, "miss": 0, "skip": 0}, "latest": None}
        except PermissionError:
            return {"ok": False, "status": "permission_denied", "records": [], "total": 0,
                    "counts": {"hit": 0, "miss": 0, "skip": 0}, "latest": None}
        except OSError as err:
            return {"ok": False, "status": "read_failed", "error": str(err), "records": [],
                    "total": 0, "counts": {"hit": 0, "miss": 0, "skip": 0}, "latest": None}
        ledger = _parse(content)
        if ledger is None:
            return {"ok": False, "status": "corrupt", "error": "账本文件损坏",
                    "records": [], "total": 0,
                    "counts": {"hit": 0, "miss": 0, "skip": 0}, "latest": None}
        records = ledger.get("records") or []
    else:
        with _LOCK:
            ledger, status = _load_for_write(path)
            records = list(ledger.get("records") or [])
            if status != "ok":
                return {"ok": status == "missing", "status": status, "records": [], "total": 0,
                        "counts": {"hit": 0, "miss": 0, "skip": 0}, "latest": None}
    counts = {"hit": 0, "miss": 0, "skip": 0}
    for rec in records:
        outcome = rec.get("outcome")
        if outcome in counts:
            counts[outcome] += 1
    page = records[offset:offset + limit]
    return {
        "ok": True,
        "status": "ok",
        "records": page,
        "total": len(records),
        "counts": counts,
        "latest": records[0] if records else None,
    }


def reset_for_tests() -> None:
    """测试钩子：清空内存缓存（与 llm_usage 的 reset 语义一致）。"""
    global _MEM_LEDGER, _LAST_PATH, _STATUS, _STATUS_MSG
    _MEM_LEDGER = None
    _LAST_PATH = None
    _STATUS, _STATUS_MSG = "ok", "正常"
