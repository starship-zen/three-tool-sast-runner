# 第三方操作指南：生成可移交的三工具扫描 ZIP

本指南面向实际运行扫描的产品方。标准交付只需要本仓库、产品源码和已准备好的本机分析器；不需要报告生成器仓库、映射资产、AI 或在线服务。输出是**扫描证据**，不表示漏洞确认、修复完成或客户发布批准。

## 1. 支持范围与准备条件

- 受支持的主机环境是 Ubuntu 24.04。Python 使用仓库锁定的 CPython 3.12.15 与 `uv.lock`；Cppcheck 2.22+ 和 LLVM 14+ 是本 Runner 的本机执行最低版本，不表示报告端已对所有后续版本或检查项完成规则映射。本轮合成原生运行实际使用 Cppcheck 2.22.0 与 LLVM/Clang 18.1.3；每次交付前仍须和报告方锁定具体工具版本及启用的检查项，升级版本或改变检查项前先完成联合验收。
- 交付扫描只支持 C 和 C++。C#、Web/JavaScript 及其他语言都不会由本 Runner 扫描；若产品含这些语言，应在产品配置的 `unsupported_languages` 中列出并保留为覆盖缺口。
- 标准交付要求一个干净 Git 源码基线、一个编译数据库组、一个明确组件、一个目标/配置。工具只根据编译数据库和显式扫描路径选择 TU；被选中的 TU 数量不证明整个产品、所有目标或所有语言均已覆盖。
- 扫描结果目录与 ZIP 必须放在源码 checkout 之外，且每次使用新的、不存在的路径。成功和失败的完整运行目录都会保留，供复核或重新扫描。

在产品方批准的主机上准备 Runner checkout 和本机工具。不要把占位仓库地址或示例产品配置当作真实产品身份；由产品负责人提供批准的源码来源、组件/目标/配置及范围。以下 `$RUNNER_REPO`、`$SOURCE_ROOT`、`$WORK`、`$PROFILE` 和工具目录均由操作者填入本机实际值。

```bash
git clone "$APPROVED_RUNNER_REMOTE" "$RUNNER_REPO"
cd "$RUNNER_REPO"
git status --porcelain
uv sync --frozen
export PATH="<Cppcheck bin>:<LLVM bin>:$PATH"
bash install/preflight.sh
```

`git status --porcelain` 应为空。`uv sync --frozen` 使用锁文件；离线主机需事先具备对应 Python 与依赖缓存。`install/preflight.sh` 汇总检查 `cppcheck`、`clang-tidy`、`clang` 和 `clang++` 是否可找到；它不安装或认证这些二进制。新的一键命令还会在扫描前检查稳定版本与最低版本。Runner 通过 `PATH` 发现分析器，没有单工具环境变量覆盖；工具缺失或版本不符时先由主机管理员配置，不要绕过检查。

## 2. 一次性配置产品范围

复制 [产品范围配置模板](../examples/runner-handoff/product-profile.example.json)，在 Runner checkout 之外保存一份产品方批准的 JSON。此 profile 是一次性输入，不是扫描结果 manifest。字段必须与模板一致：

| 字段 | 操作者填写内容 |
| --- | --- |
| `product_id`、`display_name` | 稳定产品标识与产品方使用的名称 |
| `repository` | 产品源码仓库标识；不要包含 URL 用户名或凭证 |
| `component_id`、`target_id`、`configuration` | 经产品负责人确认的单一组件、构建目标和配置标识 |
| `included_paths`、`excluded_paths` | 相对于 Git 仓库根目录的路径模式；选中的每个 TU 必须属于包含范围且不命中排除范围 |
| `languages` | 本次交付中实际支持的 `C` 和/或 `C++` |
| `unsupported_languages` | 产品内存在但本 Runner 不扫描的语言，例如 `C#` |

profile 不提供 Reporter 仓库路径、映射、风险规则或客户发布选项。不要为“凑齐字段”猜产品身份或路径。一个包只承载一个产品范围；多组件或目标归属不清时先由产品负责人拆分和确认。

**样例专用配置：**仓库内 TinyXML2 仅用于验证 Runner 命令，不代表真实产品。可将模板复制到 `$WORK/sample-profile.json`，仅在这份临时文件中填写 `product_id: "runner-sample"`、`display_name: "Runner sample only"`、`repository: "local-runner-sample"`、`component_id: "cpp-smoke"`、`target_id: "cpp-default"`、`configuration: "cmake-default"`，并把 `included_paths` 设为 `["examples/cpp/**"]`、`excluded_paths` 设为 `["examples/cpp/build/**"]`、`languages` 设为 `["C++"]`、`unsupported_languages` 设为空数组。所有值都明确属于本地样例，不能复制为生产配置。

## 3. 从干净 checkout 验证一个小样例

仓库样例在新 checkout 中生成编译数据库并构建：

```bash
cmake -S examples/cpp -B examples/cpp/build -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
cmake --build examples/cpp/build --parallel 2
```

创建一次性的样例工作目录和 profile：

