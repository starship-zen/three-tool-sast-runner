#!/usr/bin/env python3

from __future__ import annotations

import argparse
import html
import json
import re
from collections import Counter, defaultdict
from pathlib import Path


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def parse_log(log_file: Path) -> dict[str, object]:
    text = log_file.read_text(encoding="utf-8", errors="replace")
    checks = Counter()
    warning_count = 0
    compiler_warning_count = 0
    error_count = 0

    for line in text.splitlines():
        if "warning:" in line:
            warning_count += 1
            match = re.search(r"\[([^\]]+)\]\s*$", line)
            if match:
                checks[match.group(1)] += 1
            else:
                compiler_warning_count += 1
        if re.search(r"\berror:", line):
            error_count += 1

    return {
        "warning_count": warning_count,
        "compiler_warning_count": compiler_warning_count,
        "error_count": error_count,
        "checks": checks,
    }


def classify_error(line: str) -> str:
    if "return-type-c-linkage" in line:
        return "C linkage 返回 C++ 类型"
    if "non-pod-varargs" in line or "variadic function" in line:
        return "非平凡 C++ 类型进入可变参数函数"
    if "variable-sized object" in line or "VLA" in line:
        return "变长对象初始化"
    if "runtime_error" in line or "stderr" in line:
        return "缺少声明或头文件"
    return "其他编译诊断"


def build_directory_stats(files: list[dict[str, object]]) -> dict[str, Counter]:
    stats: dict[str, Counter] = defaultdict(Counter)
    for item in files:
        name = str(item["file"])
        directory = name.split("/", 1)[0] if "/" in name else "."
        stat = stats[directory]
        stat["files"] += 1
        stat["ok"] += item["status"] == "ok"
        stat["failed"] += item["status"] != "ok"
        stat["warnings"] += int(item["warning_count"])
        stat["checks"] += int(item["check_diagnostics"])
        stat["errors"] += int(item["error_count"])
    if "." not in stats:
        stats["."] = sum(stats.values(), Counter())
    return stats


def table_row(cells: list[str], tag: str = "td") -> str:
    return "<tr>" + "".join(f"<{tag}>{cell}</{tag}>" for cell in cells) + "</tr>"


