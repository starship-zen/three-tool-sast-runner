# Three-Tool Scanner Suite Evidence & Input Contract (v1.2)

> **Document Type:** Engineering Specification & Interface Contract  
> **Target Audience:** Developers building and maintaining the automated on-host scan runner.  
> **Downstream Consumer:** Offline Three-Tool Report Generator (`three_tool_report.py`).

---

## 1. Background & Purpose

This specification governs the on-host automated static application security testing (SAST) tool suite for C/C++ industrial and control system software.

The runner automates three complementary open-source static analysis engines:
1. **Cppcheck** (v2.22+)
2. **Clang-Tidy** (LLVM 14+)
3. **Clang Static Analyzer (CSA)** (LLVM 14+)

The primary objective is to produce an **immutable, self-contained, auditable evidence bundle** from deployed source code on a compilation host. Downstream reporting tools consume this bundle without network access or AI intervention to verify coverage, fold duplicates, map findings to **CWE Top 25:2025** and **OWASP Top 10:2025**, assign industrial audit risk ratings (Critical, High, Medium, Low), and generate formal IEC 62443 SI-1 / SI-2 compliance reports.

---

## 2. Core Architectural Principles

1. **Strict Single-Product Isolation**
   - Each scan execution targets exactly one product (e.g. `secp` or `tos_hub`). Multi-product mixed archives are forbidden.
   - Each product produces a standalone, self-describing directory bundle or archive (`<product>-scan-bundle-<timestamp>.zip`).
2. **Three-Tool Non-Negotiable Coverage**
   - Cppcheck, Clang-Tidy, and Clang Static Analyzer must execute against the **exact same Git Commit and exact same set of Translation Units (TUs)**. Omitting any tool blocks downstream release certification.
3. **Rigorous Layer Separation (Raw vs Derived)**
   - Analysis engine native outputs (`[RAW]`) must never be confused with runner receipts (`[RECEIPT]`) or post-processed summaries (`[DERIVED]`).
4. **Compile Database as Ground Truth**
   - CMake-exported `compile_commands.json` is the authoritative source for compiler flags, macros, include paths, and target TU discovery.

---

## 3. Four-Tier Artifact Classification

| Layer Marker | Category | Authoritative Producer | Role in Downstream Reporting |
| :--- | :--- | :--- | :--- |
| **`[RAW]`** | **Native Diagnostic Evidence** | Analysis engine binary directly | **Core Legal/Technical Truth**: ingested directly by mapping and risk assessment pipeline. |
| **`[RECEIPT]`** | **Runner Execution Metadata** | Runner / orchestrator scripts | **Audit & Provenance Evidence**: proves clean environment, exact CLI parameters, process exit codes, and 100% TU execution. |
| **`[CACHE]`** | **Internal Analysis Cache** | Analysis engine internal cache | **Performance State**: demonstrates persistent build caching; not evaluated as a diagnostic report. |
| **`[DERIVED]`** | **Derived Summaries & HTML** | Secondary formatters / Python scripts | **Human Quickview**: optional convenience for developers; downstream report generator **does not rely on this as evidence**. |

---

## 4. Standard Directory Layout