```bash
WORK="$(mktemp -d)"
cat > "$WORK/sample-profile.json" <<'JSON'
{
  "schema_version": "three-tool-product-profile-1.0",
  "product_id": "runner-sample",
  "display_name": "Runner sample only",
  "repository": "local-runner-sample",
  "component_id": "cpp-smoke",
  "target_id": "cpp-default",
  "configuration": "cmake-default",
  "included_paths": ["examples/cpp/**"],
  "excluded_paths": ["examples/cpp/build/**"],
  "languages": ["C++"],
  "unsupported_languages": []
}
JSON
```

TinyXML2 样例编译数据库应选择 `smoke.cpp` 与 `tinyxml2/tinyxml2.cpp` 两个 TU。头文件随编译依赖分析，不单独计为 TU。构建成功只证明样例可以构建，不代表安全扫描成功；C# 样例也只是构建/运行示例，不能作为 C# 扫描证据。

样例 profile 放在 checkout 外；下面以 `$WORK` 为例，先建立结果目录父目录，再选择一个唯一运行 ID：

```bash
mkdir -p "$WORK/results" "$WORK/deliveries"
RUN_ID="sample-$(date -u +%Y%m%dT%H%M%SZ)-$$"
uv run --frozen --offline python scan/deliver.py \
  --profile "$WORK/sample-profile.json" \
  --scan-path "$PWD/examples/cpp" \
  --result-path "$WORK/results/$RUN_ID" \
  --output "$WORK/deliveries/$RUN_ID.zip"
```

成功时退出码为 0，标准输出只有一行 JSON。至少核对：`status` 是 `delivered`、`scan_status` 是 `completed`、`product_id` 与 profile 一致、`selected_analysis_units` 为实际选中数量、`run_id`/`run_path` 标识本次运行、`zip_path`/`zip_sha256` 标识最终 ZIP，且 `release_approved` 为 `false`。`scanner_reported_diagnostics` 是工具报告的诊断计数，不是错误码；有诊断也可以完成扫描。零诊断也可成功，但必须有各工具真实的零结果证据和通过的执行证据门禁。

ZIP 在归档根目录包含 `manifest.json`、`delivery-receipt.json` 和收据绑定的文件，不增加外层目录。Runner 在交付前逐项复核收据中的大小和 SHA-256、ZIP 成员集合、ZIP 内字节以及当前接收端的文件数/大小限制。不要手工编辑、重打包或从 ZIP 中删文件。

## 4. 实际产品扫描与交付

先在产品方批准的干净 Git checkout 中完成该产品正常构建，并为**本次源码提交与配置**生成新的 `compile_commands.json`。CMake 项目可按批准的配置执行：

```bash
cmake -S "$SOURCE_ROOT" -B "$SOURCE_ROOT/build" \
  -DCMAKE_EXPORT_COMPILE_COMMANDS=ON \
  -DCMAKE_BUILD_TYPE="$APPROVED_BUILD_CONFIGURATION"
cmake --build "$SOURCE_ROOT/build" \
  --target "$APPROVED_TARGET" --parallel "$BUILD_JOBS"
```

在干净 checkout 中使用新的 `build/` 生成数据库；扫描路径的祖先必须能找到 `build/compile_commands.json`。不要把旧构建目录的数据库当作当前配置，也不要手工编辑编译数据库。CMake 导出的数据库可能涵盖多个目标；Runner 按显式源码路径选择 TU，不按 CMake target 名过滤。构建负责人须确认本次数据库确实符合 profile 声明的单一组件/目标/配置；无法隔离时先由构建负责人提供合适的数据库，不要把所选 TU 清单解释成完整产品覆盖。同一 TU 出现互相冲突的参数时 Runner 会拒绝继续。

把产品 profile 放到 checkout 外。`included_paths` 和 `excluded_paths` 相对于 Git 根目录填写；它们约束实际选择的 TU。profile 中的 `target_id` 与 `configuration` 是产品方给定的身份说明，Runner 不会从编译数据库推断或证明业务目标归属。若构建数据库不能把组件/目标范围清楚隔离，先生成产品方认可的独立构建数据库，不要声称当前输出覆盖整个产品。

```bash
# 每次运行新建独立结果目录和 ZIP 路径。
RUN_ID="<产品标识>-$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "$WORK/results" "$WORK/deliveries"
uv run --frozen --offline python scan/deliver.py \
  --profile "$PROFILE" \
  --scan-path "$SOURCE_ROOT/src/<已批准组件路径>" \
  --result-path "$WORK/results/$RUN_ID" \
  --output "$WORK/deliveries/$RUN_ID.zip"
```

`<...>` 是必须由产品负责人替换的占位内容，不是可直接运行的生产路径。多个同一编译数据库下的源码目录可重复传入 `--scan-path`；不同数据库组、组件或目标请分开确认，不要混入同一交付。

**发给报告方的文件：**只发送成功 JSON 中 `zip_path` 指向的 ZIP，并用 `zip_sha256` 核对传输前后文件。不要发送源码、整个 run 目录、临时 profile、分析器安装目录或个人机器路径；报告方通过安全 ZIP 接收流程读取文件，不需要你解压或改 manifest。保留原始产品源码、fresh compile database 和 `$WORK/results/$RUN_ID` 直到报告方确认接收；后续复扫使用新的源码构建、结果目录和 ZIP。

