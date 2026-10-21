# three-tool-sast-runner

[ English ](README.md) • [ 简体中文 ](README.zh-CN.md)

Automated C/C++ static analysis runner bundling **Cppcheck**, **Clang-Tidy**, and **Clang Static Analyzer (CSA)** to produce standardized, auditable evidence packages for industrial control software and IEC 62443 compliance.

> **Current Repository Status:** `v1.0 Specification & Interface Contract Baseline`  
> This repository currently defines the authoritative interface contract, evidence schema, and directory layout. The runner scripts and CLI tooling (`scan_all.sh`, `preflight.sh`, `verify.sh`) are actively being implemented and integrated by the engineering team.

---

## 1. Overview

`three-tool-sast-runner` defines a turnkey on-host scanner automation suite designed to:
1. Discover target Translation Units (TUs) from CMake `compile_commands.json`.
2. Run **Cppcheck (2.22+)**, **Clang-Tidy (LLVM 14+)**, and **Clang Static Analyzer (LLVM 14+)** on the exact same commit and compile database.
3. Format outputs into a strictly layered, immutable evidence bundle (`[RAW]` diagnostics, `[RECEIPT]` execution receipts, `[CACHE]` states, and `[DERIVED]` summaries).
4. Generate a self-describing bundle complying with the `three-tool-input-1.0` contract, ready for downstream offline report generation (mapping to CWE Top 25:2025, OWASP Top 10:2025, and Critical/High/Medium/Low audit risk ratings).

For the full architectural specification and interface contract, see [docs/SPEC.md](docs/SPEC.md) (English) or [docs/SPEC.zh-CN.md](docs/SPEC.zh-CN.md) (中文).

---

## 2. Target Directory Structure

```text
three-tool-sast-runner/
├── README.md               # Quickstart and project status
├── docs/
│   └── SPEC.md             # Full interface contract & empirical engine rules
├── analy.config.example    # Configuration template
├── scan_all.sh             # [Target Script] Master execution runner
├── preflight.sh            # [Target Script] Host dependency preflight check
├── verify.sh               # [Target Script] End-to-end smoke test on a synthetic project
├── scripts/                # [Target Scripts] Helper utilities and environment capture
│   ├── analy_config.sh
│   └── collect_acceptance_env.sh
├── cpp_check/              # [Target Module] Cppcheck wrapper and source build
│   ├── scan.sh
│   └── scripts/
├── clang_tidy/             # [Target Module] Clang-Tidy parallel runner
│   ├── scan.sh
│   └── scripts/
└── clang_static_analyzer/  # [Target Module] CSA per-TU runner with native Plist output
    ├── scan.sh
    └── scripts/
```

---

## 3. Four-Tier Artifact Classification

| Layer Marker | Description | Producer | Role in Downstream Report Generator |
| :--- | :--- | :--- | :--- |
| **`[RAW]`** | XML v2, YAML fixes, Plist | Analyzer binary directly | Consumed directly as ground truth evidence |
| **`[RECEIPT]`** | CLI args, exit codes, TU mappings, execution status JSON | Runner orchestrator | Proves clean execution, scope, and coverage |
| **`[CACHE]`** | AST caches (`cppcheck-build/`) | Analyzer binary | Performance optimization; not evaluated as reports |
| **`[DERIVED]`** | HTML reports, Markdown summaries, severity distribution summaries | Helper scripts | Human developer quick-view only |

---

## 4. Key Engine Behaviors & Zero-Result Contracts

1. **Cppcheck:** 
   * When 0 findings are detected, native execution produces a valid XML v2 structure containing `<results version="2"><cppcheck .../><errors></errors></results>`.
2. **Clang-Tidy:**
   * When 0 diagnostics are detected, Clang-Tidy **does not write any YAML file**.
   * Completeness is proven via `clang-tidy-summary.json`'s `files` array (`status: ok`, `unique_diagnostics: 0`), not by matching YAML file count to TU count.
3. **Clang Static Analyzer:**
   * When 0 bugs are detected, Clang **still outputs a valid empty Plist file** (with `<key>diagnostics</key><array></array>`).
   * Exactly $N$ Plist files must exist for the $N$ selected TUs, mapped via `tu-mapping.csv`.

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
