# three-tool-sast-runner

Automated C/C++ static analysis runner bundling **Cppcheck**, **Clang-Tidy**, and **Clang Static Analyzer (CSA)** to produce standardized, auditable evidence packages for industrial control software and IEC 62443 compliance.

---

## 1. Overview

`three-tool-sast-runner` provides a turnkey on-host scanner automation suite that:
1. Discovers target Translation Units (TUs) from CMake `compile_commands.json`.
2. Runs **Cppcheck (2.22+)**, **Clang-Tidy (LLVM 14+)**, and **Clang Static Analyzer (LLVM 14+)** on the exact same commit and compile database.
3. Formats outputs into a strictly layered, immutable evidence bundle (`[RAW]` diagnostics, `[RECEIPT]` execution receipts, `[CACHE]` states, and `[DERIVED]` summaries).
4. Generates a self-describing `manifest.json` complying with the `three-tool-input-1.0` contract, ready for downstream offline report generation (mapping to CWE Top 25:2025, OWASP Top 10:2025, and Critical/High/Medium/Low audit risk ratings).

For the full architectural specification and interface contract, see [docs/SPEC.md](docs/SPEC.md).

---

## 2. Directory Structure

```text
three-tool-sast-runner/
├── README.md               # Quickstart and overview
├── docs/
│   └── SPEC.md             # Full interface contract & empirical engine rules
├── analy.config.example    # Configuration template
├── scan_all.sh             # Master execution script
├── preflight.sh            # Environment dependency preflight check
├── verify.sh               # End-to-end smoke test on a synthetic project
├── scripts/                # Helper utilities and environment capture
│   ├── analy_config.sh
│   └── collect_acceptance_env.sh
├── cpp_check/              # Cppcheck wrapper and source build
│   ├── scan.sh
│   └── scripts/
├── clang_tidy/             # Clang-Tidy parallel runner
│   ├── scan.sh
│   └── scripts/
└── clang_static_analyzer/  # CSA per-TU runner with native Plist output
    ├── scan.sh
    └── scripts/
```

---

## 3. Quick Start

### 3.1 Preflight Check
Verify host dependencies (Bash, Python 3, CMake, Clang, Clang-Tidy, Cppcheck):
```bash
bash preflight.sh
```

### 3.2 Smoke Test
Run an end-to-end dry run on a temporary synthetic project:
```bash
bash verify.sh
```

### 3.3 Configure & Run Scan
1. Copy `analy.config.example` to `analy.config`:
   ```bash
   cp analy.config.example analy.config
   ```
2. Configure your project paths:
   ```bash
   SCAN_CODE_DIR=/path/to/source/root
   WORK_DIR=/path/to/output/directory
   BUILD_DIR=/path/to/cmake/build   # contains compile_commands.json
   SCAN_SUBDIRS=src,include         # optional, scans all if empty
   ```
3. Run the scan suite:
   ```bash
   bash scan_all.sh
   ```

---

## 4. Output Artifacts Classification

| Layer Marker | Description | Producer | Role in Downstream Report Generator |
| :--- | :--- | :--- | :--- |
| **`[RAW]`** | XML v2, YAML fixes, Plist | Analyzer binary | Consumed directly as ground truth evidence |
| **`[RECEIPT]`** | CLI args, exit codes, TU mappings, summary.json | Runner orchestrator | Proves clean execution, scope, and coverage |
| **`[CACHE]`** | AST caches (`cppcheck-build`) | Analyzer binary | Performance optimization |
| **`[DERIVED]`** | HTML reports, Markdown summaries | Helper scripts | Human developer quick-view only |

---

## 5. Downstream Ingestion

The resulting bundle is consumed directly by the offline reporting pipeline:
```bash
python3 scripts/three_tool_report.py generate \
  --product <product_id> \
  --manifest "<output_dir>/manifest.json" \
  --output "<report_run_dir>" \
  --mapping-mode strict \
  --pdf
```

---

## 6. License & Compliance
Designed for industrial functional safety and cybersecurity compliance (IEC 62443-4-1 SI-1 & SI-2).