若扫描完成但 ZIP 超出当前接收端上限，命令会以 `stage: zip`、退出码 `4` 失败，并在错误 JSON 中给出完整的 `run_path` 与已导出的 `delivery_directory`；不会发布 ZIP。保留这两个目录，不要删减或拆分文件。只有 ZIP 本身超过 64 MiB，而单文件、成员数、展开总量等其余接收限制均满足时，才可由报告方受控接收人接收完整 delivery 目录，在报告仓库中运行 `sast_scan.py prepare-runner --delivery <delivery-directory> --product <product_id> --output-dir <new-input-dir>`，随后执行完整映射审计。目录接收仍受 128 MiB 单文件、1,000 个成员和 256 MiB 总量等限制；超出这些限制时停止，联系接收方确认受支持方案。这个目录路径只供报告方 CLI 接收；扫描方无需安装报告仓库，也不要自行用通用解压工具处理 ZIP。

现有两步 CLI 仍可用于调试或接收方明确需要目录时：先运行 `scan/scan_all.sh`，再用 `scan/handoff.py export --profile ... --run ... --output <新目录>`。此路径导出标准交付目录；要生成推荐上传 ZIP，请使用上面的一键命令。

## 5. 失败、零结果、取消与重试

| 输出/退出 | 含义 | 操作 |
| --- | --- | --- |
| `status: delivered`、退出码 `0` | 三工具执行与证据门禁完成，ZIP 已复核 | 核对产品、run、ZIP 路径和 SHA；诊断数可以大于 0 |
| `stage: preflight`、退出码 `2` | profile、Git 状态、编译数据库、工具或输出路径不满足前置条件 | 按 JSON `error` 修正配置/环境；此阶段不会开始扫描 |
| `stage: scan`、退出码 `1` | 分析器或执行器失败 | 保留 JSON 提供的 run 路径；根据目录中的工具收据排障，不发送部分结果 ZIP |
| `stage: scan`、退出码 `3` | 扫描运行结束，但三工具证据门禁未通过 | 保留 run 目录；这是不完整证据，不是成功交付 |
| `stage: scan`、退出码 `130`/`143` | 收到 Ctrl+C / SIGTERM 并取消 | 等待 Runner 清理并记录活动进程；保留 run，修复原因后用全新路径重跑 |
| `stage: export` 或 `zip`、退出码 `4` | 扫描已结束，但正式导出或 ZIP 校验失败 | 保留完整 run；如 JSON 给出 `delivery_directory`，也一并保留供检查；不得手改文件后伪装成功 |

零诊断不是执行失败：Clang-Tidy 零结果不会生成 YAML，Runner 使用逐 TU 收据证明零结果；CSA 每个已选 TU 仍需有效 plist。反过来，YAML/plist/XML 缺失、版本不符、目录不一致或门禁失败也不能解释为零漏洞。

每个结果根目录与 ZIP 都采用不覆盖策略。失败后不要复用旧 `--result-path` 或覆盖已有 ZIP；更正原因后创建新的运行 ID、结果目录和 ZIP 路径。失败的原始运行目录用于排障，修复后重新执行扫描，不对原始运行产物手工修补。

## 6. 常见故障

| 现象 | 处理 |
| --- | --- |
| `[MISS]` 或工具版本不支持 | 将经批准的 Cppcheck/LLVM 二进制目录加入 `PATH`，重新运行 `bash install/preflight.sh`；版本仍不符时由主机管理员升级，不能用旧版结果继续导出。 |
| 找不到编译数据库或选中 `0` 个 TU | 重新生成当前源码/配置的 `compile_commands.json`；检查 `--scan-path` 是否在其目录或子目录，数据库是否位于根、`build/` 或 `build/Debug/`。 |
| 多个 compile-database group | 本次路径解析到多个构建数据库；按产品批准范围拆分扫描，每包只保留一个数据库组。 |
| profile scope 不匹配 | 核对 Git 根相对路径模式、排除规则与 `languages`。只改产品方批准的 profile 后，用新结果路径重扫。 |
| Git baseline dirty | 从干净 checkout 重建；不要通过忽略工作区变更或伪造 commit 来通过门禁。 |
| ZIP 超过当前 64 MiB 上传上限或交付文件超过接收端限制 | 保留 run 与错误 JSON，联系报告方确认当前正式接收方案；不要自行拆包、删原生证据或改变接收限制。 |
| 目录内有 C# / 其他不支持语言 | 将其列入 `unsupported_languages` 并明确报告覆盖缺口；本 Runner 没有 C# 分析器，C# 构建通过不等于 C# 安全扫描。 |

## 7. 交付边界

本流程只生成哈希绑定的扫描证据包。报告方还要按其冻结映射执行导入与严格映射审计，再生成和核验报告候选；人工真阳性/误报、风险、处置、复扫和客户发布审批仍分别处理。Runner 生成的 `status: delivered` 不会替代这些步骤。
