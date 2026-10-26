# Product delivery profile example

`product-profile.example.json` is a template, not an approved product profile
or scan result. Copy it into an operator-controlled location and set the
owner-approved product/repository/component/target/configuration identity,
complete C/C++ include and exclude scope, and languages. The profile does not
contain report-pipeline paths, standards, or rule mappings; a receiving
reporter binds its own approved taxonomy assets after delivery.

This first delivery lane accepts exactly one compile-database group, one
explicit component, and one target/configuration per delivery. All selected
translation units must fit the declared path/language scope. Multi-component
compile databases must be split or resolved by the product owner before
exporting; the exporter does not infer ownership from included paths or the
build database. The optional profile `path_ownership` array declares literal
Git-root-relative prefixes as `first-party`, `vendored`, or `system`; nested
declarations use the most-specific prefix downstream. Omitted declarations
remain unclassified.
The producer profile reserves the `diagnostic-files` prefix used for neutral
receiver labels. A broad boundary there could classify a header whose source
identity is unavailable; do not use it for repository ownership. The receiver's
offline supplement may name a complete observed label when attaching a legacy
ownership declaration.

Run scanning only after provisioning the approved analyzers and reviewing the
runner configuration. A completed scan requires the three-tool evidence gate
to pass; positive CSA findings remain a completed scan. A failed tool or failed
evidence gate cannot be exported as a complete delivery.

```sh
uv sync --frozen
uv run --frozen --offline bash scan/scan_all.sh --scan-path /path/to/product/src --result-path /path/to/results
uv run --frozen python scan/handoff.py export \
  --profile /path/to/product-profile.json \
  --run /path/to/results/RUN_ID \
  --output /path/to/new-product-delivery
```

The delivery contains `manifest.json`, `delivery-receipt.json`, unchanged
native Cppcheck XML / clang-tidy YAML / CSA plist bytes, captured build and
source hashes, scanner receipts, actual numeric tool releases, complete version
banners, binary hashes, counts, and per-file SHA-256 receipts. It has no mapping
or release-approval policy. Raw provenance and native diagnostics can include
absolute source/build paths or source excerpts; apply the product owner's data
handling policy before transferring the delivery to another machine.

For primary native diagnostic paths that resolve to a tracked regular file
inside the same clean captured Git commit, the exporter can add a
`source_path_identities` entry with the exact native path, source-relative path,
and export-time SHA-256. The reporter can use this producer-supplied identity to
project an explicit source ownership boundary onto the existing diagnostic
label without changing finding locations or IDs. Missing, outside, untracked
or unresolved paths remain unclassified. This metadata does not prove which
bytes the scanner consumed and does not authenticate execution.

The command refuses an existing output path. The delivery is portable: its
file references are relative and the receiver does not need the original scan
run or source checkout to verify the copied bytes. Exact absolute selected
source strings remain opaque analysis-unit identifiers so native clang-tidy
`MainSourceFile` bytes are not rewritten.

For clang-tidy YAML artifacts, the exporter also binds captured native stdout
and stderr logs, when present, through optional `diagnostic_logs` references
and matching `native-diagnostic-log` provenance. A receiver can use an unambiguous native
warning to corroborate line/column without accessing the original source.
The logs do not replace YAML, alter counts or prove authenticated execution;
older deliveries without these references cannot promise equivalent locations.
