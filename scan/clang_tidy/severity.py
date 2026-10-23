#!/usr/bin/env python3
"""Build a severity-classified, actionable remediation list from clang-tidy logs."""

from __future__ import annotations

import argparse
import csv
import glob
import re
import collections
from pathlib import Path

WARNING_RE = re.compile(
    r"^(?P<file>[^\n]+?):(?P<line>\d+):(?P<col>\d+):\s+warning:\s+"
    r"(?P<msg>.*?)(?:\n|\s)+\[(?P<check>[^\]]+)\]",
    re.MULTILINE | re.DOTALL,
)

# severity: P0 = 内存/UB/崩溃, P1 = 逻辑/正确性, P2 = 性能
SEVERITY = {
    # ---- P0 ----
    "clang-analyzer-security.insecureAPI.strcpy": "P0",
    "clang-analyzer-security.insecureAPI.bzero": "P0",
    "clang-analyzer-cplusplus.NewDelete": "P0",
    "clang-analyzer-unix.MismatchedDeallocator": "P0",
    "clang-analyzer-core.CallAndMessage": "P0",
    "clang-analyzer-core.UndefinedBinaryOperatorResult": "P0",
    "clang-analyzer-optin.cplusplus.UninitializedObject": "P0",
    "bugprone-undefined-memory-manipulation": "P0",
    "bugprone-use-after-move": "P0",
    "bugprone-not-null-terminated-result": "P0",
    "bugprone-sizeof-expression": "P0",
    "bugprone-unused-raii": "P0",
    "bugprone-exception-escape": "P0",
    "bugprone-string-integer-assignment": "P0",
    "bugprone-too-small-loop-variable": "P0",
    # ---- P1 ----
    "bugprone-integer-division": "P1",
    "bugprone-signed-char-misuse": "P1",
    "bugprone-misplaced-widening-cast": "P1",
    "bugprone-incorrect-roundings": "P1",
    "bugprone-suspicious-string-compare": "P1",
    "bugprone-suspicious-enum-usage": "P1",
    "bugprone-unused-return-value": "P1",
    "bugprone-unhandled-self-assignment": "P1",
    "bugprone-string-constructor": "P1",
    "bugprone-undelegated-constructor": "P1",
    "clang-analyzer-optin.cplusplus.VirtualCall": "P1",
    "clang-analyzer-deadcode.DeadStores": "P1",
    "performance-noexcept-move-constructor": "P1",
    "performance-move-const-arg": "P1",
    # ---- P2 ----
    "performance-for-range-copy": "P2",
    "performance-faster-string-find": "P2",
    "performance-inefficient-string-concatenation": "P2",
    "performance-inefficient-vector-operation": "P2",
    "performance-type-promotion-in-math-fn": "P2",
    "performance-trivially-destructible": "P2",
}