```text
<product_name>-scan-bundle/
├── 01-metadata/                                # Module 1: Environment & Source Baseline
│   ├── scan-environment.json                   # [RECEIPT] Machine, OS, compiler, scanner versions
│   ├── scan-environment.md                     # [DERIVED] Human-readable markdown environment table
│   ├── code-state.txt                          # [RECEIPT] Git repo, branch, commit SHA, dirty status
│   ├── compile-db-selected-tus.txt             # [RECEIPT] Exact list of source TUs selected for this scan
│   └── analy.config                            # [RECEIPT] Copy of the configuration file used for this run
├── 02-compile-db/                              # Module 2: Build & Compilation Database
│   └── compile_commands.json                   # [RECEIPT] Complete compile database
├── 03-cppcheck/                                # Module 3: Cppcheck Output
│   ├── result/
│   │   ├── cppcheck-compile-report.xml         # [RAW] XML v2 report (id, severity, msg, location, cwe)
│   │   └── cppcheck-compile-html/              # [DERIVED] Optional HTML report directory
│   ├── cppcheck-artifacts/                     # [RECEIPT] Execution receipts
│   │   ├── command.txt                         # [RECEIPT] CLI parameters executed
│   │   ├── exit-code.txt                       # [RECEIPT] Process exit code (typically 0)
│   │   ├── version.txt                         # [RECEIPT] Real output of `cppcheck --version`
│   │   ├── summary.json                        # [DERIVED] Severity count summary
│   │   └── input-files.csv                     # [RECEIPT] Actual list of files fed to cppcheck
│   └── cppcheck-build/                         # [CACHE] Persistent AST analysis cache directory
├── 04-clang-tidy/                              # Module 4: Clang-Tidy Output
│   └── result/
│       ├── fixes/                              # [RAW] Core: Native YAML diagnostic events per TU
│       │   ├── <target1>.cpp.yaml              # [RAW] Export-fixes YAML (OMITTED by tool if 0 warnings)
│       │   └── <target2>.cpp.yaml              # [RAW]
│       ├── logs/                               # [RECEIPT] Process stdout/stderr logs
│       │   ├── <target1>.cpp.log               # [RECEIPT]
│       │   └── <target2>.cpp.log               # [RECEIPT]
│       ├── clang-tidy-summary.json             # [RECEIPT] Runner-aggregated TU execution status & warning metrics
│       ├── clang-tidy-summary.md               # [DERIVED] Markdown summary
│       └── clang-tidy-report.html              # [DERIVED] HTML report
└── 05-clang-static-analyzer/                   # Module 5: Clang Static Analyzer Output
    └── result/
        ├── csa-native/                         # [RAW] Core: Native Plist Evidence
        │   ├── <target1>.plist                 # [RAW] Native CSA plist (valid empty plist if 0 bugs)
        │   └── <target2>.plist                 # [RAW]
        ├── tu-mapping.csv                      # [RECEIPT] TU reconciliation CSV: slug,file,diagnostics,exit_code
        ├── compile-db/                         # [RECEIPT/DERIVED] Run state & logs
        │   ├── analyzer-status.json            # [RECEIPT] Status JSON (total, analyzed, failed, reports, elapsed_seconds)
        │   ├── analyzer-files.log              # [RECEIPT] List of analyzed files
        │   ├── analyzer.log                    # [RECEIPT] Consolidated execution log
        │   └── index.html                      # [DERIVED] HTML index
        └── analyzer-summary.md                 # [DERIVED] Summary markdown (TUs analyzed, bugs)
```

---

## 5. Tool Contracts & Empirical Engine Rules

### 5.1 Environment Metadata (`01-metadata/`)
- `code-state.txt`: Dynamically generated prior to scanning:
  ```text
  repo: <git_remote_or_root_path>
  branch: <git_branch>
  commit: <full_40_char_git_commit_sha>
  date: <iso_or_local_timestamp>
  --- git status --porcelain ---
  <empty if clean, or list of modified/untracked files>
  ```
- `scan-environment.json`: Versions must be dynamically read via `--version` calls (e.g. `cppcheck --version`, `clang-tidy --version`, `clang --version`). Never hardcode version strings.
- `compile-db-selected-tus.txt`: One TU path per line. Line count $N$ serves as the benchmark count for all downstream reconciliation.

### 5.2 Cppcheck Contract (`03-cppcheck/`)
- **Native Invocation (`[RAW]`):**
  Must run with compilation database: `--project=compile_commands.json --xml --xml-version=2 2> result/cppcheck-compile-report.xml`.
  * **Empirical Engine Rule:** When a Translation Unit or batch has **0 findings, Cppcheck outputs a valid XML v2 structure** containing `<results version="2"><cppcheck .../><errors></errors></results>`. An empty or zero-byte XML file is an invalid execution failure.
- **Cache Persistence (`[CACHE]`):**
  Must specify `--cppcheck-build-dir=cppcheck-build` to enable persistent caching and avoid redundant re-parsing.
