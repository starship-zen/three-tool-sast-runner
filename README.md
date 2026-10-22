# three-tool-sast-runner

[ English ](README.md) • [ 简体中文 ](README.zh-CN.md)

Automated C/C++ static analysis runner bundling **Cppcheck**, **Clang-Tidy**, and **Clang Static Analyzer (CSA)** to produce standardized, auditable evidence packages for industrial control software and IEC 62443 compliance.

> **Current status:** Scanner implementation; end-to-end acceptance incomplete.
> The runner emits `scanner-evidence-1` with `downstream_compatible=false` and
> `release_ready=false`. The `three-tool-input-1.0` exporter and business-product
> isolation remain deferred. See [remaining acceptance work](scan/IMPLEMENTATION_STATUS.md).

---

## 1. Overview

`three-tool-sast-runner` defines a turnkey on-host scanner automation suite designed to:
1. Discover target Translation Units (TUs) from CMake `compile_commands.json`.
2. Run **Cppcheck (2.22+)**, **Clang-Tidy (LLVM 14+)**, and **Clang Static Analyzer (LLVM 14+)** on the exact same commit and compile database.
3. Format outputs into a strictly layered, immutable evidence bundle (`[RAW]` diagnostics, `[RECEIPT]` execution receipts, `[CACHE]` states, and `[DERIVED]` summaries).
4. Target the `three-tool-input-1.0` contract for downstream offline report generation (CWE Top 25:2025, OWASP Top 10:2025, and Critical/High/Medium/Low risk ratings). The current internal bundle is not yet a valid input to that consumer.

For the full architectural specification and interface contract, see [docs/SPEC.md](docs/SPEC.md) (English) or [docs/SPEC.zh-CN.md](docs/SPEC.zh-CN.md) (中文).

---

## 2. Directory Structure

```text
three-tool-sast-runner/
├── README.md / README.zh-CN.md
├── docs/SPEC.md / SPEC.zh-CN.md
├── analy.config.example    # JSON template; copy to scan/analy.config
├── requirements.txt        # Python dependency for native YAML validation
├── install/
│   ├── install_all.sh      # Tool installers; may require network/package access
│   ├── preflight.sh        # Binary availability check, not release certification
│   └── cpp_check/          # Bundled source archive and installer
├── scan/
│   ├── scan_all.sh         # Master runner
│   ├── verify.sh           # Real-analyzer smoke test on a temporary fixture
│   ├── serve.sh            # Local report browsing
│   ├── IMPLEMENTATION_STATUS.md
│   ├── cpp_check/scan.sh
│   ├── clang_tidy/scan.sh
│   └── clang_static_analyzer/scan.sh
└── tests/
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

## 5. Quickstart

Run from the repository root on the intended Ubuntu 24.04 host. Use Python 3.10+
and provision Cppcheck 2.22+ and LLVM 14+ separately. The bundled Cppcheck source
is a development snapshot, not the qualified version required by the evidence gate.
Minimal Ubuntu images also need `python3-venv` for the virtual environment and
`xz-utils` for the bundled source archive; source builds need their own toolchain.

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
bash install/preflight.sh
cp analy.config.example scan/analy.config
```

Edit `scan/analy.config` as JSON using only `scan_paths` (nonempty path array)
and `result_path` (output directory). Replace the template's placeholder paths.
Relative paths are resolved against `scan/`, not the shell's working directory.
Shell assignments and the old `SCAN_CODE_DIR` / `BUILD_DIR` keys are not supported.

```bash
bash scan/scan_all.sh
# Equivalent CLI input; these paths are relative to the current directory:
bash scan/scan_all.sh --scan-path /path/to/product/src --result-path /path/to/results
# Temporary defect fixture only; it does not scan the supplied business source:
bash scan/verify.sh --scan-path /path/to/product/src --result-path /path/to/smoke-results
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
```

For each scan path, the runner searches its ancestors for `compile_commands.json`,
`build/compile_commands.json` or `build/Debug/compile_commands.json`. Use a common
compile database for the three-tool evidence run. Within one database, conflicting
contexts for one source are rejected; select one approved build configuration first.
Multiple paths do not establish business-product isolation.

`install/install_all.sh` may use apt or download packages; it is not a complete
offline installer. Prepare analyzers, Python dependencies and build resources
before disconnected use. Exit 0 is execution success, not release approval;
exit 1 is an execution/report failure, 2 an input error, 3 an evidence-gate
failure after successful execution, and 130 cancellation. The synthetic smoke
test has its own fixture result and does not certify a product or source baseline.

## 6. Downstream Ingestion (Target Contract)

The consumer is the standalone `sast-report-pipeline`. The following is its target
interface, not a working handoff from the current runner. Emit the root manifest
specified in [SPEC section 7](docs/SPEC.md#7-downstream-ingestion-contract),
including the v1.2 producer marker, local frozen assets and hash-bound receipts.
Numbered directories alone are not accepted. Zero-warning clang-tidy may omit
YAML only with the explicit validated summary receipt; do not fabricate YAML.
```bash
python3 scripts/three_tool_report.py generate \
  --product <product_id> \
  --manifest "<output_dir>/manifest.json" \
  --output "<report_run_dir>" \
  --mapping-mode strict
```

This produces a candidate, not customer release or IEC 62443 certification.
CLI PDF requires a separately verified renderer; the web workbench has no PDF
capability. Passing runner tests does not validate downstream ingestion or the
reporting consumer's product, mapping and release gates.

---

## 7. License & Compliance
Designed for industrial functional safety and cybersecurity compliance (IEC 62443-4-1 SI-1 & SI-2).
