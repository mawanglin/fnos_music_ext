#!/usr/bin/env python3
"""为 native-only fpk 准备内置运行时（按架构）。

安装包自带 Python、全部 Python 依赖、Node（随 nodejs-wheel）与 ffmpeg，
NAS 上安装/启动时不再联网、不调用 apt/pip，也不改动系统。

用法:
    fetch_runtime.py --arch x86|arm --cache DIR --out DIR

产物布局（out/ 即包内 app/repo/runtime/）:
    python/              python-build-standalone（install_only_stripped）
    site-proxy/          代理依赖（解压后的 wheel）
    site-sources/        四个音源服务 + supervisor 的依赖，含 nodejs_wheel（node 在其中）
    scripts-proxy/       console_scripts 包装脚本（安装时拷进 .venv-proxy/bin）
    scripts-sources/     同上，用于 .venv-sources/bin（uvicorn/supervisord/musicbox …）
    ffmpeg/              ffmpeg + ffprobe 静态构建 + GPLv3 文本
    BUNDLED              标记文件（JSON）；存在即表示“内置运行时模式”

所有下载都做 sha256 校验；依赖按 requirements-*.lock 的精确版本取目标架构的 wheel，
只有源码包的纯 Python 依赖在构建机本地打成 wheel，且必须是 py3-none-any（否则中止）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOCK = HERE / "runtime" / "runtime.json"

# 解压后可删除的体积大头：nodejs-wheel 自带的 npm / corepack、Node 头文件与 share（运行 lxserver 和 musicdl 只需要 bin/node）
PRUNE = {
    "sources": ("nodejs_wheel/lib", "nodejs_wheel/include", "nodejs_wheel/share"),
}
# 这些 console_scripts 不生成包装（npm 生态工具已随上面的裁剪删除）
SKIP_SCRIPTS = {"npm", "npx", "corepack"}


def log(msg: str) -> None:
    print(f"[runtime] {msg}", flush=True)


def die(msg: str) -> "None":
    print(f"[runtime] ERROR: {msg}", file=sys.stderr)
    raise SystemExit(1)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, dest: Path, expect_sha256: str) -> Path:
    """下载到缓存并校验；缓存命中且校验通过则复用。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and sha256_of(dest) == expect_sha256:
        log(f"缓存命中 {dest.name}")
        return dest
    log(f"下载 {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "fnmusic-ext-build"})
    with urllib.request.urlopen(request, timeout=120) as resp, tmp.open("wb") as out:  # noqa: S310
        shutil.copyfileobj(resp, out, 1 << 20)
    actual = sha256_of(tmp)
    if actual != expect_sha256:
        tmp.unlink(missing_ok=True)
        die(f"{dest.name} 校验和不符：期望 {expect_sha256}，实际 {actual}（上游文件已变更？请核对后更新 runtime.json）")
    tmp.replace(dest)
    return dest


def safe_extract_tar(archive: Path, dest: Path, strip: int = 0) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            parts = Path(member.name).parts[strip:]
            if not parts:
                continue
            target = (dest / Path(*parts)).resolve()
            if root != target and root not in target.parents:
                die(f"归档含越界路径: {member.name}")
            member.name = str(Path(*parts))
            tar.extract(member, dest, filter="tar")


def fetch_python(lock: dict, arch: str, cache: Path, out: Path) -> str:
    spec = lock["python"]
    url = spec["url"].format(version=spec["version"], release=spec["release"], triple=spec["triples"][arch])
    archive = download(url, cache / "python" / Path(url).name.replace("%2B", "+"), spec["sha256"][arch])
    target = out / "python"
    shutil.rmtree(target, ignore_errors=True)
    safe_extract_tar(archive, target, strip=1)  # 归档顶层是 python/
    if not (target / "bin" / "python3").is_file():
        die("python-build-standalone 解压后缺少 bin/python3")
    return spec["version"]


def fetch_ffmpeg(lock: dict, arch: str, cache: Path, out: Path) -> str:
    spec = lock["ffmpeg"]
    url = spec["url"][arch]
    archive = download(url, cache / "ffmpeg" / f"ffmpeg-{spec['version']}-{arch}.tar.xz", spec["sha256"][arch])
    target = out / "ffmpeg"
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    wanted = {"ffmpeg", "ffprobe", "GPLv3.txt", "readme.txt"}
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            name = Path(member.name)
            if member.isfile() and name.name in wanted and len(name.parts) == 2:
                data = tar.extractfile(member)
                assert data is not None
                (target / name.name).write_bytes(data.read())
    for tool in ("ffmpeg", "ffprobe"):
        path = target / tool
        if not path.is_file():
            die(f"ffmpeg 归档缺少 {tool}")
        path.chmod(0o755)
    (target / "SOURCE.txt").write_text(
        "ffmpeg/ffprobe 为 johnvansickle.com 提供的 GPLv3 静态构建（版本 %s）。\n"
        "源码与构建说明: https://johnvansickle.com/ffmpeg/ 与 https://ffmpeg.org/\n"
        "许可证全文见同目录 GPLv3.txt。\n" % spec["version"],
        encoding="utf-8",
    )
    return spec["version"]


def pip(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "pip", *args], text=True, capture_output=True)


