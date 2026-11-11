#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# fnmusic-ext fpk 打包脚本（本地与 CI 共用）
# 组装应用骨架 + 仓库主体 → fnpack build → dist/fnmusic-ext-<版本>.fpk
#
# native-only：每个架构各出一个 fpk，内置 Python/依赖/Node/ffmpeg（见 fetch_runtime.py），
# NAS 上安装时不联网、不调用 apt/pip、不改动系统。产物 dist/fnmusic-ext-<版本>-<x86|arm>.fpk
#
# 用法:
#   packaging/fpk/build.sh                     # x86 + arm 都打（fnpack 缺失时自动下载）
#   packaging/fpk/build.sh --arch x86|arm|all  # 指定架构
#   packaging/fpk/build.sh --stage-only DIR    # 只组装不打包（结构测试用，默认 x86 且不拉运行时）
#   packaging/fpk/build.sh --no-runtime        # 不下载内置运行时（仅结构测试）
#   packaging/fpk/build.sh --fnpack /path/fnpack --out dist
# 环境变量: FNPACK_VERSION（默认 1.2.3）、FNMUSIC_BUILD_CACHE（运行时下载缓存目录）
# ==============================================================================

FPK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${FPK_DIR}/../.." && pwd)"
VERSION="$(python3 "${REPO_ROOT}/proxy/version.py" --format version)"
if [ -z "${VERSION}" ]; then
    echo "ERROR: 无法读取 ${REPO_ROOT}/VERSION" >&2
    exit 1
fi
python3 "${REPO_ROOT}/scripts/check_music_compat.py" --root "${REPO_ROOT}"

FNPACK_BIN="${FNPACK:-}"
STAGE=""
STAGE_ONLY=0
ARCH_ARG=""
NO_RUNTIME=0
OUT_DIR="${REPO_ROOT}/dist"

while [ $# -gt 0 ]; do
    case "$1" in
        --stage-only)
            [ $# -ge 2 ] || { echo "用法: $0 --stage-only <dir>" >&2; exit 1; }
            STAGE_ONLY=1
            STAGE="$2"
            shift 2
            ;;
        --arch)
            [ $# -ge 2 ] || { echo "用法: $0 --arch x86|arm|all" >&2; exit 1; }
            ARCH_ARG="$2"
            shift 2
            ;;
        --no-runtime)
            NO_RUNTIME=1
            shift
            ;;
        --fnpack)
            [ $# -ge 2 ] || { echo "用法: $0 --fnpack <path>" >&2; exit 1; }
            FNPACK_BIN="$2"
            shift 2
            ;;
        --out)
            [ $# -ge 2 ] || { echo "用法: $0 --out <dir>" >&2; exit 1; }
            OUT_DIR="$2"
            shift 2
            ;;
        -h|--help)
            sed -n '3,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "未知参数: $1" >&2
            exit 1
            ;;
    esac
done

# 架构：x86 / arm / all。--stage-only 只组装一个架构（默认 x86），且默认不下载内置运行时。
if [ "${STAGE_ONLY}" -eq 1 ]; then
    ARCHES="${ARCH_ARG:-x86}"
    if [ "${FNMUSIC_STAGE_RUNTIME:-0}" != "1" ]; then NO_RUNTIME=1; fi
else
    ARCHES="${ARCH_ARG:-all}"
fi
[ "${ARCHES}" = "all" ] && ARCHES="x86 arm"
for _a in ${ARCHES}; do
    case "${_a}" in
        x86|arm) ;;
        *) echo "ERROR: --arch 只能是 x86、arm 或 all（收到: ${_a}）" >&2; exit 1 ;;
    esac
done
if [ "${STAGE_ONLY}" -eq 1 ] && [ "$(echo ${ARCHES} | wc -w)" -ne 1 ]; then
    echo "ERROR: --stage-only 只能组装单个架构" >&2
    exit 1
fi

command -v rsync >/dev/null 2>&1 || {
    echo "ERROR: 需要 rsync（Debian: sudo apt-get install -y rsync）" >&2
    exit 1
}

# 确保 lxserver 预编译包已在构建缓存就绪（内嵌到 fpk 中实现 NAS 用户离线安装）
BUILD_CACHE="${FPK_DIR}/.build/cache"
RUNTIME_CACHE="${FNMUSIC_BUILD_CACHE:-${FPK_DIR}/.build/runtime-cache}"
mkdir -p "${BUILD_CACHE}" "${RUNTIME_CACHE}"
if [ -f "${REPO_ROOT}/scripts/update_lxserver.sh" ]; then
    echo "[fpk] 检查构建缓存中的 lxserver 预编译包..."
    bash "${REPO_ROOT}/scripts/update_lxserver.sh" "" "${BUILD_CACHE}" || echo "[WARN] 缓存预编译包检查失败"
