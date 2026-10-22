# three-tool-sast-runner

[ English ](README.md) • [ 简体中文 ](README.zh-CN.md)

三工具自动化 C/C++ 静态代码分析执行器（Runner）：一键自动化调度 **Cppcheck**、**Clang-Tidy** 与 **Clang Static Analyzer (CSA)**，生成符合工业控制软件安全与 IEC 62443 标准要求的可审计结构化证据包。

> **当前状态：** 已有扫描执行器实现，端到端验收尚未完成。
> 当前输出 `scanner-evidence-1`，明确标记 `downstream_compatible=false` 和
> `release_ready=false`；另可导出产品范围的 `three-tool-scan-delivery-1.0`。
> 报告消费者负责绑定其本地映射资产并生成标准 `three-tool-input-1.0`；交付包不等于报告或发布批准。详见
> [剩余验收工作](scan/IMPLEMENTATION_STATUS.md)。

[示例库与验证命令](examples/README.md)：固定版本的 TinyXML2（C++，构建及扫描）和
GuardClauses（C#，仅构建/运行；尚未实现 C# 扫描）。

---

## 1. 项目概述

`three-tool-sast-runner` 旨在提供一套开箱即用的部署机/编译机端自动化扫描套件：
1. 从 CMake 导出的 `compile_commands.json` 自动解析出被测的编译单元（TU）；
2. 在**完全相同的 Git 提交与相同的编译数据库**基线上，无缝并行调度 **Cppcheck (2.22+)**、**Clang-Tidy (LLVM 14+)**、**Clang Static Analyzer (LLVM 14+)**；
3. 将产物整理为具有严格分层标识的自包含证据包（`[RAW]` 原生诊断、`[RECEIPT]` 运行收据、`[CACHE]` 内部缓存、`[DERIVED]` 衍生摘要）；
4. 导出可移交的产品范围 `three-tool-scan-delivery-1.0`，包含未修改的原生结果及哈希绑定的扫描/构建/版本收据。独立报告消费者负责绑定本地标准和映射，再准备规范报告输入。

完整技术规约与引擎实测行为请参考 [docs/SPEC.zh-CN.md](docs/SPEC.zh-CN.md)（中文）或 [docs/SPEC.md](docs/SPEC.md)（英文）。

---

## 2. 实际目录结构

```text
three-tool-sast-runner/
├── README.md / README.zh-CN.md
├── docs/SPEC.md / SPEC.zh-CN.md
├── analy.config.example    # JSON 模板，复制到 scan/analy.config
├── requirements.txt        # 原生 YAML 校验所需的 Python 依赖
├── install/
│   ├── install_all.sh      # 工具安装入口，可能联网或访问软件包源
│   ├── preflight.sh        # 二进制可用性检查，不是发布认证
│   └── cpp_check/          # 源码归档和安装器
├── scan/
│   ├── scan_all.sh         # 主执行入口
│   ├── verify.sh           # 临时合成工程的真实分析器冒烟验证
│   ├── serve.sh            # 本地报告浏览
│   ├── IMPLEMENTATION_STATUS.md
│   ├── cpp_check/scan.sh
│   ├── clang_tidy/scan.sh
│   └── clang_static_analyzer/scan.sh
└── tests/
```

---

## 3. 产物四层分层定义与标识

| 分层标识 | 类别定义 | 生产者主体 | 在下游报告生成中的作用 |
| :--- | :--- | :--- | :--- |
| **`[RAW]`** | **分析器原生诊断层** | 编译器/分析器二进制直接写出的结构化文件 | **核心法定证据**：下游映射与评级引擎直接消费此层数据 |
| **`[RECEIPT]`** | **执行器元数据与收据层** | 自动化调度 Runner 脚本记录的环境与进程回执 | **审计与对账证据**：证明环境真实、命令可溯、所有 TU 均被执行 |
| **`[CACHE]`** | **分析器内部缓存层** | 分析器二进制内部中间状态 | **性能优化数据**：证明启用了持久化构建缓存，非最终审计报告 |
| **`[DERIVED]`** | **衍生统计与浏览层** | 后处理脚本或转换程序生成的 Markdown/HTML | **人工阅读辅助**：供开发者快速浏览，下游生成器不以此为数据源 |

---

## 4. 核心引擎行为与零结果机制（实测铁律）

在开发扫描工具时，必须遵守底层静态分析器的实际行为特性：

1. **Cppcheck:** 
   * 当检测结果为 0 缺陷时，原生命令输出包含标准 XML 结构的文本：`<results version="2"><cppcheck .../><errors></errors></results>`。空文件或非 XML 属于异常失败。
