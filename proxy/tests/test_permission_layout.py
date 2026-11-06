"""安装面权限布局的回归测试（shell 逻辑，用假身份执行）。

覆盖：venv 祖先目录 0711 + ACL 分支、venv 树 a+rX、`.env` 0660、repo 组可写
清单包含 public_playlists_cache、unit UMask。
"""
import os
import stat
import subprocess
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[2]
SCRIPT = BASE / "ensure_proxy_deps.sh"


def _function_source(name: str) -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index(f"{name}() {{")
    end = text.index("\n}\n", start) + 3
    return text[start:end]


def _run_as_root_shell(body: str, env_extra: dict, *args: str) -> subprocess.CompletedProcess:
    """用假 `id -u` 让函数走 root 分支；stat 属主固定报 0（路径本身属当前用户）。"""
    return subprocess.run(
        ["bash", "-c", body, "_", *args],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, **env_extra},
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission layout")
def test_venv_ancestors_traversable_and_venv_readable(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "id").write_text("#!/bin/sh\necho 0\n")
    (tools / "id").chmod(0o755)
    (tools / "stat").write_text(
        '#!/bin/sh\n'
        'if [ "$1" = "-c" ] && [ "$2" = "%u" ]; then echo 0; exit 0; fi\n'
        'exec /usr/bin/stat "$@"\n')
    (tools / "stat").chmod(0o755)
    calls = tmp_path / "setfacl.log"
    (tools / "setfacl").write_text('#!/bin/sh\necho "$@" >> "$SETFACL_LOG"\n')
    (tools / "setfacl").chmod(0o755)

    base = tmp_path / "app"
    base.mkdir(mode=0o755)
    venv = base / "runtime" / "venv"
    (venv / "bin").mkdir(parents=True)
    runtime_dir = venv.parent
    runtime_dir.chmod(0o700)
    python = venv / "bin" / "python"
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o700)
    lib = venv / "lib" / "pkg"
    lib.mkdir(parents=True)
    (lib / "module.py").write_text("VALUE = 1\n")
    (lib / "module.py").chmod(0o600)

    body = (
        "set -eu\n"
        "VENV_DIR=\"$1\"\n"
        "BASE_DIR=\"$2\"\n"
        "log_info() { :; }\n"
        "log_warn() { :; }\n"
        + _function_source("ensure_proxy_venv_access")
        + "\nensure_proxy_venv_access\n"
    )
    result = _run_as_root_shell(body, {
        "PATH": f"{tools}:{os.environ['PATH']}",
        "SETFACL_LOG": str(calls),
    }, str(venv), str(base))
    assert result.returncode == 0, result.stderr
    # 祖先目录：运行身份可遍历（0711），共享上级不被改动
    assert stat.S_IMODE(runtime_dir.stat().st_mode) == 0o711
    assert stat.S_IMODE(base.stat().st_mode) == 0o755
    # venv 树：目录 0755、文件 0644，可执行文件保留 x
    assert stat.S_IMODE((venv / "lib").stat().st_mode) == 0o755
    assert stat.S_IMODE((lib / "module.py").stat().st_mode) == 0o644
    assert stat.S_IMODE(python.stat().st_mode) & 0o111 == 0o111
    # ACL 分支：目录被显式放行给容器 appuser
    acl = calls.read_text(encoding="utf-8")
    assert "o::x" in acl and "u:1000:x" in acl


def test_venv_access_skips_when_not_root(tmp_path):
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    python = venv / "bin" / "python"
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o700)
    body = (
        "set -eu\n"
        "VENV_DIR=\"$1\"\n"
        "BASE_DIR=\"$2\"\n"
        "log_info() { :; }\n"
        "log_warn() { :; }\n"
        + _function_source("ensure_proxy_venv_access")
        + "\nensure_proxy_venv_access\n"
    )
    result = subprocess.run(["bash", "-c", body, "_", str(venv), str(tmp_path)],
                            capture_output=True, text=True, timeout=60,
                            env={**os.environ, "PATH": f"/usr/bin:/bin"})
    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(python.stat().st_mode) == 0o700


def test_install_surface_permissions_are_wired():
    install = (BASE / "install.sh").read_text(encoding="utf-8")
    assert 'chmod 0660 "${ENV_PATH}"' in install
    assert "0640 \"${ENV_PATH}\"" not in install
    assert "public_playlists_cache" in install
    group_writable = install.split("for _d in ", 1)[1].split("; do", 1)[0]
    assert "public_playlists_cache" in group_writable
    assert (BASE / "fnmusic-ext.service").read_text(encoding="utf-8").count("UMask=0027") == 1
    assert "0640" not in (BASE / "fnmusic-ext.service").read_text(encoding="utf-8")


def test_env_writer_default_is_group_writable(tmp_path):
    import sys

    sys.path.insert(0, str(BASE))
    from proxy import env_merge

    out = tmp_path / ".env"
    env_merge.write_env_atomic(out, "A='1'\n")
    assert stat.S_IMODE(out.stat().st_mode) == 0o660
