from __future__ import annotations

import re

import json

from pathlib import Path

from collections import Counter, defaultdict

WARNING_RE = re.compile(
    r"^(?P<file>[^\n]+?):(?P<line>\d+):(?P<column>\d+):\s+warning:\s+"
    r"(?P<message>.*?)(?:\n|\s)+\[(?P<check>[^\]]+)\]",
    re.MULTILINE | re.DOTALL,
)

def collect_diagnostics(log_dir: Path) -> dict[str, object]:

    """Aggregate warning lines and deduplicate repeated header diagnostics.



    A header is analyzed once per translation unit that includes it, so a single

    header-level warning is written to every matching log. Deduplicating on

    (file, line, column, check) turns the multiplied raw count back into the

    number of distinct defect sites, which is the real review baseline.

    """

    raw_total = 0

    by_check_raw: Counter[str] = Counter()

    by_check_unique: dict[str, set[tuple[str, str, str]]] = defaultdict(set)

    unique_sites: set[tuple[str, str, str, str]] = set()

    unique_by_file: dict[str, set[tuple[str, str, str, str]]] = defaultdict(set)



    for log_file in sorted(path for path in log_dir.glob("*.log")
                           if not path.name.endswith((".out.log", ".err.log"))):

        text = log_file.read_text(encoding="utf-8", errors="replace")
        for match in WARNING_RE.finditer(text):

            if not match:

                continue

            file_path = match.group("file")

            site = (file_path, match.group("line"), match.group("column"))

            check = match.group("check")

            raw_total += 1

            by_check_raw[check] += 1

            by_check_unique[check].add(site)

            unique_sites.add(site + (check,))

            unique_by_file[file_path].add(site + (check,))



    return {

        "raw_total": raw_total,

        "unique_total": len(unique_sites),

        "by_check": [

            {"check": check, "raw": raw, "unique": len(by_check_unique[check])}

            for check, raw in by_check_raw.most_common()

        ],

        "unique_by_file": {

            path: len(sites) for path, sites in sorted(unique_by_file.items())

        },

    }

def write_summary(summary_md: Path, summary_json: Path, payload: dict[str, object]) -> None:

    summary_json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")



    lines = [

        "# Clang-Tidy Summary",

        "",

        f"- Compiler: `{payload['clang_tidy_bin']}`",

        f"- Compile database: `{payload['compile_db_dir']}`",

        f"- Checks: `{payload['checks']}`",

        f"- Header filter: `{payload['header_filter']}`",

        f"- Scan directories: `{', '.join(payload['scan_dirs'])}`",

        f"- Selected files: `{payload['selected_file_count']}`",

        f"- Jobs: `{payload['jobs']}`",

        "",

    ]



    warnings = payload.get("warnings") or {}

    if warnings:

        raw_total = int(warnings.get("raw_total", 0))

        unique_total = int(warnings.get("unique_total", 0))

        ratio = raw_total / unique_total if unique_total else 0.0

        lines += [

            "## Diagnostics",

            "",

            f"- Raw warning lines: `{raw_total}`",

            f"- Unique defect sites (file:line:column + check): `{unique_total}`",

            f"- Duplication factor: `{ratio:.1f}x`",

            "",

            "Header warnings repeat once per translation unit that includes the",

            "header, so `raw` overstates the backlog. Treat `unique` as the review",

            "baseline and the `Raw` column as a per-check duplication signal.",

            "",

            "| Check | Raw | Unique |",

            "| --- | --- | --- |",

        ]

        for warning in warnings.get("by_check", [])[:30]:

            lines.append(

                f"| `{warning['check']}` | {warning['raw']} | {warning['unique']} |"

            )

        lines.append("")



    lines += [

        "## Files",

        "",

        "| File | Status | Unique (this file) | Log | Fixes |",

        "| --- | --- | --- | --- | --- |",

    ]



    for item in payload["files"]:

        file_path = item["file"]

        status = item["status"]

        log_rel = item["log"]

        fixes_rel = item["fixes"]

        unique_rel = item.get("unique_diagnostics", 0)

        lines.append(

            f"| `{file_path}` | {status} | {unique_rel} | `{log_rel}` | `{fixes_rel}` |"

        )



    summary_md.write_text("\n".join(lines) + "\n", encoding="utf-8")

if __name__ == "__main__":
    raise SystemExit("Internal report helper; use scan/scan_all.sh or a tool scan.sh.")
