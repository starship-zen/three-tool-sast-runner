# three-tool-sast-runner

[ English ](README.md) • [ 简体中文 ](README.zh-CN.md)

三工具自动化 C/C++ 静态代码分析执行器（Runner）：一键自动化调度 **Cppcheck**、**Clang-Tidy** 与 **Clang Static Analyzer (CSA)**，生成符合工业控制软件安全与 IEC 62443 标准要求的可审计结构化证据包。

> **当前状态：** 已有扫描执行器实现，端到端验收尚未完成。
> 当前输出 `scanner-evidence-1`，明确标记 `downstream_compatible=false` 和
> `release_ready=false`。下游契约导出和业务产品隔离继续暂缓，详见
> [剩余验收工作](scan/IMPLEMENTATION_STATUS.md)。

---

## 1. 项目概述

`three-tool-sast-runner` 旨在提供一套开箱即用的部署机/编译机端自动化扫描套件：
1. 从 CMake 导出的 `compile_commands.json` 自动解析出被测的编译单元（TU）；
2. 在**完全相同的 Git 提交与相同的编译数据库**基线上，无缝并行调度 **Cppcheck (2.22+)**、**Clang-Tidy (LLVM 14+)**、**Clang Static Analyzer (LLVM 14+)**；
3. 将产物整理为具有严格分层标识的自包含证据包（`[RAW]` 原生诊断、`[RECEIPT]` 运行收据、`[CACHE]` 内部缓存、`[DERIVED]` 衍生摘要）；
4. 以 `three-tool-input-1.0` 为目标契约，支持后续离线报告的 CWE Top 25:2025、OWASP Top 10:2025 映射和 Critical/High/Medium/Low 四级风险评定。当前内部证据包尚不能直接输入下游生成器。

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

在目标 Ubuntu 24.04 主机上，从仓库根目录执行。需要 Python 3.10+，并另外准备
Cppcheck 2.22+ 和 LLVM 14+。内置 Cppcheck 源码仍是开发快照，不满足证据门禁的版本要求。
精简 Ubuntu 镜像还需 `python3-venv` 创建虚拟环境、`xz-utils` 解压内置源码；
源码编译另需相应构建工具链。

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
bash install/preflight.sh
cp analy.config.example scan/analy.config
```

编辑 `scan/analy.config`，只填写 JSON 字段 `scan_paths`（非空路径数组）和
`result_path`（输出目录），替换模板占位路径。配置内相对路径以 `scan/` 为基准，
不是当前 Shell 目录。不支持 Shell 赋值和旧 `SCAN_CODE_DIR` / `BUILD_DIR` 字段。

```bash
bash scan/scan_all.sh
# 等价的命令行输入；其中相对路径以当前目录为基准：
bash scan/scan_all.sh --scan-path /path/to/product/src --result-path /path/to/results
# 只扫描临时缺陷样例，不扫描所传业务源码：
bash scan/verify.sh --scan-path /path/to/product/src --result-path /path/to/smoke-results
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
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

## 6. 下游自动化报告生成对接（目标契约）

下游是独立的 `sast-report-pipeline`。以下是目标接口，不是当前执行器已经打通的交接流程。
须按 [SPEC 第 7 节](docs/SPEC.zh-CN.md#7-对接下游离线报告生成器)
生成根清单，包含 v1.2 producer 标记、本地冻结资产和哈希绑定收据；仅有编号目录不能导入。
clang-tidy 零告警仅可在显式 summary 收据校验通过后省略 YAML，不得伪造 YAML。

```bash
# 生成待审候选报告包，不等于客户发布批准。
python3 scripts/three_tool_report.py generate \
  --product <product_id> \
  --manifest "<output_dir>/manifest.json" \
  --output "<report_run_dir>" \
  --mapping-mode strict
```

产物是候选报告，不是客户发布或 IEC 62443 认证。命令行 PDF 须另行验收渲染工具链，
网页工作台当前不支持 PDF。执行器测试通过，不证明下游导入、产品归属、映射或发布门禁通过。

---

## 7. 许可证与合规标准
专为工业功能安全与工控网络安全合规设计（遵循 IEC 62443-4-1 SI-1 与 SI-2）。