2. **Clang-Tidy:**
   * 当某个源文件为 0 告警时，**Clang-Tidy 原生机制绝不创建 YAML 文件**。
   * 因此 `fixes/` 下的 `.yaml` 文件数通常 $\le$ 选定的 TU 总数。覆盖完整性由 `clang-tidy-summary.json` 中的 `files` 数组（`status: ok`, `unique_diagnostics: 0`）对账，绝不可死卡 YAML 文件数。
3. **Clang Static Analyzer:**
   * 当某个源文件为 0 缺陷时，**Clang 依然会生成合法的空 Plist 文件**（包含完整的 XML 声明和 `<dict>`，但 `<key>diagnostics</key><array></array>` 为空）。
   * 门禁铁律：`csa-native/` 目录下的 `.plist` 文件总数，**必须严格等于选定的 TU 总数**，由 `tu-mapping.csv` 记录映射。

---

## 5. 快速上手

在目标 Ubuntu 24.04 主机上，从仓库根目录执行。先用 `uv sync --frozen` 准备锁定的 Python 3.12.15 环境，并另外准备
Cppcheck 2.22+ 和 LLVM 14+。内置 Cppcheck 源码仍是开发快照，不满足证据门禁的版本要求。
精简 Ubuntu 镜像还需 `python3-venv` 创建虚拟环境、`xz-utils` 解压内置源码；
源码编译另需相应构建工具链。

```bash
uv sync --frozen
bash install/preflight.sh
cp analy.config.example scan/analy.config
```

编辑 `scan/analy.config`，只填写 JSON 字段 `scan_paths`（非空路径数组）和
`result_path`（输出目录），替换模板占位路径。配置内相对路径以 `scan/` 为基准，
不是当前 Shell 目录。不支持 Shell 赋值和旧 `SCAN_CODE_DIR` / `BUILD_DIR` 字段。

```bash
uv run --frozen bash scan/scan_all.sh
# 等价的命令行输入；其中相对路径以当前目录为基准：
uv run --frozen bash scan/scan_all.sh --scan-path /path/to/product/src --result-path /path/to/results
# 只扫描临时缺陷样例，不扫描所传业务源码：
uv run --frozen bash scan/verify.sh --scan-path /path/to/product/src --result-path /path/to/smoke-results
PYTHONDONTWRITEBYTECODE=1 uv run --frozen python -m unittest discover -s tests
uv run --frozen python scan/handoff.py export \
  --profile /path/to/product-profile.json \
  --run /path/to/results/RUN_ID \
  --output /path/to/new-product-delivery
```

执行器从每个扫描路径逐级向上寻找 `compile_commands.json`、
`build/compile_commands.json` 或 `build/Debug/compile_commands.json`。
三工具证据扫描需要共同的编译数据库。同一数据库内源码的编译配置冲突会被拒绝，应先选定经确认的
构建配置；传入多个目录不能证明业务产品隔离。

`install/install_all.sh` 可能调用 apt 或下载软件包，不是完整离线安装器。断网运行前须先
准备分析器、Python 依赖和构建资源。退出码 0 表示执行成功，不代表发布批准；1 表示执行或
报告失败，2 表示输入错误，3 表示执行成功但证据门禁失败，130 表示取消。冒烟测试另有样例
验证结果，不能认证产品或源码基线。

---

## 6. 产品范围证据交付

复制并填写运维方维护的
[`examples/runner-handoff/product-profile.example.json`](examples/runner-handoff/product-profile.example.json)，明确产品、组件、目标、配置、路径和语言范围。首个交付通道只接受一个编译数据库组、一个组件及一个目标/配置；每个选中 TU 都必须落在已声明路径/语言范围内。多组件或归属不清时须先由产品负责人拆分/确认，执行器不会从编译数据库推断业务产品。

`scan/handoff.py export` 生成新的可移交目录，包含哈希绑定的文件清单、原样复制的原生扫描结果、源码/构建标识、类型化执行收据、实际工具版本、完整版本横幅、二进制哈希和计数；不包含映射、报告路径或发布策略。原始收据可能暴露主机绝对路径，原生输出可能含源码片段，转移前按产品方数据处理要求审核。参见[交付说明](examples/runner-handoff/README.md)。

接收端 `sast-report-pipeline` 才负责生成规范 `three-tool-input-1.0`、绑定冻结映射、严格审计、生成/验证报告并打包。扫描交付不等于报告、漏洞确认、发布批准或 IEC 62443 认证。

CSA 使用 LLVM 原生 `plist-html`：每个 TU 只分析一次，在独立目录同时生成两种格式。合法零诊断 plist 可无 HTML，有诊断则须有 HTML。报告输入仍为原生 plist，执行收据只记录实际运行的命令。

---

## 7. 许可证与合规标准
专为工业功能安全与工控网络安全合规设计（遵循 IEC 62443-4-1 SI-1 与 SI-2）。
