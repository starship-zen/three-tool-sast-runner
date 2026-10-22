#!/usr/bin/env bash

# 安装 Clang Static Analyzer（scan-build）及其编译器依赖。
#
# scan-build 随 clang-tools 提供，与 clang / clang++ 同一 LLVM 工具链。
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
TOOL_DIR="${SCRIPT_DIR}"
MODE="${STATIC_ANALYZER_INSTALL_MODE:-auto}"
APT_PACKAGES_STR="${STATIC_ANALYZER_APT_PACKAGES:-clang clang-tools}"
read -r -a APT_PACKAGES <<<"${APT_PACKAGES_STR}"

log() { printf '[install][scan-build] %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

find_first() {
    local explicit="$1"
    shift
    local candidate path
    if [[ -n "${explicit}" ]]; then
        [[ -x "${explicit}" ]] || die "not executable: ${explicit}"
        printf '%s\n' "${explicit}"
        return 0
    fi
    for candidate in "$@"; do
        if command -v "${candidate}" >/dev/null 2>&1; then
            command -v "${candidate}"
            return 0
        fi
    done
    return 1
}

find_scan_build() {
    local result path
    if result="$(find_first "${SCAN_BUILD_BIN:-}" scan-build scan-build-21 scan-build-20 \
        scan-build-19 scan-build-18 scan-build-17 scan-build-16 scan-build-15 scan-build-10)"; then
        printf '%s\n' "${result}"
        return 0
    fi
    for path in /usr/lib/llvm-*/bin/scan-build; do
        if [[ -x "${path}" ]]; then
            printf '%s\n' "${path}"
            return 0
        fi
    done
    return 1
}

apt_install() {
    local -a sudo_cmd=()
    [[ "$(id -u)" -ne 0 ]] && sudo_cmd=(sudo)
    log "installing packages via apt: ${APT_PACKAGES[*]}"
    "${sudo_cmd[@]}" apt-get update
    "${sudo_cmd[@]}" apt-get install -y "${APT_PACKAGES[@]}"
}

case "${MODE}" in
auto)
    if scan_build="$(find_scan_build)"; then
        log "scan-build already available: ${scan_build}"
    else
        apt_install
    fi
    ;;
apt)
    apt_install
    ;;
skip)
    log "install skipped (STATIC_ANALYZER_INSTALL_MODE=skip)"
    ;;
*)
    die "unknown STATIC_ANALYZER_INSTALL_MODE: ${MODE} (auto|apt|skip)"
    ;;
esac

scan_build="$(find_scan_build)" || die "scan-build still not found after installation"
clang_bin="$(find_first "${CLANG_BIN:-}" clang /usr/bin/clang)" || die "clang not found"
clangxx_bin="$(find_first "${CLANGXX_BIN:-}" clang++ /usr/bin/clang++)" || die "clang++ not found"

log "scan-build: ${scan_build}"
log "clang:      ${clang_bin}"
log "clang++:    ${clangxx_bin}"
"${scan_build}" --help >/dev/null 2>&1 || die "scan-build is not runnable: ${scan_build}"
log "scan-build is ready"
