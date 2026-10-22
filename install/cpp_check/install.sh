#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
TOOL_DIR="${SCRIPT_DIR}"
DEFAULT_SOURCE_DIR="${TOOL_DIR}/tmp/cppcheck-source/cppcheck"
SOURCE_DIR="${CPPCHECK_SOURCE_DIR:-${DEFAULT_SOURCE_DIR}}"
SOURCE_EXTRACTED_BY_RUN=false
PREFIX="${CPPCHECK_INSTALL_PREFIX:-${HOME:+${HOME}/.local}}"
FILESDIR="${CPPCHECK_FILESDIR:-${PREFIX}/share/Cppcheck}"
JOBS="${CPPCHECK_BUILD_JOBS:-$(nproc)}"
MATCHCOMPILER="${CPPCHECK_MATCHCOMPILER:-yes}"
BUILD_SYSTEM="${CPPCHECK_BUILD_SYSTEM:-make}"
# make 会在源码树留下构建产物；仅本次从固定归档新解出的源码树可自动清理。
# 调用方指定或已有的源码树始终保留；设为 0 可保留本次新解出的树以便增量编译。
CLEAN_AFTER_BUILD="${CPPCHECK_CLEAN_AFTER_BUILD:-1}"
DEFAULT_CMAKE_BUILD_DIR="${TOOL_DIR}/tmp/cppcheck-cmake-build"
# Explicit CMake build-directory overrides are retained because their contents are caller-owned.
CMAKE_BUILD_DIR="${CPPCHECK_CMAKE_BUILD_DIR:-${DEFAULT_CMAKE_BUILD_DIR}}"
CMAKE_BUILD_DIR_CREATED_BY_RUN=false

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
        SOURCE_PATH_WAS_ABSENT=true
        [[ ! -e "${SOURCE_DIR}" && ! -L "${SOURCE_DIR}" ]] || SOURCE_PATH_WAS_ABSENT=false
        mkdir -p "$(dirname "${SOURCE_DIR}")"
        tar -xJf "${ARCHIVE}" -C "$(dirname "${SOURCE_DIR}")"
        cp "${CHECKSUM}" "${SOURCE_STAMP}"
        if [[ "${SOURCE_PATH_WAS_ABSENT}" == true ]]; then
            SOURCE_EXTRACTED_BY_RUN=true
        fi
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

path_is_same_or_child() {
    local candidate="$1"
    local parent="$2"
    [[ "${parent}" == "/" ]] && [[ "${candidate}" == /* ]] && return 0
    [[ "${candidate}" == "${parent}" || "${candidate}" == "${parent}/"* ]]
}

clean_default_cmake_build_dir() {
    local build_real source_real
    if [[ "${CMAKE_BUILD_DIR_CREATED_BY_RUN}" != true || -n "${CPPCHECK_CMAKE_BUILD_DIR:-}" ]]; then
        log "keeping caller-supplied or pre-existing CMake build directory"
        return 0
    fi
    if [[ ! -d "${CMAKE_BUILD_DIR}" || -L "${CMAKE_BUILD_DIR}" || ! -O "${CMAKE_BUILD_DIR}" ]]; then
        log "keeping CMake build directory because its ownership or path is unsafe"
        return 0
    fi
    build_real="$(cd -- "${CMAKE_BUILD_DIR}" 2>/dev/null && pwd -P)" || {
        log "keeping CMake build directory because its resolved path is unavailable"
        return 0
    }
    source_real="$(cd -- "${SOURCE_DIR}" 2>/dev/null && pwd -P)" || {
        log "keeping CMake build directory because the source path cannot be resolved"
        return 0
    }
    if [[ "${build_real}" != "${DEFAULT_CMAKE_BUILD_DIR}" ]] || \
        path_is_same_or_child "${source_real}" "${build_real}" || \
        path_is_same_or_child "${build_real}" "${source_real}"; then
        log "keeping CMake build directory because its resolved path is unsafe"
        return 0
    fi
    log "removing installer-created CMake build artifacts from ${CMAKE_BUILD_DIR}"
    if find "${CMAKE_BUILD_DIR}" -mindepth 1 -delete 2>/dev/null; then
        rmdir "${CMAKE_BUILD_DIR}" 2>/dev/null || true
    else
        log "warning: CMake build directory cleanup was incomplete"
    fi
}

clean_fresh_default_source_artifacts() {
    local source_real
    if [[ "${SOURCE_EXTRACTED_BY_RUN}" != true || "${SOURCE_DIR}" != "${DEFAULT_SOURCE_DIR}" || \
        -L "${SOURCE_DIR}" || ! -O "${SOURCE_DIR}" ]]; then
        log "keeping caller-supplied or pre-existing source directory"
        return 0
    fi
    source_real="$(cd -- "${SOURCE_DIR}" 2>/dev/null && pwd -P)" || {
        log "keeping source directory because its resolved path is unavailable"
        return 0
    }
    if [[ "${source_real}" != "${DEFAULT_SOURCE_DIR}" ]]; then
        log "keeping source directory because its resolved path is unsafe"
        return 0
    fi
    log "cleaning build artifacts in freshly extracted source ${SOURCE_DIR}"
    make -C "${SOURCE_DIR}" clean >/dev/null 2>&1 || \
        log "warning: make clean failed; 源码目录可能残留 .o（不影响安装结果）"
    # The archive was extracted by this run, so this fallback only removes its build output.
    find "${SOURCE_DIR}" -name '*.o' -delete 2>/dev/null || true
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
        clean_fresh_default_source_artifacts
    fi
    ;;
cmake)
    command -v cmake >/dev/null 2>&1 || \
        die "cmake not found; 安装：sudo apt-get install -y cmake（或设 CPPCHECK_BUILD_SYSTEM=make）"
    have_cxx || \
        die "C++ 编译器 not found; 安装：sudo apt-get install -y build-essential"
    # 源码树外构建，保持 source/cppcheck 目录干净。
    log "building from ${SOURCE_DIR} with CMake (build dir=${CMAKE_BUILD_DIR})"
    if [[ -z "${CPPCHECK_CMAKE_BUILD_DIR:-}" && ! -e "${CMAKE_BUILD_DIR}" && ! -L "${CMAKE_BUILD_DIR}" ]]; then
        CMAKE_BUILD_DIR_CREATED_BY_RUN=true
    fi
    cmake -S "${SOURCE_DIR}" -B "${CMAKE_BUILD_DIR}" \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX="${PREFIX}" \
        -DCMAKE_INSTALL_DATADIR="${FILESDIR}" >/dev/null
    cmake --build "${CMAKE_BUILD_DIR}" --parallel "${JOBS}"
    cmake --install "${CMAKE_BUILD_DIR}"

    if [[ "${CLEAN_AFTER_BUILD}" != "0" ]]; then
        clean_default_cmake_build_dir
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