def wheel_for(requirement: str, arch: str, lock: dict, wheel_dir: Path) -> Path:
    """取得 requirement 在目标架构的 wheel：优先二进制 wheel，其次本地构建纯 Python wheel。"""
    wheel_dir.mkdir(parents=True, exist_ok=True)
    name = re.split(r"[=<>!~ ]", requirement, maxsplit=1)[0]
    norm = re.sub(r"[-_.]+", "_", name).lower()
    version = requirement.split("==", 1)[1]

    def cached() -> list[Path]:
        return [p for p in wheel_dir.glob("*.whl")
                if re.sub(r"[-_.]+", "_", p.name.split("-")[0]).lower() == norm
                and p.name.split("-")[1] == version]

    hit = cached()
    if hit:
        return hit[0]
    spec = lock["wheels"]
    cmd = ["download", "-q", "--no-deps", "--only-binary=:all:", "-d", str(wheel_dir),
           "--python-version", spec["python_version"], "--implementation", "cp",
           "--abi", "cp" + spec["python_version"].replace(".", ""), "--abi", "abi3", "--abi", "none"]
    for plat in spec["platforms"][arch]:
        cmd += ["--platform", plat]
    cmd.append(requirement)
    if pip(*cmd).returncode == 0 and cached():
        return cached()[0]
    # 没有目标架构的二进制 wheel：只允许是纯 Python 包，在构建机本地打 wheel
    with_sdist = pip("wheel", "-q", "--no-deps", "-w", str(wheel_dir), requirement)
    if with_sdist.returncode != 0:
        die(f"{requirement} 没有 {arch} 的 wheel，本地构建也失败：\n{with_sdist.stderr[-800:]}")
    built = cached()
    if not built:
        die(f"{requirement} 构建后未找到 wheel")
    if not built[0].name.endswith("-none-any.whl"):
        built[0].unlink()
        die(f"{requirement} 没有 {arch} 的二进制 wheel，且源码构建产物不是纯 Python（{built[0].name}）；无法跨架构打包")
    return built[0]


def extract_wheel(wheel: Path, site: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        for info in archive.infolist():
            target = (site / info.filename).resolve()
            if site.resolve() not in target.parents and target != site.resolve():
                die(f"wheel 含越界路径: {info.filename}")
            archive.extract(info, site)
            mode = (info.external_attr >> 16) & 0o777
            if mode & 0o111 and not info.is_dir():
                target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def entry_point_scripts(site: Path) -> dict[str, tuple[str, str]]:
    """扫描 *.dist-info/entry_points.txt 的 console_scripts: 名称 -> (模块, 可调用对象)。"""
    scripts: dict[str, tuple[str, str]] = {}
    for entry in sorted(site.glob("*.dist-info/entry_points.txt")):
        section = ""
        for raw in entry.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1]
                continue
            if section != "console_scripts" or "=" not in line or line.startswith("#"):
                continue
            script, target = (part.strip() for part in line.split("=", 1))
            target = target.split("[", 1)[0].strip()
            module, _, func = target.partition(":")
            if module and func and script not in SKIP_SCRIPTS:
                scripts.setdefault(script, (module, func))
    return scripts


WRAPPER = """#!/bin/sh
# 自动生成：等价于 venv 里的 console_script。通过同目录的 python 启动，保证加载 venv 的 .pth。
D="$(cd "$(dirname "$0")" && pwd)"
exec "$D/python" -c 'import importlib, sys
obj = importlib.import_module("{module}")
for part in "{func}".split("."):
    obj = getattr(obj, part)
sys.exit(obj())' "$@"
"""


def build_site(name: str, arch: str, lock: dict, cache: Path, out: Path) -> int:
    lock_file = HERE / "runtime" / lock["wheels"]["locks"][name]
    site = out / f"site-{name}"
    scripts_dir = out / f"scripts-{name}"
    shutil.rmtree(site, ignore_errors=True)
    shutil.rmtree(scripts_dir, ignore_errors=True)
    site.mkdir(parents=True)
    scripts_dir.mkdir(parents=True)
    count = 0
    for line in lock_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        extract_wheel(wheel_for(line, arch, lock, cache / "wheels" / arch), site)
        count += 1
    for rel in PRUNE.get(name, ()):
        shutil.rmtree(site / rel, ignore_errors=True)
    for script, (module, func) in entry_point_scripts(site).items():
        path = scripts_dir / script
        path.write_text(WRAPPER.format(module=module, func=func), encoding="utf-8")
        path.chmod(0o755)
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--arch", required=True, choices=("x86", "arm"))
    parser.add_argument("--cache", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    args.out.mkdir(parents=True, exist_ok=True)
    py_version = fetch_python(lock, args.arch, args.cache, args.out)
    ff_version = fetch_ffmpeg(lock, args.arch, args.cache, args.out)
    counts = {name: build_site(name, args.arch, lock, args.cache, args.out) for name in ("proxy", "sources")}
    node = args.out / "site-sources" / "nodejs_wheel" / "bin" / "node"
    if not node.is_file():
        die("site-sources 中缺少 nodejs_wheel/bin/node（musicdl 依赖 nodejs-wheel-binaries，锁文件是否被改动？）")
    node.chmod(0o755)
    (args.out / "BUNDLED").write_text(json.dumps({
        "arch": args.arch, "python": py_version, "ffmpeg": ff_version,
        "node": "nodejs_wheel/bin/node", "packages": counts,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log(f"完成：arch={args.arch} python={py_version} ffmpeg={ff_version} 依赖={counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
