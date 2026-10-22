# Three-Tool Scanner Suite Evidence & Input Contract (v1.2)

[ English ](SPEC.md) • [ 简体中文 ](SPEC.zh-CN.md)

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
  For the v1.2 reporting handoff, `warnings.raw_total` and `by_check[].raw`
  count the submitted native YAML diagnostic records, not CLI banner totals.
  Keep any differing log statistics separately; do not substitute them for the
  native record counts.

### 5.4 Clang Static Analyzer Contract (`05-clang-static-analyzer/`)
- **Native Plist Output (`[RAW]`):**
  Invoke once per TU via `clang --analyze ... -Xanalyzer -analyzer-output=plist-html -o reports/<slug>/<slug>.plist`.
  LLVM writes multi-file native plist and HTML from the same analysis. Each TU
  has its own directory; the execution receipt's `artifact` is authoritative
  for export, rather than a guessed flat path. Record one real command receipt,
  not a fabricated second `html_execution`. No second HTML analysis is run.
  A valid zero-diagnostic plist needs no HTML; positive diagnostics require
  nonempty regular HTML reports. Nonzero exit, invalid plist or missing positive
  HTML fails completion. Do not skip HTML based on an ordinary single-file
  plist: that format can omit cross-file paths supported by `plist-html`.
  Native finding totals may therefore increase for cross-file defects; compare
  diagnostic records, not repeated warning lines from combined consumers.
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

The consumer is the standalone `sast-report-pipeline` repository, not the
historical partner-drop adapter. Its canonical `three-tool-input-1.0` manifest
is not the runner's product-delivery format. The producer emits
`three-tool-scan-delivery-1.0`; the receiving consumer validates that delivery,
binds its locally approved standards/mappings and prepares canonical report
input. A numbered directory alone is not valid input to either boundary.

```bash
# Generate a candidate report package; this is not customer release approval.
python3 scripts/three_tool_report.py generate \
  --product secp \
  --manifest "/path/to/<product>-scan-bundle/manifest.json" \
  --output "/path/to/new-product-report" \
  --mapping-mode strict
```

PDF is optional CLI output requiring a separately provisioned and verified
renderer toolchain. The local web workbench does not currently generate PDF.

### 7.1 Consumer root manifest and zero-diagnostic contract (2026-10-21)

The consumer's root `manifest.json` is mandatory. It uses
`schema_version: "three-tool-input-1.0"` and
`producer_contract: "three-tool-sast-runner-1.2"`. Required sections are
`run_id`, `project` (including `product_id`), `source`, `tool_runs`, `artifacts`,
`assets`, and `provenance_inputs`. All referenced paths are relative to this
root and SHA-256-bound. The receiving consumer owns mapping and report policy;
the producer does not embed taxonomy assets in its delivery. The runnable
synthetic canonical fixture is maintained by `sast-report-pipeline`; it is not
an executed scan.

- Put only real native XML/YAML/plist in `artifacts`. Bind each to its actual
  tool/version/component/configuration/target and source analysis unit when
  known. Never submit cache or derived HTML as native diagnostics.
- Populate `source.repository`, `source.commit`, source scope and
  `source.build_configuration_sha256` from the actual recorded inputs. Preserve
  the corresponding configuration bytes with `purpose: "build-configuration"`.
  If the compile database is intentionally the build-configuration identity,
  bind that exact file and hash explicitly; do not invent a separate hash.
- Each `tool_runs` item records the actual version, command and configuration
  hash, plus its targets' actual start/end, attempted/completed status, exit
  status, outcome and selected/completed analysis units. Preserve per-tool
  configuration bytes as `purpose: "rules-configuration"` with matching identity.
  The three declared TU sets must reconcile. Missing facts remain JSON null,
  with an incomplete candidate/release gate rather than guessed success.
  A completed target must explicitly supply integer `units_attempted` and
  `units_completed` equal to its selected TU count; missing counts are not
  successful completion evidence.
- Bind frozen standards and rule mappings in `assets`. Full CWE classification,
  CWE Top 25:2025 and OWASP Top 10:2025 are distinct views; runtime needs no AI
  or online taxonomy. Unknown rule mappings must not become automatic N/A.
- Preserve source/environment/selection/execution receipts separately as
  hash-bound `provenance_inputs`; these are not native diagnostic records.
  New common-run target IDs must be actual target identities, not historical
  `new-delivery` / `prior-delivery` collection labels.

For a clang-tidy target containing zero-warning units, the explicit supporting
entry is:

```json
{
  "purpose": "supporting-evidence",
  "evidence_type": "clang-tidy-zero-diagnostic-receipt",
  "path": "04-clang-tidy/result/clang-tidy-summary.json",
  "sha256": "<actual SHA-256 of these summary bytes>",
  "tool": "clang-tidy",
  "tool_version": "<actual version, or null if genuinely unknown>",
  "component": "<matching component>",
  "configuration": "<matching configuration, or null>",
  "target": "<matching target>"
}
```

This is a template, not a complete executable manifest. The referenced summary
uses section 5.3's fields. Its unique file identities and selected count must
match the declared target units; successful zero rows must not contradict native
diagnostics, while positive rows still need attributable YAML. Only a fully-zero
target may omit all native YAML. Do not fabricate an empty YAML or a diagnostic
record. Cppcheck and CSA still require their real native zero-output files.
Summary validation proves consistency of supplied receipts, not authenticated
execution, immutable source, vulnerability confirmation or release approval.

### 7.2 Producer product-scan delivery (2026-10-22)

