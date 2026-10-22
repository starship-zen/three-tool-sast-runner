#!/usr/bin/env bash

# 安装 clang-tidy。
#
# 默认走 Ubuntu/Debian 包安装（快、可复现）；无 sudo 环境可改为离线解包：
#   CLANG_TIDY_INSTALL_MODE=deb bash install/clang_tidy/install.sh
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
TOOL_DIR="${SCRIPT_DIR}"
TMP_DIR="${CLANG_TIDY_TMP_DIR:-${TOOL_DIR}/tmp}"
TOOLING_DIR="${TMP_DIR}/tooling"
MODE="${CLANG_TIDY_INSTALL_MODE:-auto}"

APT_PACKAGES_STR="${CLANG_TIDY_APT_PACKAGES:-clang-tidy clang-tools}"
read -r -a APT_PACKAGES <<<"${APT_PACKAGES_STR}"
DEB_PACKAGES_STR="${CLANG_TIDY_DEB_PACKAGES:-clang-tidy-21 clang-tools-21}"
read -r -a DEB_PACKAGES <<<"${DEB_PACKAGES_STR}"

log() { printf '[install][clang-tidy] %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

find_clang_tidy() {
    local candidate path
    if [[ -n "${CLANG_TIDY_BIN:-}" ]]; then
        [[ -x "${CLANG_TIDY_BIN}" ]] || die "CLANG_TIDY_BIN is not executable: ${CLANG_TIDY_BIN}"
        printf '%s\n' "${CLANG_TIDY_BIN}"
        return 0
    fi
    for candidate in clang-tidy clang-tidy-21 clang-tidy-20 clang-tidy-19 clang-tidy-18 \
        clang-tidy-17 clang-tidy-16 clang-tidy-15 clang-tidy-14 clang-tidy-10; do
        if command -v "${candidate}" >/dev/null 2>&1; then
            command -v "${candidate}"
            return 0
        fi
    done
    for path in /usr/lib/llvm-*/bin/clang-tidy "${TOOLING_DIR}"/usr/lib/llvm-*/bin/clang-tidy; do
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

deb_install() {
    mkdir -p "${TOOLING_DIR}"
    log "downloading .deb packages into ${TOOLING_DIR}: ${DEB_PACKAGES[*]}"
    (cd "${TOOLING_DIR}" && apt-get download "${DEB_PACKAGES[@]}")
    local deb
    for deb in "${TOOLING_DIR}"/*.deb; do
        [[ -f "${deb}" ]] || continue
        log "extracting $(basename "${deb}")"
        dpkg-deb -x "${deb}" "${TOOLING_DIR}"
    done
    log "extracted binaries live under ${TOOLING_DIR}/usr/lib/llvm-*/bin"
}

case "${MODE}" in
auto)
    if bin="$(find_clang_tidy)"; then
        log "clang-tidy already available: ${bin}"
    else
        apt_install
    fi
    ;;
apt)
    apt_install
    ;;
deb)
    deb_install
    ;;
skip)
    log "install skipped (CLANG_TIDY_INSTALL_MODE=skip)"
    ;;
*)
    die "unknown CLANG_TIDY_INSTALL_MODE: ${MODE} (auto|apt|deb|skip)"
    ;;
esac

bin="$(find_clang_tidy)" || die "clang-tidy still not found after installation"
log "using clang-tidy: ${bin}"
"${bin}" --version | sed -n '1,3p'
