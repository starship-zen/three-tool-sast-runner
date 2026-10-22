import common
from clang_tidy import scanner as clang_tidy
from cpp_check import scanner as cpp_check
from clang_static_analyzer import scanner as clang_static_analyzer

SCANNERS = (clang_tidy, cpp_check, clang_static_analyzer)


def resolve_tool(name):
    for scanner in SCANNERS:
        if name in scanner.REQUIRED_TOOLS:
            return scanner.resolve_tools()[name]
    return common.find_tool(name)


if __name__ == "__main__":
    raise SystemExit("Internal tool registry; use a scan.sh entry.")
