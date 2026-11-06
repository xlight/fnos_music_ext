"""官方库只读访问：worker 以曲库属主身份运行，直接 mode=ro 读取。

3.0.0 之前 proxy 以 root 常驻、读取时临时提权；现在 worker 由 takeover 以
曲库属主身份启动，不再有任何 setuid/租约逻辑。这里锁定两条行为：

- 读取不触碰运行身份（没有 seteuid 调用）；
- 缺库与权限拒绝分别给出可操作的错误。
"""
import os
import sqlite3

import pytest

from proxy import app as music
from proxy import recommend as rec


def _make_db(path, rows=(("u", "甲", "admin", "active"),)):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE user(id INTEGER, guid TEXT, name TEXT, role TEXT, status TEXT)")
        con.executemany("INSERT INTO user VALUES(?,?,?,?,?)",
                        [(i, *row) for i, row in enumerate(rows, 1)])


def test_roster_reads_directly_without_identity_switch(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(os, "seteuid", lambda uid: calls.append(uid), raising=False)
    monkeypatch.setattr(os, "setegid", lambda gid: calls.append(gid), raising=False)
    db = tmp_path / "music.db"
    _make_db(db)
    assert music.list_music_users(str(db)) == [{"guid": "u", "name": "甲", "role": "admin"}]
    assert calls == []


def test_missing_db_reports_unavailable(tmp_path):
    with pytest.raises(ValueError, match="不可用"):
        music.list_music_users(str(tmp_path / "missing.db"))


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_unreadable_db_reports_permission(tmp_path):
    db = tmp_path / "music.db"
    _make_db(db)
    db.chmod(0o000)
    try:
        with pytest.raises(ValueError, match="不可读"):
            music.list_music_users(str(db))
    finally:
        db.chmod(0o600)


def test_music_db_helper_opens_read_only(tmp_path):
    db = tmp_path / "music.db"
    _make_db(db)
    with rec._music_db(str(db)) as con:
        assert con.execute("SELECT guid FROM user").fetchone() == ("u",)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        with rec._music_db(str(db)) as con:
            con.execute("CREATE TABLE should_fail(x)")


def test_library_dir_detection_reads_shared_library_as_runtime_user(tmp_path, monkeypatch):
    library = tmp_path / "music"
    library.mkdir()
    db = tmp_path / "music.db"
    _make_db(db)
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE shared_library(id INTEGER, path TEXT)")
        con.execute("INSERT INTO shared_library VALUES(1, ?)", (str(library),))
    calls = []
    monkeypatch.setattr(os, "seteuid", lambda uid: calls.append(uid), raising=False)
    monkeypatch.setattr(music, "CONF", dict(music.CONF, music_db=str(db), library_dir=""))
    monkeypatch.setattr(music, "_DETECTED_LIBRARY_DIR", "")
    assert music.detect_library_dir() == str(library)
    assert calls == []
