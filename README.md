# three-tool-sast-runner

[ English ](README.md) • [ 简体中文 ](README.zh-CN.md)

Automated C/C++ static analysis runner bundling **Cppcheck**, **Clang-Tidy**, and **Clang Static Analyzer (CSA)** to produce standardized, auditable evidence packages for industrial control software and IEC 62443 compliance.

> **Current status:** The runner emits `scanner-evidence-1` with
> `downstream_compatible=false` and `release_ready=false`, and can export a
> separate product-scoped `three-tool-scan-delivery-1.0`. The report consumer
> owns the adapter that binds its local mappings/assets and creates canonical
> `three-tool-input-1.0`; neither delivery nor report candidate grants release
> approval. See [remaining acceptance work](scan/IMPLEMENTATION_STATUS.md).

[Example libraries](examples/README.md): pinned TinyXML2 (C++, build and scan) and
GuardClauses (C#, build/run only; C# scanning is not implemented).

---

## 1. Overview

`three-tool-sast-runner` defines a turnkey on-host scanner automation suite designed to:
1. Discover target Translation Units (TUs) from CMake `compile_commands.json`.
2. Run **Cppcheck (2.22+)**, **Clang-Tidy (LLVM 14+)**, and **Clang Static Analyzer (LLVM 14+)** on the exact same commit and compile database.
3. Format outputs into a strictly layered, immutable evidence bundle (`[RAW]` diagnostics, `[RECEIPT]` execution receipts, `[CACHE]` states, and `[DERIVED]` summaries).
4. Export a portable, product-scoped `three-tool-scan-delivery-1.0` containing unchanged native outputs and hash-bound scan/build/version receipts. The separate report consumer binds its own approved standards and mappings when preparing canonical input; the runner does not ship reporting policy.

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

Run from the repository root on the intended Ubuntu 24.04 host. Provision the
locked Python 3.12.15 environment with `uv sync --frozen`; provision Cppcheck 2.22+
and LLVM 14+ separately. The bundled Cppcheck source
is a development snapshot, not the qualified version required by the evidence gate.
Minimal Ubuntu images also need `python3-venv` for the virtual environment and
`xz-utils` for the bundled source archive; source builds need their own toolchain.

```bash
uv sync --frozen
bash install/preflight.sh
cp analy.config.example scan/analy.config
```

Edit `scan/analy.config` as JSON using only `scan_paths` (nonempty path array)
and `result_path` (output directory). Replace the template's placeholder paths.
Relative paths are resolved against `scan/`, not the shell's working directory.
Shell assignments and the old `SCAN_CODE_DIR` / `BUILD_DIR` keys are not supported.

```bash
uv run --frozen bash scan/scan_all.sh
# Equivalent CLI input; these paths are relative to the current directory:
uv run --frozen bash scan/scan_all.sh --scan-path /path/to/product/src --result-path /path/to/results
# Temporary defect fixture only; it does not scan the supplied business source:
uv run --frozen bash scan/verify.sh --scan-path /path/to/product/src --result-path /path/to/smoke-results
PYTHONDONTWRITEBYTECODE=1 uv run --frozen python -m unittest discover -s tests
uv run --frozen python scan/handoff.py export \
  --profile /path/to/product-profile.json \
  --run /path/to/results/RUN_ID \
  --output /path/to/new-product-delivery
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

## 6. Product-scoped delivery

Copy and complete the operator-owned profile template at
[`examples/runner-handoff/product-profile.example.json`](examples/runner-handoff/product-profile.example.json).
The exporter requires one compile-database group and one explicitly declared
component/target/configuration; every selected TU must fit the profile's
repository-relative path and C/C++ language scope. It rejects failed evidence
gates and multiple groups, and never infers business ownership from compile
database labels.

`scan/handoff.py export` writes a new, portable delivery with a hash-bound file
inventory, unchanged native scanner bytes, source/build identity, typed
execution receipts, actual tool release tokens, captured version banners and
binary hashes, plus explicit artifact and zero-tidy counts. The original scan
run and source checkout are not needed to verify the delivery after copying.
The native outputs and raw provenance can contain absolute source/build paths
and source excerpts; apply the product owner's data-handling policy before
transfer. See [the delivery instructions](examples/runner-handoff/README.md)
for the exact profile and command.

The receiving `sast-report-pipeline` owns canonical `three-tool-input-1.0`
preparation, frozen taxonomy assets, strict mapping audit, report generation,
verification and packing. A delivery is not itself a canonical report input,
vulnerability confirmation, release approval or IEC 62443 certification.

CSA uses LLVM's native `plist-html` output: one analysis per TU produces both
formats in a TU-isolated directory. Valid zero-diagnostic plist needs no HTML;
findings require HTML. The native plist remains the reporting input, and the
receipt records only the command that actually ran.

---

## 7. License & Compliance
Designed for industrial functional safety and cybersecurity compliance (IEC 62443-4-1 SI-1 & SI-2).
