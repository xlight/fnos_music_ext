"""补全记录账本 proxy/completion_log.py 的用例（change add-full-track-completion · 任务 1.6）。

覆盖：字段与 event/outcome 映射、有界覆盖写、原子写、损坏隔离（rename .corrupt-* 且不覆盖原内容）、
读盘失败/无权限的显式状态、写盘失败绝不抛出、字符串字段 redact 脱敏、并发 record 不丢记录。
"""
import json
import os
import threading
from pathlib import Path

import pytest

from proxy import completion_log as cl


@pytest.fixture()
def ledger_path(tmp_path, monkeypatch):
    path = tmp_path / "completion_log.json"
    monkeypatch.setenv("FNMUSIC_COMPLETION_LOG_FILE", str(path))
    cl.reset_for_tests()
    yield path
    cl.reset_for_tests()


def _rec(**over):
    rec = {
        "guid": "online:netease:167691",
        "provider": "netease",
        "song_id": "167691",
        "title": "天龙八部之宿敌",
        "artist": "许嵩",
        "reason": "preview_rejected",
        "elapsed_ms": 4321,
        "fallback_source": "kuwo",
        "fallback_guid": "online:kuwo:1090336",
        "scope": "scopehash",
    }
    rec.update(over)
    return rec


def test_record_writes_fields_and_mapping(ledger_path):
    assert cl.record("completion_hit", **_rec()) == "ok"
    data = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    rec = data["records"][0]
    assert rec["event"] == "completion_hit"
    assert rec["outcome"] == "hit"                      # 固定映射
    assert rec["provider"] == "netease"
    assert rec["title"] == "天龙八部之宿敌"
    assert rec["time"].endswith("+08:00")                # ISO8601 带偏移
    assert cl.record("completion_failed", **_rec(reason="no_items")) == "ok"
    assert cl.record("completion_skip", **_rec(reason="switch_off")) == "ok"
    s = cl.summary()
    assert s["counts"] == {"hit": 1, "miss": 1, "skip": 1}
    assert s["total"] == 3
    assert s["latest"]["outcome"] == "skip"              # 头插：最新在前


def test_records_are_bounded(ledger_path):
    for i in range(cl.MAX_RECORDS + 50):
        cl.record("completion_hit", **_rec(title=f"歌{i}"))
    data = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert len(data["records"]) == cl.MAX_RECORDS
    assert data["records"][0]["title"] == f"歌{cl.MAX_RECORDS + 49}"


def test_unknown_event_is_ignored(ledger_path):
    assert cl.record("something_else", **_rec()) == "ignored"
    assert not ledger_path.exists()


def test_string_fields_are_redacted(ledger_path):
    cl.record("completion_hit", **_rec(title="某歌 token=abcdef123456"))
    data = json.loads(ledger_path.read_text(encoding="utf-8"))
    title = data["records"][0]["title"]
    assert "abcdef123456" not in title
    assert "***" in title


def test_corrupt_file_is_quarantined_not_overwritten(ledger_path):
    ledger_path.write_bytes(b"{not json")
    status = cl.record("completion_hit", **_rec())
    assert status in ("corrupt", "ok")
    # 写者触发隔离：原文件被 rename 成 .corrupt-*，内容未被覆盖
    quarantined = list(ledger_path.parent.glob("completion_log.json.corrupt-*"))
    assert quarantined, "损坏文件应被隔离"
    assert quarantined[0].read_bytes() == b"{not json"
    # 新账本从这里继续写（第一条记录已落盘）
    data = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert len(data["records"]) == 1
    assert cl.record("completion_hit", **_rec()) == "ok"
    assert len(json.loads(ledger_path.read_text(encoding="utf-8"))["records"]) == 2
    # 读者报告 corrupt 而不是假 0
    ledger_path.write_bytes(b"###broken")
    s = cl.summary(read_only=True)
    assert s["ok"] is False and s["status"] == "corrupt"


def test_missing_file_reports_missing_status(ledger_path):
    s = cl.summary(read_only=True)
    assert s["ok"] is True and s["status"] == "missing" and s["records"] == [] and s["latest"] is None


def test_record_never_raises_on_unwritable_path(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_COMPLETION_LOG_FILE", str(tmp_path / "no-such-dir" / "x" / "c.json"))
    cl.reset_for_tests()
    # 父目录不存在且无法创建（把父目录做成文件）→ 必须吞掉异常
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setenv("FNMUSIC_COMPLETION_LOG_FILE", str(blocker / "c.json"))
    assert cl.record("completion_hit", **_rec()) in ("save_failed", "permission_denied", "ok")


def test_concurrent_records_do_not_lose_entries(ledger_path):
    def worker(n):
        for i in range(20):
            cl.record("completion_hit", **_rec(title=f"t{n}-{i}"))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    s = cl.summary()
    assert s["total"] == 80            # 4 线程 × 20 条，一条不丢


def test_summary_pagination(ledger_path):
    for i in range(10):
        cl.record("completion_skip", **_rec(title=f"s{i}", reason="cooldown"))
    page = cl.summary(limit=3, offset=2)
    assert page["total"] == 10
    assert [r["title"] for r in page["records"]] == ["s7", "s6", "s5"]
    assert page["counts"]["skip"] == 10


def test_summary_clamps_limit(ledger_path):
    for i in range(5):
        cl.record("completion_hit", **_rec())
    assert len(cl.summary(limit=99999)["records"]) == 5
