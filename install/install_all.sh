#!/usr/bin/env bash

# 一键安装三套静态分析工具：clang-tidy、Clang Static Analyzer、Cppcheck。
#
# 安装完成后会自动跑一次环境自检（preflight.sh），并提示下一步。
#
# 用法：
#   bash install/install_all.sh
#   bash install/install_all.sh --only clang_tidy,cpp_check
set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

ALL_TOOLS=(clang_tidy clang_static_analyzer cpp_check)
selected=()

usage() {
    cat <<EOF
用法: bash ${BASH_SOURCE[0]} [--only TOOL[,TOOL...]]

  --only clang_tidy,cpp_check   只安装指定工具
  可用工具: ${ALL_TOOLS[*]}

安装行为由各工具的环境变量控制，例如：
  CLANG_TIDY_INSTALL_MODE=deb       无 sudo 时离线解包
  CPPCHECK_INSTALL_PREFIX=/opt/tools 自定义 cppcheck 安装前缀
详见 README.zh-CN.md。
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
    -h | --help)
        usage
        exit 0
        ;;
    --only)
        [[ $# -ge 2 ]] || {
            printf 'error: --only 需要一个参数\n' >&2
            exit 1
        }
        IFS=',' read -r -a selected <<<"$2"
        shift 2
        ;;
    --only=*)
        IFS=',' read -r -a selected <<<"${1#--only=}"
        shift
        ;;
    *)
        printf 'error: 未知参数: %s\n' "$1" >&2
        usage >&2
        exit 1
        ;;
    esac
done

[[ "${#selected[@]}" -eq 0 ]] && selected=("${ALL_TOOLS[@]}")
for tool in "${selected[@]}"; do
    known=0
    for item in "${ALL_TOOLS[@]}"; do
        [[ "${tool}" == "${item}" ]] && known=1
    done
    [[ "${known}" -eq 1 ]] || {
        printf 'error: 未知工具: %s（可用: %s）\n' "${tool}" "${ALL_TOOLS[*]}" >&2
        exit 1
    }
done

printf '== 安装前环境自检 ==\n'
bash "${SCRIPT_DIR}/preflight.sh" || true

status=0
for tool in "${selected[@]}"; do
    printf '\n=== install: %s ===\n' "${tool}"
    if ! bash "${SCRIPT_DIR}/${tool}/install.sh"; then
        printf 'error: %s install failed\n' "${tool}" >&2
        status=1
    fi
done

printf '\n== 安装后环境自检 ==\n'
if bash "${SCRIPT_DIR}/preflight.sh"; then
    printf '\n下一步：\n'
    printf '  1) 复制根目录 analy.config.example 为 %s，仅填写 scan_paths / result_path\n' \
        "${SCRIPT_DIR}/../scan/analy.config"
    printf '  2) 正式扫描：bash %s/scan_all.sh\n' "${SCRIPT_DIR}/../scan"
else
    printf '\n仍有依赖缺失，按上面的 [MISS] 提示处理后重跑本脚本。\n'
    status=1
fi

exit "${status}"