- **Audit Receipts (`[RECEIPT]` & `[DERIVED]`):**
  Record exact command line in `command.txt`, process exit code in `exit-code.txt`, binary version in `version.txt`, and file list in `input-files.csv` (`[RECEIPT]`). `summary.json` is a post-processed severity count (`[DERIVED]`).

### 5.3 Clang-Tidy Contract (`04-clang-tidy/`)
- **Native Diagnostics (`[RAW]`):**
  Run per TU with `-export-fixes=fixes/<slug>.yaml`.
  * **Empirical Engine Rule:** When a Translation Unit has **0 diagnostics, Clang-Tidy does NOT generate a YAML file**.
  * Consequently, the number of files in `fixes/` will typically be $\le N$.
- **Runner Receipt & Reconciliation JSON (`[RECEIPT]`):**
  Because zero-warning files omit YAML output, full coverage must be proven via `clang-tidy-summary.json`. The runner script must generate this file with:
  ```json
  {
    "selected_file_count": N,
    "warnings": {
      "raw_total": <int>,
      "unique_total": <int>,
      "by_check": [{"check": "<name>", "raw": <int>, "unique": <int>}]
    },
    "files": [
      {"file": "<path>", "status": "ok", "unique_diagnostics": 0},
      {"file": "<path>", "status": "ok", "unique_diagnostics": 2}
    ]
  }
  ```
  Every TU with `status: "ok"` and `unique_diagnostics: 0` is verified as cleanly analyzed with zero findings.

### 5.4 Clang Static Analyzer Contract (`05-clang-static-analyzer/`)
- **Native Plist Output (`[RAW]`):**
  Invoked per TU via `clang --analyze ... -Xclang -analyzer-output=plist -o csa-native/<slug>.plist`.
  * **Empirical Engine Rule:** When a Translation Unit has **0 bugs, Clang STILL outputs a valid empty Plist file** (containing valid XML headers and `<dict>` structure, with an empty `<array></array>` under `<key>diagnostics</key>`).
  * **Hard Gate:** Exactly $N$ `.plist` files must exist in `csa-native/`, corresponding 1-to-1 to the $N$ selected TUs.
- **TU Reconciliation CSV (`[RECEIPT]`):**
  The runner must generate `tu-mapping.csv` with exactly 4 columns:
  ```csv
  slug,file,diagnostics,exit_code
  0f4533aa883f,src/service/main.cpp,0,0
  95b99c0f081d,src/service/handler.cpp,1,0
  ```
  Row count must strictly equal $N$.

---

## 6. Preflight Validation Checklist (Runner Gate)

Before packaging or publishing a scan drop, the runner's self-test must verify:

| Check Category | Verification Requirement | Failure Impact |
| :--- | :--- | :--- |
| **Product Isolation** | Bundle contains only modules of the designated product | Downstream rejection; cannot certify product |
| **TU Identity Parity** | `compile-db-selected-tus.txt` set == `cppcheck-artifacts/input-files.csv` set == `clang-tidy-summary.json.files` set == `tu-mapping.csv.file` set == CSA analysis targets | Coverage mismatch; reconciliation failure |
| **Raw Evidence Integrity** | `cppcheck-compile-report.xml` is valid XML v2; `.yaml` count matches warning TUs; `.plist` count == TU count | Classified as Unverified |
| **Commit Traceability** | `code-state.txt` commit is a valid 40-character SHA-1 (not empty / not recorded) | Audit traceability failure |

---

## 7. Downstream Ingestion Contract

The bundle generated by this runner is consumed directly by the offline report generator:

```bash
# Generate final IEC 62443 compliance report package (PDF, HTML/MD appendix, CSV, SARIF)
python3 scripts/three_tool_report.py generate \
  --product <secp|tos_hub> \
  --manifest "/path/to/<product>-scan-bundle/manifest.json" \
  --output "workspaces/mm7-safety/sast/runs/partner-<product>-final" \
  --mapping-mode strict \
  --pdf
```
