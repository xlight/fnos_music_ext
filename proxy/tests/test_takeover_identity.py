"""takeover 运行身份解析与运行目录布局的回归测试。

代理不再以 root 常驻：worker 以曲库属主身份启动（Popen user=/group=/extra_groups=）。
这里用假的属主快照验证候选顺序与 `user=` 参数拼装，不真正切换身份。
"""
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from proxy import takeover as t  # noqa: E402


@pytest.fixture()
def as_root(monkeypatch):
    monkeypatch.setattr(t.os, "name", "posix")
    monkeypatch.setattr(t.os, "geteuid", lambda: 0)
    return monkeypatch


def test_non_root_keeps_current_identity(monkeypatch):
    monkeypatch.setattr(t.os, "geteuid", lambda: 1000)
    assert t.resolve_runtime_identity(Path("/repo"), {}) is None
    assert t.run_as_identity(None) == {}


def test_save_dir_owner_wins_and_groups_are_deduped(as_root, tmp_path):
    owners = {
        "/vol1/1000/music": (1000, 1001),
        str(tmp_path / ".env"): (1000, 1000),
        str(tmp_path / "sources-data"): (0, 0),
    }
    as_root.setattr(t, "_owner_of", lambda path: owners.get(str(path)))
    identity = t.resolve_runtime_identity(tmp_path, {"FNMUSIC_TEE_SAVE_DIR": "/vol1/1000/music"})
    uid, gid, extra = identity
    assert (uid, gid) == (1000, 1000)
    assert 1001 in extra            # 曲库组：写库需要
    assert 1000 not in extra        # 主组不重复加入
    assert t.run_as_identity(identity) == {"user": 1000, "group": 1000, "extra_groups": extra}


def test_library_dir_used_when_save_dir_empty(as_root, tmp_path):
    owners = {
        "/vol1/2000/music": (2000, 2000),
        str(tmp_path / ".env"): (1000, 1000),
    }
    as_root.setattr(t, "_owner_of", lambda path: owners.get(str(path)))
    identity = t.resolve_runtime_identity(
        tmp_path, {"FNMUSIC_TEE_SAVE_DIR": "", "FNMUSIC_LIBRARY_DIR": "/vol1/2000/music"})
    assert identity[0] == 2000 and identity[1] == 1000


def test_root_owned_library_falls_back_to_container_appuser(as_root, tmp_path):
    owners = {
        "/vol1/0/music": (0, 0),
        str(tmp_path / ".env"): (0, 1000),
        str(tmp_path / "sources-data"): (1000, 1000),
    }
    as_root.setattr(t, "_owner_of", lambda path: owners.get(str(path)))
    identity = t.resolve_runtime_identity(tmp_path, {"FNMUSIC_TEE_SAVE_DIR": "/vol1/0/music"})
    assert identity[0] == 1000
    assert identity[1] == 1000


def test_describe_identity_is_readable():
    assert "uid=1000" in t.describe_identity((1000, 1000, []))
    assert "current user" in t.describe_identity(None)


@pytest.mark.skipif(os.geteuid() != 0, reason="requires root to hand the ipc dir to another uid")
def test_runtime_layout_is_traversable_and_hands_ipc_to_worker(tmp_path):
    state = t.State("/tmp/unused-target.sock", "/tmp/unused-upstream.sock", tmp_path / "run")
    t.ensure_runtime_layout(state, (1000, 1000, []))
    directory = state.directory
    ipc = directory / "ipc"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o711
    assert stat.S_IMODE(ipc.stat().st_mode) == 0o711
    assert ipc.stat().st_uid == 1000 and ipc.stat().st_gid == 1000


def test_restore_removes_runtime_status(tmp_path):
    state = t.State("/tmp/unused-target.sock", "/tmp/unused-upstream.sock", tmp_path / "run")
    ipc = state.directory / "ipc"
    ipc.mkdir(parents=True)
    status = ipc / "runtime.json"
    status.write_text("{}")
    state.remove_runtime_status()
    assert not status.exists()
    # 幂等：文件不存在时不得抛错
    state.remove_runtime_status()


@pytest.mark.skipif(os.geteuid() != 0, reason="requires root to spawn as another uid")
def test_preflight_fails_closed_when_venv_not_traversable(tmp_path):
    """venv 对 worker 不可执行：给出含用户与路径的错误，绝不以 root 继续。"""
    base = tmp_path / "repo"
    proxy = base / "proxy"
    proxy.mkdir(parents=True)
    venv = base / ".venv-proxy"
    (venv / "bin").mkdir(parents=True)
    python = venv / "bin" / "python"
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o755)
    venv.chmod(0o700)          # root only: worker (uid 1000) cannot traverse
    with pytest.raises(t.Unsafe) as excinfo:
        t.preflight(base, {"HOME": str(tmp_path)}, (1000, 1000, []))
    message = str(excinfo.value)
    assert "uid=1000" in message and str(python) in message


@pytest.mark.skipif(os.geteuid() != 0, reason="requires root to spawn as another uid")
def test_spawned_worker_creates_library_files_as_library_owner(tmp_path):
    """以曲库属主身份落盘：文件 0640、目录 0750（unit UMask=0027），无需事后 chown。"""
    import subprocess
    import sys

    library = tmp_path / "library"
    library.mkdir()
    os.chown(library, 1000, 1000)
    library.chmod(0o750)
    code = (
        "import os, sys\n"
        "root = sys.argv[1]\n"
        "open(os.path.join(root, 'track.flac'), 'w').close()\n"
        "os.makedirs(os.path.join(root, 'Artist'), exist_ok=True)\n"
    )
    subprocess.run([sys.executable, "-c", code, str(library)],
                   user=1000, group=1000, extra_groups=[], check=True,
                   preexec_fn=lambda: os.umask(0o027))
    track = library / "track.flac"
    folder = library / "Artist"
    assert track.stat().st_uid == 1000 and folder.stat().st_uid == 1000
    assert stat.S_IMODE(track.stat().st_mode) == 0o640
    assert stat.S_IMODE(folder.stat().st_mode) == 0o750


def test_worker_app_has_no_identity_switching_code():
    """运行期不再降权/提权：seteuid/setegid 与 musicdb 租约不应存在于代理模块。"""
    proxy_dir = Path(__file__).resolve().parents[1]
    for name in ("app.py", "recommend.py", "takeover.py"):
        source = (proxy_dir / name).read_text(encoding="utf-8")
        assert "seteuid(" not in source, name
        assert "setegid(" not in source, name
        assert "privileged_read" not in source, name
    assert not (proxy_dir / "musicdb.py").exists()