def render_html(
    payload: dict[str, object], result_dir: Path, static_analyzer_dir: Path
) -> str:
    files = payload["files"]
    all_checks = Counter()
    error_categories = Counter()
    total_warnings = 0
    total_compiler_warnings = 0
    total_errors = 0

    enriched_files: list[dict[str, object]] = []
    for item in files:
        log_path = result_dir / str(item["log"])
        log_stats = parse_log(log_path) if log_path.is_file() else {
            "warning_count": 0,
            "compiler_warning_count": 0,
            "error_count": 0,
            "checks": Counter(),
        }
        checks = log_stats["checks"]
        all_checks.update(checks)
        total_warnings += int(log_stats["warning_count"])
        total_compiler_warnings += int(log_stats["compiler_warning_count"])
        total_errors += int(log_stats["error_count"])

        if log_path.is_file():
            for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if re.search(r"\berror:", line):
                    error_categories[classify_error(line)] += 1

        enriched = dict(item)
        enriched["warning_count"] = log_stats["warning_count"]
        enriched["check_diagnostics"] = sum(checks.values())
        enriched["error_count"] = log_stats["error_count"]
        enriched_files.append(enriched)

    directory_stats = build_directory_stats(enriched_files)
    status_counts = Counter(str(item["status"]) for item in enriched_files)
    warnings_meta = payload.get("warnings") or {}
    raw_diagnostics = int(warnings_meta.get("raw_total", sum(all_checks.values())))
    unique_diagnostics = int(warnings_meta.get("unique_total", 0))
    static_summary = static_analyzer_dir / "analyzer-summary.md"
    static_text = static_summary.read_text(encoding="utf-8", errors="replace") if static_summary.is_file() else ""
    static_row = re.search(
        r"^\| `[^`]+` \| (ok|issues|partial|failed) \| (\d+) \|", static_text, re.MULTILINE
    )
    static_ran = bool(static_row) and static_row.group(1) in ("ok", "issues")
    static_no_bugs = bool(static_row) and static_row.group(2) == "0"

    scan_dirs = ", ".join(str(item) for item in payload["scan_dirs"])
    scope_text = ", ".join(str(item) for item in payload.get("scan_paths", payload["scan_dirs"]))
    directory_options = "".join(f'<option value="{esc(directory + "/" if directory != "." else ".")}">{esc(directory)}</option>'
                                for directory in payload["scan_dirs"])
    generated_at = payload.get("generated_at", "unknown")
    selected_count = payload.get("selected_file_count", len(enriched_files))
    failed_count = len(enriched_files) - status_counts["ok"]

    directory_rows = []
    for directory in payload["scan_dirs"]:
        stat = directory_stats.get(directory, Counter())
        directory_rows.append(table_row([
            f"<code>{esc(directory)}/</code>",
            esc(stat["files"]),
            esc(stat["ok"]),
            esc(stat["failed"]),
            esc(stat["warnings"]),
            esc(stat["checks"]),
            esc(stat["errors"]),
        ]))

    check_rows = []
    by_check = warnings_meta.get("by_check")
    if by_check:
        for entry in by_check[:30]:
            check_rows.append(table_row([
                f"<code>{esc(entry['check'])}</code>",
                esc(entry["raw"]),
                esc(entry["unique"]),
            ]))
    else:
        for name, count in all_checks.most_common(30):
            check_rows.append(table_row([f"<code>{esc(name)}</code>", esc(count), "-"]))

    error_rows = []
    for name, count in error_categories.most_common():
        error_rows.append(table_row([esc(name), esc(count)]))

    file_rows = []
    for item in enriched_files:
        status = str(item["status"])
        status_class = "ok" if status == "ok" else "failed"
        log_href = str(item["log"])
        fixes_href = str(item["fixes"])
        fixes_path = result_dir / fixes_href
        fixes_link = f'<a href="{esc(fixes_href)}">yaml</a>' if fixes_path.is_file() else "-"
        file_rows.append(table_row([
            f"<code>{esc(item['file'])}</code>",
            f'<span class="status {status_class}">{esc(status)}</span>',
            esc(item["warning_count"]),
            esc(item["check_diagnostics"]),
            esc(item.get("unique_diagnostics", "-")),
            esc(item["error_count"]),
            f'<a href="{esc(log_href)}">log</a>',
            fixes_link,
        ]))

    if not static_summary.is_file():
        static_status = "未运行"
        static_class = "failed"
    elif static_ran and static_no_bugs:
        static_status = "通过"
        static_class = "ok"
    elif static_ran:
        static_status = "需复核"
        static_class = "failed"
    else:
        static_status = "失败"
        static_class = "failed"

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>clang-tidy 全量分析报告</title>
  <style>
    :root {{
      --bg: #f4f6f8;
      --surface: #ffffff;
      --text: #17202a;
      --muted: #65717d;
      --line: #dce2e8;
      --blue: #1769aa;
      --green: #18794e;
      --red: #b42318;
      --amber: #9a6700;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--bg); color: var(--text); font: 14px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    main {{ max-width: 1440px; margin: 0 auto; padding: 32px 24px 56px; }}
    h1 {{ margin: 0 0 8px; font-size: 28px; letter-spacing: 0; }}
    h2 {{ margin: 34px 0 12px; font-size: 20px; letter-spacing: 0; }}
    h3 {{ margin: 24px 0 8px; font-size: 16px; letter-spacing: 0; }}
    p {{ margin: 8px 0 14px; }}
    .subtle {{ color: var(--muted); }}
    .hero {{ background: var(--surface); border: 1px solid var(--line); border-radius: 8px; padding: 24px; }}
    .chips {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 16px; }}
    .chip {{ border: 1px solid var(--line); background: #f8fafc; border-radius: 999px; color: #344454; padding: 4px 10px; font-size: 12px; }}
    .cards {{ display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 12px; margin-top: 18px; }}
    .card {{ background: var(--surface); border: 1px solid var(--line); border-radius: 8px; padding: 16px; min-width: 0; }}
    .card .label {{ color: var(--muted); font-size: 12px; }}
    .card .value {{ font-size: 26px; font-weight: 700; margin-top: 4px; }}
    .card.ok .value {{ color: var(--green); }}
    .card.failed .value {{ color: var(--red); }}
    .card.info .value {{ color: var(--blue); }}
    .section {{ margin-top: 22px; background: var(--surface); border: 1px solid var(--line); border-radius: 8px; padding: 18px; overflow: hidden; }}
    .section > h2 {{ margin-top: 0; }}
    .table-wrap {{ overflow-x: auto; }}
    table {{ border-collapse: collapse; width: 100%; min-width: 680px; }}
    th, td {{ border-bottom: 1px solid var(--line); padding: 9px 10px; text-align: left; vertical-align: top; }}
    th {{ background: #f8fafc; color: #344454; font-weight: 600; white-space: nowrap; }}
    tr:last-child td {{ border-bottom: 0; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; word-break: break-word; }}
    a {{ color: var(--blue); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .status {{ display: inline-block; border-radius: 999px; padding: 2px 8px; font-size: 12px; font-weight: 600; }}
    .status.ok {{ background: #e7f6ee; color: var(--green); }}
    .status.failed {{ background: #fdecea; color: var(--red); }}
    .filters {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }}
    .filters input, .filters select {{ border: 1px solid var(--line); border-radius: 6px; background: #fff; color: var(--text); padding: 8px 10px; font: inherit; }}
    .filters input {{ flex: 1 1 320px; min-width: 220px; }}
    .filters select {{ flex: 0 1 180px; }}
    .note {{ border-left: 4px solid #e0a100; background: #fff9e6; padding: 12px 14px; margin-top: 12px; }}
    footer {{ color: var(--muted); font-size: 12px; margin-top: 20px; }}
    @media (max-width: 980px) {{ .cards {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }} }}
    @media (max-width: 640px) {{ main {{ padding: 18px 12px 40px; }} .cards {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} h1 {{ font-size: 23px; }} }}
  </style>
</head>
<body>
<main>
  <header class="hero">
    <h1>clang-tidy 与 Clang Static Analyzer 全量分析报告</h1>
    <p class="subtle">生成时间：{esc(generated_at)} · 检查集合：<code>{esc(payload['checks'])}</code></p>
    <div class="chips">
      <span class="chip">扫描范围：{esc(scan_dirs)}</span>
      <span class="chip">编译数据库：{esc(payload['compile_db_dir'])}</span>
      <span class="chip">并发数：{esc(payload['jobs'])}</span>
      <span class="chip">工具：{esc(payload.get("tool_version", "unknown"))}</span>
    </div>
  </header>

  <section class="cards">
    <div class="card info"><div class="label">目标翻译单元</div><div class="value">{esc(selected_count)}</div></div>
    <div class="card ok"><div class="label">正常退出</div><div class="value">{esc(status_counts['ok'])}</div></div>
    <div class="card failed"><div class="label">非零退出</div><div class="value">{esc(failed_count)}</div></div>
    <div class="card failed"><div class="label">规则诊断(原始)</div><div class="value">{esc(raw_diagnostics)}</div></div>
    <div class="card info"><div class="label">规则诊断(去重)</div><div class="value">{esc(unique_diagnostics)}</div></div>
    <div class="card info"><div class="label">错误文本</div><div class="value">{esc(total_errors)}</div></div>
  </section>

  <section class="section">
    <h2>结论</h2>
    <p>本次请求扫描路径：<code>{esc(scope_text)}</code>。共选出 {esc(selected_count)} 个翻译单元；执行状态、诊断和日志以逐文件记录为准。</p>
    <p>规则诊断(原始)包含公共头文件随翻译单元展开产生的重复提示，<strong>规则诊断(去重)</strong> 按 <code>文件:行:列 + 检查规则</code> 去重后才是独立缺陷数量，应以此作为复核基线。非零退出需要结合日志中的编译器 error 与工具链版本进行复核。</p>
    <div class="note"><strong>Clang Static Analyzer：{static_status}</strong>。扫描结果来自同轮工具目录；若未运行，请从仓库根目录执行 <code>bash scan/clang_static_analyzer/scan.sh --scan-path 扫描路径 --result-path 结果路径</code>。</div>
  </section>

  <section class="section">
    <h2>目录统计</h2>
    <div class="table-wrap"><table><thead>{table_row(['目录', '目标数', '成功', '非零退出', 'warning 文本', '规则诊断', 'error 文本'], 'th')}</thead><tbody>{''.join(directory_rows)}</tbody></table></div>
  </section>

  <section class="section">
    <h2>高频检查规则</h2>
    <div class="table-wrap"><table><thead>{table_row(['检查规则', '原始', '去重后唯一'], 'th')}</thead><tbody>{''.join(check_rows)}</tbody></table></div>
  </section>

  <section class="section">
    <h2>错误类型</h2>
    <div class="table-wrap"><table><thead>{table_row(['类型', '次数'], 'th')}</thead><tbody>{''.join(error_rows) or table_row(['未提取到 error 文本', '0'])}</tbody></table></div>
  </section>

  <section class="section">
    <h2>文件明细</h2>
    <div class="filters">
      <input id="fileSearch" type="search" placeholder="搜索文件路径或状态">
      <select id="statusFilter"><option value="">全部状态</option><option value="ok">ok</option><option value="failed">failed</option></select>
      <select id="directoryFilter"><option value="">全部目录</option>{directory_options}</select>
    </div>
    <div class="table-wrap"><table id="fileTable"><thead>{table_row(['文件', '状态', 'warning 文本', '规则诊断', '唯一诊断(本文件)', 'error 文本', '日志', 'fixes'], 'th')}</thead><tbody>{''.join(file_rows)}</tbody></table></div>
  </section>

  <footer>本页面由 <code>scan/clang_tidy/dashboard.py</code> 根据本轮汇总和单文件日志生成。</footer>
</main>
<script>
  const search = document.getElementById('fileSearch');
  const status = document.getElementById('statusFilter');
  const directory = document.getElementById('directoryFilter');
  const rows = Array.from(document.querySelectorAll('#fileTable tbody tr'));
  function updateRows() {{
    const query = search.value.toLowerCase().trim();
    const statusValue = status.value;
    const directoryValue = directory.value;
    rows.forEach(row => {{
      const text = row.textContent.toLowerCase();
      const filename = row.cells[0].textContent.trim();
      const rowDirectory = filename.includes('/') ? filename.split('/')[0] + '/' : '.';
      const matchesQuery = !query || text.includes(query);
      const matchesStatus = !statusValue || row.cells[1].textContent.trim() === statusValue;
      const matchesDirectory = !directoryValue || rowDirectory === directoryValue;
      row.hidden = !(matchesQuery && matchesStatus && matchesDirectory);
    }});
  }}
  [search, status, directory].forEach(control => control.addEventListener('input', updateRows));
</script>
</body>
</html>
"""



if __name__ == "__main__":
    raise SystemExit("Internal report helper; use scan/scan_all.sh or a tool scan.sh.")