`scan/handoff.py export --profile PROFILE --run RUN --output NEW_DIRECTORY`
exports one completed, evidence-gated compile-database group as a standalone
`three-tool-scan-delivery-1.0`. Its exact operator profile is
`three-tool-product-profile-1.0`: product ID/display name, repository label,
explicit component/target/configuration, repository-relative included and
excluded paths, and supported/unsupported languages. It contains no reporter
checkout locator, standards asset, rule mapping or release policy.

The delivery root has `manifest.json` and `delivery-receipt.json`. The manifest
preserves canonical-shaped project/source/component/tool-run/artifact and
provenance fields, but is deliberately not `three-tool-input-1.0`. It binds
each native artifact and copied provenance file by relative path, size and
SHA-256; preserves actual numeric tool release tokens alongside full captured
version banners and binary hashes; and records selected-unit, artifact and
scanner receipt counts. The receipt binds the manifest and every other payload
file. Native scanner bytes are copied unchanged. Missing clang-tidy YAML is
represented by a typed reference to the original per-file zero-result runner
receipt; the receiver parses native YAML and derives its own v1.2 native-count
receipt. No empty YAML or diagnostic is synthesized.

This first lane supports exactly one group, component, build target and
configuration. All selected units must match the profile's explicit path and
language scope. It rejects multiple groups and failed/incomplete gates; it does
not infer business product/component ownership from the compilation database.
Selected path strings are preserved as opaque analysis-unit IDs for exact
clang-tidy `MainSourceFile` matching, while repository-relative scope is bound
separately to the captured Git root. Raw provenance may disclose host paths and
native outputs can contain source excerpts; operators must apply their data
handling policy before transferring a delivery. The delivery remains
untrusted scanner evidence, not authenticated execution or release approval.

## 8. Implemented Runner Safety Contracts (2026-10-22)

### 8.1 Scope

These operational contracts cover installer cleanup, compile-context selection,
derived HTML, product-scan delivery and documented configuration. They do not
implement the consumer's canonical mapping/report adapter, independently
certify business ownership or upgrade analyzer versions.

### 8.2 Entry Points

Use `uv run --frozen --offline bash scan/scan_all.sh [--scan-path PATH ...] [--result-path PATH]`
after provisioning the locked environment.
Without CLI values, read JSON from `scan/analy.config`; the root template is
copied there. `bash install/preflight.sh` checks binary availability only.

### 8.3 Input and Ownership Contracts

Configuration accepts only `scan_paths` and `result_path`. Configuration-relative
paths use the config directory; CLI-relative paths use the current directory.
Within one database, identical resolved directories and argument vectors for a source may
deduplicate; differing contexts must not silently select the first record.
Cppcheck cleanup is limited to safe default directories newly created/extracted
by that installer run. Explicit overrides, pre-existing and unsafe/symlinked
directories are retained. `CPPCHECK_CLEAN_AFTER_BUILD=0` disables cleanup.
Derived HTML escapes untrusted text/attributes without changing native evidence
or the filesystem paths used to read source files.

The shared CSA/dependency argument sanitizer removes direct and forwarded
`-save-temps[=cwd|obj]` / `--save-temps` options; unknown modes fail rather than
being silently accepted. It rejects external `--config`, `--config-user-dir`
and `--config-system-dir` inputs until their contents can be captured. Operators
must expand approved arguments in the compile database. This is not a sandbox
or a claim that every possible compiler extension has no filesystem effects.

### 8.4 Validation and Errors

Malformed/unknown configuration or conflicting compile contexts raises an input
error (main CLI exit 2), before a scan run is created. A cleanup path that cannot
be proven safe is kept, not recursively removed. Missing YAML validation support
cannot be reported as verified evidence; provision PyYAML from `requirements.txt`.

SIGINT returns 130; SIGTERM returns 143. The main-thread run installs/restores
its SIGTERM handler and uses the common cancellation path to stop/reap active
process groups. If a run manifest exists, record `cancelled:true`, `ok:false`,
the cancellation exit code and completion time; an active scanner also gets a
cancelled receipt. A cancelled run is not exportable as a completed scan.

### 8.5 Good, Base and Bad Cases

An ordinary one-context TU runs normally. Repeated identical contexts select one
TU. Two macro configurations for the same source fail explicitly. A custom build
directory retains unrelated files. A source filename containing HTML markup is
rendered as text, not active markup.

### 8.6 Required Regression Checks

`python3 -m unittest discover -s tests` must exercise identical/conflicting
contexts and ordering, rejection before scan creation, installer-owned versus
caller-owned/symlinked paths, HTML index/detail/statistics escaping, and loading
the shipped configuration template with the real parser. Stub-driven tests do
not establish real analyzer or downstream acceptance.

`tests/test_execution_boundaries.py` additionally checks direct/forwarded flags,
external-config rejection before CSA execution, real-Clang preservation of a
pre-existing `sample.i`, active serial/threaded child cleanup on SIGTERM and
handler restoration. Existing SIGINT and timeout tests must continue to pass.

### 8.7 Wrong versus Correct

Wrong: keep the first macro configuration, delete all contents of a caller's
build directory, insert a raw filename into HTML, or source the old Shell config.
Correct: reject conflicting contexts, preserve caller-owned artifacts, escape
display values, and parse the documented JSON configuration.

### 8.8 Pinned Library Examples

`examples/README.md` documents a vendored TinyXML2 C++ build/scan fixture and a
GuardClauses C# build/run fixture. Each subset retains its license and a pinned
upstream commit with per-file SHA-256 checks in `tests/test_examples.py`. The C#
fixture does not add C# analyzer support: direct `.cs` input must still fail before
creating a run. Successful builds do not establish security coverage or release
acceptance, and neither example is a vulnerability-detection benchmark.
