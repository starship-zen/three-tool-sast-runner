#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
if [[ $# -gt 0 ]]; then
    if [[ $# -eq 1 && ( "$1" == "--help" || "$1" == "-h" ) ]]; then
        printf '用法: bash install/preflight.sh（仅检查工具，不读取扫描配置）\n'
        exit 0
    fi
    printf 'error: 不支持额外参数\n' >&2
    exit 2
fi
python3 - "${SCRIPT_DIR}/../scan" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from registry import resolve_tool

failed = False
for name in ("clang-tidy", "cppcheck", "clang", "clang++"):
    try:
        print("[OK] " + name + ": " + resolve_tool(name))
    except ValueError as error:
        print("[MISS] " + str(error))
        failed = True

try:
    import yaml
    version = getattr(yaml, "__version__", None)
    if version == "6.0.3":
        print("[OK] pyyaml: 6.0.3")
    else:
        print("[WARN] pyyaml: " + str(version) + " (推荐锁定版本 6.0.3)")
except ImportError:
    print("[MISS] pyyaml: 未安装 (运行收据校验必须，执行: uv pip install PyYAML==6.0.3)")
    failed = True

sys.exit(1 if failed else 0)
PY
