"""fpk 内置运行时模式（runtime/BUNDLED）的 shell 行为测试。

内置模式下：选 Python/Node/ffmpeg 只用包内文件；ensure_*_deps 离线组装 venv，
全程不调用 pip / apt / sudo，也不联网。无标记（源码安装）时保持上游的宿主机探测。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[2]
BASH = shutil.which("bash")
if BASH is None:
    pytest.skip("bash 不可用", allow_module_level=True)

REAL_PY = os.path.realpath(getattr(sys, "_base_executable", sys.executable))


def _fake_runtime(root: Path, *, python=True, node=True, ffmpeg=True, bundled=True) -> Path:
    rt = root / "runtime"
    (rt / "python" / "bin").mkdir(parents=True)
    if python:
        (rt / "python" / "bin" / "python3").symlink_to(REAL_PY)
    for kind, modules in (("proxy", ("fastapi", "uvicorn", "httpx", "mutagen")),
                          ("sources", ("supervisor", "musicdl", "fastapi", "uvicorn", "httpx",
                                       "curl_cffi", "PIL", "qrcode", "multipart"))):
        site = rt / f"site-{kind}"
        site.mkdir(parents=True)
        for mod in modules:
            (site / mod).mkdir()
            (site / mod / "__init__.py").write_text("", encoding="utf-8")
        scripts = rt / f"scripts-{kind}"
        scripts.mkdir()
        (scripts / f"tool-{kind}").write_text("#!/bin/sh\necho tool\n", encoding="utf-8")
        (scripts / f"tool-{kind}").chmod(0o755)
    if node:
        n = rt / "site-sources" / "nodejs_wheel" / "bin" / "node"
        n.parent.mkdir(parents=True)
        n.write_text('#!/bin/sh\n[ "$1" = "-p" ] && echo 22 || echo v22\n', encoding="utf-8")
        n.chmod(0o755)
    if ffmpeg:
        (rt / "ffmpeg").mkdir()
        for tool in ("ffmpeg", "ffprobe"):
            (rt / "ffmpeg" / tool).write_text("#!/bin/sh\n", encoding="utf-8")
            (rt / "ffmpeg" / tool).chmod(0o755)
    if bundled:
        (rt / "BUNDLED").write_text("{}", encoding="utf-8")
    return rt


def _stub_forbidden(bindir: Path, log: Path) -> None:
    bindir.mkdir(parents=True, exist_ok=True)
    for name in ("pip", "pip3", "apt-get", "apt", "sudo", "curl", "wget"):
        stub = bindir / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\nexit 1\n', encoding="utf-8")
        stub.chmod(0o755)


def test_choose_functions_prefer_bundled(tmp_path):
    rt = _fake_runtime(tmp_path)
    r = subprocess.run(
        [BASH, "-c", f'cd "{BASE}"; source proxy/runtime_common.sh; '
                     'choose_python_bin && echo "$FNMUSIC_SELECTED_PYTHON|$FNMUSIC_PYTHON_SOURCE"; '
                     'choose_node_bin && echo "$FNMUSIC_SELECTED_NODE|$FNMUSIC_NODE_SOURCE"; '
                     'choose_media_bins && echo "$FNMUSIC_SELECTED_FFMPEG|$FNMUSIC_SELECTED_FFPROBE"'],
        env={"PATH": os.environ["PATH"], "BASE_DIR": str(tmp_path)}, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    py, node, media = r.stdout.strip().splitlines()
    assert py == f"{rt}/python/bin/python3|bundled"
    assert node == f"{rt}/site-sources/nodejs_wheel/bin/node|bundled"
    assert media == f"{rt}/ffmpeg/ffmpeg|{rt}/ffmpeg/ffprobe"


def test_bundled_mode_never_falls_back_to_system(tmp_path):
    """内置模式下包内组件损坏要明确失败，而不是悄悄改用系统的 Python/Node/ffmpeg。"""
    _fake_runtime(tmp_path, python=False, node=False, ffmpeg=False)
    r = subprocess.run(
        [BASH, "-c", f'cd "{BASE}"; source proxy/runtime_common.sh; '
                     'choose_python_bin; echo "py=$?"; choose_node_bin; echo "node=$?"; choose_media_bins; echo "ff=$?"'],
        env={"PATH": os.environ["PATH"], "BASE_DIR": str(tmp_path)}, capture_output=True, text=True)
    assert r.stdout.split() == ["py=1", "node=1", "ff=1"], r.stdout + r.stderr


def test_without_marker_keeps_host_probing(tmp_path):
    _fake_runtime(tmp_path, bundled=False)
    r = subprocess.run(
        [BASH, "-c", f'cd "{BASE}"; source proxy/runtime_common.sh; fnmusic_bundled && echo bundled || echo host'],
        env={"PATH": os.environ["PATH"], "BASE_DIR": str(tmp_path)}, capture_output=True, text=True)
    assert r.stdout.strip() == "host"


def test_bundled_make_venv_mounts_site_and_scripts(tmp_path):
    rt = _fake_runtime(tmp_path)
    venv = tmp_path / ".venv-proxy"
    cmd = f'cd "{BASE}"; source proxy/runtime_common.sh; bundled_make_venv "{venv}" proxy'
    r = subprocess.run([BASH, "-c", cmd], env={"PATH": os.environ["PATH"], "BASE_DIR": str(tmp_path)},
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = subprocess.run([str(venv / "bin" / "python"), "-c",
                          "import fastapi, sys; print(fastapi.__file__)"], capture_output=True, text=True)
    assert out.returncode == 0 and str(rt / "site-proxy") in out.stdout, out.stderr
    assert (venv / "bin" / "tool-proxy").stat().st_mode & 0o111
    # 依赖目录变化（应用换卷/重装到别处）：venv 被重建而不是沿用旧挂载
    other = tmp_path / "other"
    _fake_runtime(other)
    r2 = subprocess.run([BASH, "-c", cmd], env={"PATH": os.environ["PATH"], "BASE_DIR": str(other)},
                        capture_output=True, text=True)
    assert r2.returncode == 0, r2.stderr
    out2 = subprocess.run([str(venv / "bin" / "python"), "-c", "import fastapi; print(fastapi.__file__)"],
                          capture_output=True, text=True)
    assert str(other / "runtime" / "site-proxy") in out2.stdout


def test_ensure_proxy_deps_bundled_is_offline_and_never_calls_pip_or_apt(tmp_path):
    base = tmp_path / "repo"
    (base / "proxy").mkdir(parents=True)
    shutil.copy(BASE / "proxy" / "runtime_common.sh", base / "proxy")
    shutil.copy(BASE / "proxy" / "requirements.txt", base / "proxy")
    shutil.copy(BASE / "ensure_proxy_deps.sh", base)
    _fake_runtime(base)
    forbidden_log = tmp_path / "forbidden.log"
    bindir = tmp_path / "bin"
    _stub_forbidden(bindir, forbidden_log)
    r = subprocess.run([BASH, str(base / "ensure_proxy_deps.sh")],
                       env={"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path)},
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "内置" in r.stdout
    assert not forbidden_log.exists(), forbidden_log.read_text()
    assert (base / ".venv-proxy" / "bin" / "python").exists()


def test_ensure_proxy_deps_bundled_fails_loudly_when_dependency_missing(tmp_path):
    base = tmp_path / "repo"
    (base / "proxy").mkdir(parents=True)
    shutil.copy(BASE / "proxy" / "runtime_common.sh", base / "proxy")
    shutil.copy(BASE / "proxy" / "requirements.txt", base / "proxy")
    shutil.copy(BASE / "ensure_proxy_deps.sh", base)
    rt = _fake_runtime(base)
    shutil.rmtree(rt / "site-proxy" / "mutagen")
    r = subprocess.run([BASH, str(base / "ensure_proxy_deps.sh")],
                       env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}, capture_output=True, text=True, timeout=120)
    assert r.returncode != 0
    assert "安装包可能损坏" in (r.stdout + r.stderr)


def test_ensure_sources_native_has_apt_guards_in_bundled_mode():
    text = (BASE / "ensure_sources_native.sh").read_text(encoding="utf-8")
    assert "fnmusic_bundled && return 1" in text          # apt_install_missing
    assert "内置运行时模式不调用 apt" in text                # apt_install_packages
    assert "bundled_make_venv" in text and "SKIP_PIP_INSTALL=1" in text
    assert '"${PYTHON_BIN}" -m zipfile' in text            # 不依赖系统 python3 解压
