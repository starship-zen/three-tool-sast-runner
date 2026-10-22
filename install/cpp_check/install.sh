#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
TOOL_DIR="${SCRIPT_DIR}"
SOURCE_DIR="${CPPCHECK_SOURCE_DIR:-${TOOL_DIR}/tmp/cppcheck-source/cppcheck}"
PREFIX="${CPPCHECK_INSTALL_PREFIX:-${HOME:+${HOME}/.local}}"
FILESDIR="${CPPCHECK_FILESDIR:-${PREFIX}/share/Cppcheck}"
JOBS="${CPPCHECK_BUILD_JOBS:-$(nproc)}"
MATCHCOMPILER="${CPPCHECK_MATCHCOMPILER:-yes}"
BUILD_SYSTEM="${CPPCHECK_BUILD_SYSTEM:-make}"
# make 是源码内就地编译，会留下上百 MB 的 .o 与二进制；安装后默认清理，
# 避免把源码目录一起打包交接时体积膨胀。需要保留增量编译时设为 0。
CLEAN_AFTER_BUILD="${CPPCHECK_CLEAN_AFTER_BUILD:-1}"
CMAKE_BUILD_DIR="${CPPCHECK_CMAKE_BUILD_DIR:-${TOOL_DIR}/tmp/cppcheck-cmake-build}"

log() { printf '[install][cppcheck] %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

if [[ -z "${CPPCHECK_SOURCE_DIR:-}" ]]; then
    ARCHIVE="${TOOL_DIR}/source/cppcheck-source.tar.xz"
    CHECKSUM="${TOOL_DIR}/source/cppcheck-source.tar.xz.sha256"
    [[ -f "${ARCHIVE}" && -f "${CHECKSUM}" ]] || die "offline Cppcheck archive or checksum missing"
    command -v sha256sum >/dev/null 2>&1 || die "sha256sum not found"
    (cd "${TOOL_DIR}/source" && sha256sum -c "${CHECKSUM}") || die "Cppcheck source archive checksum mismatch"
    SOURCE_STAMP="$(dirname "${SOURCE_DIR}")/archive.sha256"
    if [[ -f "${SOURCE_DIR}/Makefile" ]]; then
        cmp -s "${CHECKSUM}" "${SOURCE_STAMP}" || die "cached source snapshot differs; remove ${SOURCE_DIR%/cppcheck} before rebuilding"
    else
        mkdir -p "$(dirname "${SOURCE_DIR}")"
        tar -xJf "${ARCHIVE}" -C "$(dirname "${SOURCE_DIR}")"
        cp "${CHECKSUM}" "${SOURCE_STAMP}"
    fi
fi
[[ -d "${SOURCE_DIR}" ]] || die "cppcheck source not found: ${SOURCE_DIR}"
[[ -f "${SOURCE_DIR}/Makefile" ]] || die "cppcheck Makefile not found in ${SOURCE_DIR}"
[[ "${JOBS}" =~ ^[1-9][0-9]*$ ]] || die "CPPCHECK_BUILD_JOBS must be a positive integer"
[[ -n "${PREFIX}" ]] || die "install prefix is empty; set CPPCHECK_INSTALL_PREFIX or HOME"

have_cxx() {
    local candidate
    for candidate in "${CXX:-}" g++ c++ clang++; do
        [[ -n "${candidate}" ]] && command -v "${candidate}" >/dev/null 2>&1 && return 0
    done
    return 1
}

# 删除目录内容（避免依赖 rm -rf）。
clean_dir() {
    local dir="$1"
    [[ -d "${dir}" ]] || return 0
    find "${dir}" -mindepth 1 -delete 2>/dev/null || true
    rmdir "${dir}" 2>/dev/null || true
}

case "${BUILD_SYSTEM}" in
make)
    command -v make >/dev/null 2>&1 || \
        die "make not found; 安装：sudo apt-get install -y make（或 build-essential）"
    have_cxx || \
        die "C++ 编译器 not found; 安装：sudo apt-get install -y build-essential"
    # 在源码目录内就地编译。
    log "building from ${SOURCE_DIR} with make (jobs=${JOBS}, MATCHCOMPILER=${MATCHCOMPILER})"
    make -C "${SOURCE_DIR}" -j"${JOBS}" MATCHCOMPILER="${MATCHCOMPILER}"

    log "installing to PREFIX=${PREFIX} FILESDIR=${FILESDIR}"
    make -C "${SOURCE_DIR}" install PREFIX="${PREFIX}" FILESDIR="${FILESDIR}"

    if [[ "${CLEAN_AFTER_BUILD}" != "0" ]]; then
        log "cleaning build artifacts in ${SOURCE_DIR} (CPPCHECK_CLEAN_AFTER_BUILD=0 可保留)"
        make -C "${SOURCE_DIR}" clean >/dev/null 2>&1 ||
            log "warning: make clean failed; 源码目录可能残留 .o（不影响安装结果）"
        # make clean 会漏掉个别生成物（如 cppcheck Makefile 产出的 "-.o"）。
        find "${SOURCE_DIR}" -name '*.o' -delete 2>/dev/null || true
    fi
    ;;
cmake)
    command -v cmake >/dev/null 2>&1 || \
        die "cmake not found; 安装：sudo apt-get install -y cmake（或设 CPPCHECK_BUILD_SYSTEM=make）"
    have_cxx || \
        die "C++ 编译器 not found; 安装：sudo apt-get install -y build-essential"
    # 源码树外构建，保持 source/cppcheck 目录干净。
    log "building from ${SOURCE_DIR} with CMake (build dir=${CMAKE_BUILD_DIR})"
    cmake -S "${SOURCE_DIR}" -B "${CMAKE_BUILD_DIR}" \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX="${PREFIX}" \
        -DCMAKE_INSTALL_DATADIR="${FILESDIR}" >/dev/null
    cmake --build "${CMAKE_BUILD_DIR}" --parallel "${JOBS}"
    cmake --install "${CMAKE_BUILD_DIR}"

    if [[ "${CLEAN_AFTER_BUILD}" != "0" ]]; then
        log "removing out-of-source build dir ${CMAKE_BUILD_DIR} (CPPCHECK_CLEAN_AFTER_BUILD=0 可保留)"
        clean_dir "${CMAKE_BUILD_DIR}"
    fi
    ;;
*)
    die "unknown CPPCHECK_BUILD_SYSTEM: ${BUILD_SYSTEM} (make|cmake)"
    ;;
esac

BIN="${PREFIX}/bin/cppcheck"
[[ -x "${BIN}" ]] || die "cppcheck binary not found at ${BIN}"
log "installed cppcheck: ${BIN}"
"${BIN}" --version

if [[ -f "${PREFIX}/bin/cppcheck-htmlreport" ]]; then
    log "installed cppcheck-htmlreport: ${PREFIX}/bin/cppcheck-htmlreport"
fi
