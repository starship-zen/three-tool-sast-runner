# three-tool-sast-runner

[ English ](README.md) • [ 简体中文 ](README.zh-CN.md)

三工具自动化 C/C++ 静态代码分析执行器（Runner）：一键自动化调度 **Cppcheck**、**Clang-Tidy** 与 **Clang Static Analyzer (CSA)**，生成符合工业控制软件安全与 IEC 62443 标准要求的可审计结构化证据包。

> **当前仓库状态：** `v1.0 规格定义与数据契约基线（开发推进中）`  
> 本仓库当前已固化权威的数据接口契约、四层产物证据规范与目录布局。自动化调度脚本与 CLI 工具（`scan_all.sh`、`preflight.sh`、`verify.sh`）正由工程团队进行开发、测试与集成。

---

## 1. 项目概述

`three-tool-sast-runner` 旨在提供一套开箱即用的部署机/编译机端自动化扫描套件：
1. 从 CMake 导出的 `compile_commands.json` 自动解析出被测的编译单元（TU）；
2. 在**完全相同的 Git 提交与相同的编译数据库**基线上，无缝并行调度 **Cppcheck (2.22+)**、**Clang-Tidy (LLVM 14+)**、**Clang Static Analyzer (LLVM 14+)**；
3. 将产物整理为具有严格分层标识的自包含证据包（`[RAW]` 原生诊断、`[RECEIPT]` 运行收据、`[CACHE]` 内部缓存、`[DERIVED]` 衍生摘要）；
4. 输出符合 `three-tool-input-1.0` 规范的标准包，供下游离线报告生成器直接消费（自动对账并映射至 CWE Top 25:2025、OWASP Top 10:2025，并评定 Critical/High/Medium/Low 四级工业审计风险）。

完整技术规约与引擎实测行为请参考 [docs/SPEC.zh-CN.md](docs/SPEC.zh-CN.md)（中文）或 [docs/SPEC.md](docs/SPEC.md)（英文）。

---

## 2. 目标目录树结构

```text
three-tool-sast-runner/
├── README.md               # 项目自述（英文）
├── README.zh-CN.md         # 项目自述（中文）
├── docs/
│   ├── SPEC.md             # 完整技术契约与引擎实测规则（英文）
│   └── SPEC.zh-CN.md       # 完整技术契约与引擎实测规则（中文）
├── analy.config.example    # 扫描配置模板文件
├── scan_all.sh             # [目标脚本] 主执行运行器
├── preflight.sh            # [目标脚本] 主机依赖环境预检脚本
├── verify.sh               # [目标脚本] 针对合成最小工程的端到端冒烟测试
├── scripts/                # [目标脚本] 辅助工具与环境快照采集
│   ├── analy_config.sh
│   └── collect_acceptance_env.sh
├── cpp_check/              # [目标模块] Cppcheck 包装器与源码构建
│   ├── scan.sh
│   └── scripts/
├── clang_tidy/             # [目标模块] Clang-Tidy 多进程并行运行器
│   ├── scan.sh
│   └── scripts/
└── clang_static_analyzer/  # [目标模块] CSA 逐 TU 运行器（输出原生 Plist）
    ├── scan.sh
    └── scripts/
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

## 5. 快速上手（目标使用流程）

### 5.1 环境自检
在部署机上验证基础依赖（Bash, Python 3, CMake, Clang, Clang-Tidy, Cppcheck）：
```bash
bash preflight.sh
```

### 5.2 冒烟验证
在临时合成工程上执行自检验证：
```bash
bash verify.sh
```

### 5.3 配置并启动扫描
1. 复制配置模板：
   ```bash
   cp analy.config.example analy.config
   ```
2. 编辑 `analy.config` 指定工程路径：
   ```bash
   SCAN_CODE_DIR=/path/to/source/root
   WORK_DIR=/path/to/output/directory
   BUILD_DIR=/path/to/cmake/build   # 必须包含 compile_commands.json
   SCAN_SUBDIRS=src,include         # 可选，留空自动扫描全库
   ```
3. 运行完整扫描套件：
   ```bash
   bash scan_all.sh
   ```

---

## 6. 下游自动化报告生成对接

本执行器产出的自包含证据包，直接由下游离线报告生成器消费：

```bash
# 生成最终版 IEC 62443 合规交付报告（含 10 页 PDF 主报告、HTML/Markdown 附录、CSV、SARIF）
python3 scripts/three_tool_report.py generate \
  --product <product_id> \
  --manifest "<output_dir>/manifest.json" \
  --output "<report_run_dir>" \
  --mapping-mode strict \
  --pdf
```

---

## 7. 许可证与合规标准
专为工业功能安全与工控网络安全合规设计（遵循 IEC 62443-4-1 SI-1 与 SI-2）。