ACTION = {
    "clang-analyzer-security.insecureAPI.strcpy": "改为 snprintf/std::string 或带长度的接口，确认目标缓冲区大小并保证 NUL 结尾",
    "clang-analyzer-security.insecureAPI.bzero": "用 memset 替换 bzero，或按平台兼容性显式保留",
    "clang-analyzer-cplusplus.NewDelete": "核对 new/delete 与 new[]/delete[] 配对，改用智能指针",
    "clang-analyzer-unix.MismatchedDeallocator": "统一分配/释放方式（malloc/free、new/delete、strdup/自定义），勿混用",
    "clang-analyzer-core.CallAndMessage": "调用前判空并初始化实参，补空指针/未初始化检查",
    "clang-analyzer-core.UndefinedBinaryOperatorResult": "初始化参与运算的变量，确认分支已覆盖赋值",
    "clang-analyzer-optin.cplusplus.UninitializedObject": "在构造函数初始化列表中初始化全部成员",
    "bugprone-undefined-memory-manipulation": "仅对平凡可拷贝类型使用 memset/memcpy，改用赋值或构造",
    "bugprone-use-after-move": "std::move 后不得再读取该对象；调整取值顺序或改用拷贝",
    "bugprone-not-null-terminated-result": "改用 std::string 或显式补 '\\0'，避免越界读取",
    "bugprone-sizeof-expression": "检查 sizeof 的运算对象，区分 sizeof(指针) 与 sizeof(数组/元素)",
    "bugprone-unused-raii": "RAII 临时对象被立即销毁；为对象命名或改用显式作用域（锁对象尤其危险）",
    "bugprone-exception-escape": "noexcept/析构函数内捕获异常，禁止异常逃逸导致 terminate",
    "bugprone-string-integer-assignment": "确认是把字符还是整数写入 string，把整型改为字符字面量",
    "bugprone-too-small-loop-variable": "循环变量改用 size_t/uint64_t，避免计数溢出死循环",
    "bugprone-integer-division": "确认是有意整除；需要浮点结果时显式转换其中一个操作数",
    "bugprone-signed-char-misuse": "涉及大于 127 的字节值时改用 unsigned char 或显式 cast",
    "bugprone-misplaced-widening-cast": "把 cast 移到运算之后，避免先截断再提升",
    "bugprone-incorrect-roundings": "用 std::lround/统一舍入规则替换 +0.5 强转",
    "bugprone-suspicious-string-compare": "字符串比较改用 strcmp/std::string 比较，确认返回值的用法",
    "bugprone-suspicious-enum-usage": "核对枚举运算与隐式转换，显式转换并确认语义",
    "bugprone-unused-return-value": "接收并使用返回值，或显式 (void) 标注有意忽略",
    "bugprone-unhandled-self-assignment": "赋值运算符内先判断自赋值，或使用 copy-and-swap",
    "bugprone-string-constructor": "核对 string 构造的参数含义（长度/字符/指针），修正误用",
    "bugprone-undelegated-constructor": "在委托构造函数中调用目标构造函数，避免重复初始化遗漏",
    "clang-analyzer-optin.cplusplus.VirtualCall": "构造/析构中不要调用虚函数；改为调用非虚辅助函数或移到 init()",
    "clang-analyzer-deadcode.DeadStores": "删除死赋值或修正分支逻辑，确认不是漏用计算结果",
    "performance-noexcept-move-constructor": "为移动构造/赋值补 noexcept，避免容器退化为拷贝",
    "performance-move-const-arg": "对 const 对象使用 std::move 无效；去掉 const 或删除 move",
    "performance-for-range-copy": "循环内按 const 引用遍历（大对象/非平凡拷贝类型）",
    "performance-faster-string-find": "单字符查找改用 string::find(char) 或 std::string_view",
    "performance-inefficient-string-concatenation": "用 reserve/+=/append 替换连续的 operator+ 拼接",
    "performance-inefficient-vector-operation": "循环前 reserve，避免反复扩容",
    "performance-type-promotion-in-math-fn": "改用对应类型的数学重载（如 std::fabsf），避免隐式提升与截断",
    "performance-trivially-destructible": "去掉不必要的空析构，避免破坏平凡可析构与优化",
}

SEV_LABEL = {
    "P0": "P0 内存安全 / 未定义行为 / 崩溃",
    "P1": "P1 逻辑 / 正确性",
    "P2": "P2 性能（非功能性）",
}


# In-repo third-party / vendored headers: not ours to fix. Governed by
# header-filter exclusion, NOLINT-with-reason, or a dependency upgrade.
VENDORED_PREFIXES = (
    "thirdparty/",
)


def ownership(rel_path: str) -> str:
    if rel_path.startswith(("/usr/", "/opt/", "/usr/local/")):
        return "system"
    if rel_path.startswith(VENDORED_PREFIXES):
        return "vendored"
    return "project"


def normalize(project_root: Path, raw_path: str) -> str:
    try:
        return str(Path(raw_path).resolve().relative_to(project_root))
    except ValueError:
        return raw_path


