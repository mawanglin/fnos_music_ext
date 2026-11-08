"""fetch_runtime.py（fpk 内置运行时准备脚本）的离线单元测试：不联网、不依赖 pip 下载。"""

import hashlib
import importlib.util
import io
import json
import stat
import sys
import zipfile
from pathlib import Path

import pytest

FPK_DIR = Path(__file__).resolve().parents[2] / "packaging" / "fpk"
spec = importlib.util.spec_from_file_location("fetch_runtime", FPK_DIR / "fetch_runtime.py")
fr = importlib.util.module_from_spec(spec)
sys.modules["fetch_runtime"] = fr
spec.loader.exec_module(fr)


def _wheel(path: Path, files: dict[str, bytes], exec_names=()):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            info = zipfile.ZipInfo(name)
            mode = 0o755 if name in exec_names else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            zf.writestr(info, data)


def test_lock_file_is_well_formed():
    lock = json.loads((FPK_DIR / "runtime" / "runtime.json").read_text(encoding="utf-8"))
    for arch in ("x86", "arm"):
        assert len(lock["python"]["sha256"][arch]) == 64
        assert len(lock["ffmpeg"]["sha256"][arch]) == 64
        assert lock["wheels"]["platforms"][arch]
    for name in lock["wheels"]["locks"].values():
        lines = [l for l in (FPK_DIR / "runtime" / name).read_text(encoding="utf-8").splitlines() if l.strip()]
        assert lines and all("==" in l for l in lines), f"{name} 必须是精确版本锁"
    sources = (FPK_DIR / "runtime" / lock["wheels"]["locks"]["sources"]).read_text(encoding="utf-8")
    # Node 随 nodejs-wheel-binaries 内置（lxserver 与 musicdl 共用），锁里不能丢
    assert "nodejs-wheel-binaries==" in sources
    for required in ("supervisor==", "musicdl==", "uvicorn==", "fastapi==", "NetEase-MusicBox=="):
        assert required in sources, required


def test_download_verifies_sha256_and_rejects_mismatch(tmp_path, monkeypatch):
    payload = b"runtime-bytes"

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(fr.urllib.request, "urlopen", lambda *a, **k: Resp(payload))
    dest = tmp_path / "x.bin"
    ok = fr.download("https://example.invalid/x", dest, hashlib.sha256(payload).hexdigest())
    assert ok.read_bytes() == payload
    # 缓存命中：不再请求网络
    monkeypatch.setattr(fr.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应联网")))
    fr.download("https://example.invalid/x", dest, hashlib.sha256(payload).hexdigest())
    # 校验和不符：中止并清理半成品
    monkeypatch.setattr(fr.urllib.request, "urlopen", lambda *a, **k: Resp(b"tampered"))
    bad = tmp_path / "y.bin"
    with pytest.raises(SystemExit):
        fr.download("https://example.invalid/y", bad, hashlib.sha256(payload).hexdigest())
    assert not bad.exists() and not (tmp_path / "y.bin.part").exists()


def test_extract_wheel_keeps_exec_bit_and_blocks_traversal(tmp_path):
    whl = tmp_path / "a-1-py3-none-any.whl"
    _wheel(whl, {"pkg/bin/tool": b"#!/bin/sh\n", "pkg/data.txt": b"x"}, exec_names={"pkg/bin/tool"})
    site = tmp_path / "site"
    site.mkdir()
    fr.extract_wheel(whl, site)
    assert (site / "pkg/bin/tool").stat().st_mode & 0o111
    assert not (site / "pkg/data.txt").stat().st_mode & 0o111
    evil = tmp_path / "evil-1-py3-none-any.whl"
    _wheel(evil, {"../escape.txt": b"x"})
    with pytest.raises(SystemExit):
        fr.extract_wheel(evil, site)


def test_entry_point_scripts_and_wrappers(tmp_path):
    site = tmp_path / "site"
    dist = site / "demo-1.0.dist-info"
    dist.mkdir(parents=True)
    (dist / "entry_points.txt").write_text(
        "[console_scripts]\ndemo = demo.cli:main\nnpm = nodejs_wheel:npm\n"
        "dotted = demo.cli:Cli.run [extra]\n[gui_scripts]\ngui = demo.gui:main\n", encoding="utf-8")
    scripts = fr.entry_point_scripts(site)
    assert scripts == {"demo": ("demo.cli", "main"), "dotted": ("demo.cli", "Cli.run")}  # npm 被跳过，gui 不收
    text = fr.WRAPPER.format(module="demo.cli", func="Cli.run")
    assert 'exec "$D/python"' in text and 'importlib.import_module("demo.cli")' in text


def test_wheel_for_rejects_non_pure_python_sdist(tmp_path, monkeypatch):
    """目标架构没有二进制 wheel 时，只允许本地构建纯 Python wheel；带原生扩展的必须中止。"""
    lock = {"wheels": {"python_version": "3.13", "platforms": {"arm": ["manylinux2014_aarch64"]}}}
    wheel_dir = tmp_path / "w"
    calls = []

    class Done:
        def __init__(self, rc=0, err=""): self.returncode, self.stderr = rc, err

    def fake_pip(*args):
        calls.append(args[0])
        if args[0] == "download":
            return Done(1, "no matching distribution")
        # pip wheel：产出一个平台相关 wheel（模拟含 C 扩展的 sdist）
        _wheel(wheel_dir / "native_pkg-1.0-cp313-cp313-linux_x86_64.whl", {"m.so": b"x"})
        return Done(0)

    monkeypatch.setattr(fr, "pip", fake_pip)
    with pytest.raises(SystemExit):
        fr.wheel_for("native_pkg==1.0", "arm", lock, wheel_dir)
    assert calls == ["download", "wheel"]
    assert not list(wheel_dir.glob("*.whl")), "不合格的构建产物必须被删除"

    def fake_pure(*args):
        if args[0] == "download":
            return Done(1, "none")
        _wheel(wheel_dir / "pure_pkg-2.0-py3-none-any.whl", {"p.py": b"x"})
        return Done(0)

    monkeypatch.setattr(fr, "pip", fake_pure)
    assert fr.wheel_for("pure_pkg==2.0", "arm", lock, wheel_dir).name == "pure_pkg-2.0-py3-none-any.whl"