fi
case " ${ARCHES} " in
    *" arm "*)
        echo "[fpk] 检查 lxserver ARM64 原生模块..."
        if ! bash "${REPO_ROOT}/container/ensure_lxserver_native.sh" \
            --artifact-dir "${BUILD_CACHE}" \
            --version-file "${REPO_ROOT}/container/lxserver.version" \
            --required-arch arm64 --download-only; then
            if [ "${STAGE_ONLY}" -eq 1 ]; then
                echo "[WARN] 离线组装缺少 ARM64 原生模块；正式打包会强制失败"
            else
                exit 1
            fi
        fi
        ;;
esac

# ------------------------------------------------------------------------------
# 1. 按架构组装打包目录
# ------------------------------------------------------------------------------
stage_arch() {
    local ARCH="$1"
    if [ "${STAGE_ONLY}" -ne 1 ]; then
        STAGE="${FPK_DIR}/.build/fpk-${ARCH}"
    fi
    rm -rf "${STAGE}"
    mkdir -p "${STAGE}/app/repo" "${STAGE}/app/ui"

# 仓库主体（排除开发文件与本地秘密；.env.example 保留作模板）
rsync -a --delete \
    --include='.env.example' \
    --exclude='.env' --exclude='.env.*' \
    --exclude='.git/' --exclude='.git*' \
    --exclude='.github/' --exclude='.zcode/' --exclude='.cursor/' --exclude='.agents/' \
    --exclude='.tasks_archive/' --exclude='.tools/' \
    --exclude='packaging/' --exclude='dist/' \
    --exclude='tests/' --exclude='docs/' --exclude='pytest.ini' \
    --exclude='CONTRIBUTING.md' --exclude='AGENTS.md' --exclude='CLAUDE.md' \
    --exclude='test_*.py' --exclude='test_*.js' --exclude='conftest.py' --exclude='smoke_test.sh' --exclude='run_tests.py' \
    --exclude='.venv*/' --exclude='__pycache__/' --exclude='.pytest_cache/' \
    --exclude='*.pyc' --exclude='*.log' --exclude='*.logzip' \
    --exclude='fnmusic-logs-*-????????.zip' --exclude='logs/' --exclude='/logs' \
    --exclude='*.part' --exclude='.DS_Store' \
    --exclude='cache/' --exclude='sources-data/' --exclude='backup/' \
    --exclude='musicbox-data/' --exclude='musicdl_outputs/' \
    --exclude='recommend_cache/' --exclude='public_playlists_cache/' --exclude='play_history/' \
    --exclude='online_favorites/' --exclude='online_favorites.json' \
    --exclude='llm_usage.json' --exclude='llm_usage.json.*' \
    --exclude='playlist_tracks/' --exclude='album_registry.json' --exclude='bind_registry.json' \
    --exclude='*.fpk' \
    --exclude='/docker-compose.yml' --exclude='.dockerignore' --exclude='/ensure_base_image.sh' \
    --exclude='/container/Dockerfile' --exclude='/container/supervisord.conf' \
    "${REPO_ROOT}/" "${STAGE}/app/repo/"

# 注入预编译包至打包 staging 目录，实现 fpk 离线内嵌而不污染源码树
    if [ -f "${REPO_ROOT}/container/lxserver.version" ]; then
        # shellcheck disable=SC1090
        source "${REPO_ROOT}/container/lxserver.version"
        CACHED_ZIP="${BUILD_CACHE}/${LXSERVER_ZIP_NAME:-}"
        if [ -f "${CACHED_ZIP}" ]; then
            mkdir -p "${STAGE}/app/repo/container/lxserver-artifact"
            cp -p "${CACHED_ZIP}" "${STAGE}/app/repo/container/lxserver-artifact/"
            echo "[fpk] 已将离线预编译包注入至打包目标目录: ${LXSERVER_ZIP_NAME}"
        fi
        # lxserver 的 x64 原生模块随上游 zip；ARM64 包额外内嵌锁定的 arm64 绑定（x86 包不需要）
        if [ "${ARCH}" = "arm" ]; then
            ARM64_NATIVE="${LXSERVER_NATIVE_VERSION:+music-tag-native-linux-arm64-gnu-${LXSERVER_NATIVE_VERSION}.tgz}"
            if [ -n "${ARM64_NATIVE}" ] && [ -f "${BUILD_CACHE}/${ARM64_NATIVE}" ]; then
                cp -p "${BUILD_CACHE}/${ARM64_NATIVE}" "${STAGE}/app/repo/container/lxserver-artifact/"
                echo "[fpk] 已将 ARM64 原生模块注入至打包目标目录: ${ARM64_NATIVE}"
            elif [ "${STAGE_ONLY}" -eq 1 ]; then
                echo "[WARN] 打包目标目录缺少 ARM64 原生模块: ${ARM64_NATIVE:-}"
            else
                echo "ERROR: 缺少 ARM64 原生模块 ${ARM64_NATIVE:-}" >&2
                exit 1
            fi
        fi
    fi

    # 内置运行时（Python/依赖/Node/ffmpeg）：安装时无需联网、不调用 apt/pip
    if [ "${NO_RUNTIME}" -eq 1 ]; then
        echo "[WARN] 跳过内置运行时（--no-runtime）：该包只用于结构检查，不能安装使用"
    else
        python3 "${FPK_DIR}/fetch_runtime.py" --arch "${ARCH}" --cache "${RUNTIME_CACHE}" \
            --out "${STAGE}/app/repo/runtime"
    fi

    # 正式和预发布的 manifest、包内 VERSION、文件名均保留数字 -N；通道另存 RELEASE_CHANNEL。
    sed -e "s/@VERSION@/${VERSION}/" -e "s/@PLATFORM@/${ARCH}/" "${FPK_DIR}/manifest.in" > "${STAGE}/manifest"
    cp "${FPK_DIR}/ICON.PNG" "${FPK_DIR}/ICON_256.PNG" "${STAGE}/"
    cp -a "${FPK_DIR}/app/ui/." "${STAGE}/app/ui/"
    cp -a "${FPK_DIR}/cmd" "${STAGE}/cmd"
    cp -a "${FPK_DIR}/config" "${STAGE}/config"
    cp -a "${FPK_DIR}/wizard" "${STAGE}/wizard"
    chmod +x "${STAGE}"/cmd/*
    # 内置运行时里的可执行文件显式 0755（不依赖打包/解压时的 umask）
    if [ -d "${STAGE}/app/repo/runtime" ]; then
        find "${STAGE}/app/repo/runtime/python/bin" "${STAGE}/app/repo/runtime/ffmpeg" \
            "${STAGE}/app/repo/runtime/scripts-proxy" "${STAGE}/app/repo/runtime/scripts-sources" \
            -maxdepth 1 -type f -exec chmod 0755 {} +
        chmod 0755 "${STAGE}/app/repo/runtime/site-sources/nodejs_wheel/bin/node"
        # 代理会降权到曲库属主运行：运行时目录必须对 group/other 可读可进入，不依赖构建机 umask
        chmod -R u+rwX,go+rX "${STAGE}/app/repo/runtime"
    fi

    echo "[fpk] 组装完成: ${STAGE} (version=${VERSION}, arch=${ARCH})"
}

# ------------------------------------------------------------------------------
# 2. fnpack 打包
# ------------------------------------------------------------------------------
if [ "${STAGE_ONLY}" -eq 1 ]; then
    for ARCH in ${ARCHES}; do stage_arch "${ARCH}"; done
    exit 0
fi

if [ -z "${FNPACK_BIN}" ]; then
    if command -v fnpack >/dev/null 2>&1; then
        FNPACK_BIN="$(command -v fnpack)"
    else
        case "$(uname -m)" in
            x86_64) FNARCH=amd64 ;;
            aarch64|arm64) FNARCH=arm64 ;;
            *) echo "ERROR: 不支持的架构 $(uname -m)" >&2; exit 1 ;;
        esac
        FNPACK_VERSION="${FNPACK_VERSION:-1.2.3}"
        TOOLS_DIR="${REPO_ROOT}/.tools"
        FNPACK_BIN="${TOOLS_DIR}/fnpack"
        if [ ! -x "${FNPACK_BIN}" ] || ! "${FNPACK_BIN}" --help >/dev/null 2>&1; then
            mkdir -p "${TOOLS_DIR}"
            echo "[fpk] 下载 fnpack ${FNPACK_VERSION} (linux-${FNARCH})..."
            curl -fsSL -o "${FNPACK_BIN}" \
                "https://static2.fnnas.com/fnpack/fnpack-${FNPACK_VERSION}-linux-${FNARCH}"
            chmod +x "${FNPACK_BIN}"
        fi
    fi
fi

mkdir -p "${OUT_DIR}"
for ARCH in ${ARCHES}; do
    stage_arch "${ARCH}"
    WORK_DIR="$(mktemp -d)"
    if ! (cd "${WORK_DIR}" && "${FNPACK_BIN}" build --directory "${STAGE}"); then
        rm -rf "${WORK_DIR}"
        echo "ERROR: fnpack build 失败 (${ARCH})" >&2
        exit 1
    fi
    BUILT="$(find "${WORK_DIR}" "${STAGE}" -maxdepth 2 -name '*.fpk' -print -quit 2>/dev/null || true)"
    if [ -z "${BUILT}" ]; then
        rm -rf "${WORK_DIR}"
        echo "ERROR: 未找到 fnpack 生成的 .fpk 文件 (${ARCH})" >&2
        exit 1
    fi
    FINAL="${OUT_DIR}/fnmusic-ext-${VERSION}-${ARCH}.fpk"
    mv "${BUILT}" "${FINAL}"
    rm -rf "${WORK_DIR}"
    ( cd "$(dirname "${FINAL}")" && sha256sum "$(basename "${FINAL}")" > "$(basename "${FINAL}").sha256" )
    echo "[fpk] 打包完成: ${FINAL} ($(( $(stat -c %s "${FINAL}") / 1048576 )) MB)"
done