def collect(logs_dir: Path) -> dict[tuple[str, str, str, str], str]:
    unique: dict[tuple[str, str, str, str], str] = {}
    for log_file in sorted(glob.glob(str(logs_dir / "*.log"))):
        if log_file.endswith((".out.log", ".err.log")):
            continue
        text = Path(log_file).read_text(encoding="utf-8", errors="replace")
        for m in WARNING_RE.finditer(text):
            key = (m.group("file"), m.group("line"), m.group("col"), m.group("check"))
            unique.setdefault(key, m.group("msg"))
    return unique


def fixit_checks(fixes_dir: Path) -> set[str]:
    """Check names that carry clang-tidy FixIts (candidates for --fix)."""
    names: set[str] = set()
    for path in sorted(glob.glob(str(fixes_dir / "*.yaml"))):
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        for block in text.split("- DiagnosticName:"):
            if "Replacements:" not in block:
                continue
            # `Replacements: []` means the diagnostic has no FixIt.
            if re.search(r"Replacements:\s*\[\s*\]", block):
                continue
            name = block.split("\n", 1)[0].strip()
            if name:
                names.add(name)
    return names


def generate(args):
    result_dir = Path(args.result_dir).resolve()
    project_root = Path(args.project_root).resolve()
    unique = collect(result_dir / "logs")
    fixable = fixit_checks(result_dir / "fixes")

    rows = []
    for (file_path, line, col, check), msg in unique.items():
        rel = normalize(project_root, file_path)
        rows.append(
            {
                "severity": SEVERITY.get(check, "P3"),
                "ownership": ownership(rel),
                "check": check,
                "file": rel,
                "line": line,
                "column": col,
                "message": msg,
                "fixit": "yes" if check in fixable else "no",
                "remediation": ACTION.get(check, "人工复核该告警"),
            }
        )
    rows.sort(
        key=lambda r: (r["ownership"] != "project", r["severity"], r["file"], int(r["line"]), int(r["column"]), r["check"])
    )

    with open(args.output_csv, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "severity",
                "ownership",
                "check",
                "file",
                "line",
                "column",
                "message",
                "fixit",
                "remediation",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    owned = [r for r in rows if r["ownership"] == "project"]
    foreign = [r for r in rows if r["ownership"] != "project"]
    by_sev = collections.Counter(r["severity"] for r in owned)
    by_check = collections.Counter((r["severity"], r["check"]) for r in owned)
    by_file = collections.Counter(r["file"] for r in owned)
    by_sev_file = collections.Counter((r["severity"], r["file"]) for r in owned)

    out = []
    out.append("# clang-tidy 基线 — 严重度分级与整改清单")
    out.append("")
    out.append(f"- 数据来源: `{result_dir}`")
    out.append(f"- 唯一缺陷站点(按 `文件:行:列 + 检查规则` 去重): **{len(rows)}**")
    out.append(
        f"- 其中自有代码 **{len(owned)}**；第三方/vendored "
        f"{sum(1 for r in foreign if r['ownership'] == 'vendored')}；系统头 "
        f"{sum(1 for r in foreign if r['ownership'] == 'system')}"
    )
    out.append(f"- 明细清单: `{Path(args.output_csv).name}`")
    out.append("- 整改数量只统计**自有代码**；第三方与系统头单列治理方式。")
    out.append("")
    out.append("## 严重度定义")
    out.append("")
    out.append("| 级别 | 含义 | 数量 | 建议处理窗口 |")
    out.append("| --- | --- | --- | --- |")
    out.append(f"| P0 | {SEV_LABEL['P0']} | {by_sev.get('P0',0)} | 立即，随下次发布前闭环 |")
    out.append(f"| P1 | {SEV_LABEL['P1']} | {by_sev.get('P1',0)} | 本迭代内，按模块批量处理 |")
    out.append(f"| P2 | {SEV_LABEL['P2']} | {by_sev.get('P2',0)} | 排期优化，可批量自动修复 |")
    out.append("")

    for sev in ("P0", "P1", "P2"):
        group = [r for r in owned if r["severity"] == sev]
        if not group:
            continue
        out.append(f"## {SEV_LABEL[sev]}（{len(group)} 条）")
        out.append("")
        out.append("| 检查规则 | 数量 | 可 --fix | 整改动作 |")
        out.append("| --- | --- | --- | --- |")
        for (s, check), count in sorted(by_check.items(), key=lambda kv: (-kv[1], kv[0][1])):
            if s != sev:
                continue
            out.append(
                f"| `{check}` | {count} | {'是' if check in fixable else '否'} | {ACTION.get(check, '人工复核')} |"
            )
        out.append("")
        out.append("涉及文件:")
        out.append("")
        for (s, file_path), count in sorted(
            ((k, v) for k, v in by_sev_file.items() if k[0] == sev), key=lambda kv: (-kv[1], kv[0][1])
        ):
            out.append(f"- `{file_path}` — {count}")
        out.append("")

    out.append("## 非自有代码（不计入整改数）")
    out.append("")
    if foreign:
        foreign_counts = collections.Counter(
            (r["ownership"], r["check"], r["file"]) for r in foreign
        )
        out.append("| 归属 | 检查规则 | 文件 | 数量 |")
        out.append("| --- | --- | --- | --- |")
        for (owner, check, file_path), count in sorted(foreign_counts.items()):
            out.append(f"| {owner} | `{check}` | `{file_path}` | {count} |")
        out.append("")
        out.append(
            "治理方式: 用 `--header-filter` 排除,或在 vendored 头内加带原因的 "
            "`// NOLINT(check-name)`；能升级依赖的优先升级。"
        )
    else:
        out.append("无。")
    out.append("")
    out.append("## 建议推进批次")
    out.append("")
    out.append("1. **批次 A（P0）** 先按检查规则聚合处理,`insecureAPI.strcpy` 单规则占 P0 一半,可一次收敛。")
    out.append("2. **批次 B（P1 逻辑）** 按模块(`src/common`、`src/detector`、`src/tos`)分派,先 `DeadStores` 与 `integer-division`。")
    out.append("3. **批次 C（P2 性能）** 用 `clang-tidy --fix` 批量处理 `可 --fix=是` 的规则,其余人工确认后关闭或 NOLINT。")
    out.append("")
    out.append("## 验收方式")
    out.append("")
    out.append("```bash")
    out.append("# 修复后重跑,与新基线对比唯一缺陷数")
    out.append("CLANG_TIDY_RESULT_DIR=$PWD/work/<下一版本>/clang_tidy/result \\")
    out.append("  bash clang_tidy/scan.sh --jobs 8")
    out.append("```")
    out.append("")
    out.append("- 目标: P0 归零,P1/P2 收敛到经评审的 NOLINT 白名单。")
    out.append("- 每条 NOLINT 必须写明原因,避免无理由抑制重新积累。")
    out.append("")
    out.append("## 各文件缺陷数 Top 20（全等级）")
    out.append("")
    out.append("| 文件 | 数量 | P0 | P1 | P2 |")
    out.append("| --- | --- | --- | --- | --- |")
    for file_path, count in by_file.most_common(20):
        p0 = by_sev_file.get(("P0", file_path), 0)
        p1 = by_sev_file.get(("P1", file_path), 0)
        p2 = by_sev_file.get(("P2", file_path), 0)
        out.append(f"| `{file_path}` | {count} | {p0} | {p1} | {p2} |")
    out.append("")

    Path(args.output_md).write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"rows={len(rows)} P0={by_sev.get('P0',0)} P1={by_sev.get('P1',0)} P2={by_sev.get('P2',0)}")
    print(f"fixit-capable checks: {sorted(fixable)}")
    return 0


if __name__ == "__main__":
    raise SystemExit("Internal report helper; use scan/scan_all.sh or a tool scan.sh.")
