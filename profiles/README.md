# 产品范围推荐预设配置（Preset Profiles）

本目录包含为智港通本次交付提供的**推荐预设配置文件**，用于标准化界定代码扫描范围。执行一键交付命令时，直接通过 `--profile` 参数指定对应的配置文件即可。

## 预设清单

| 配置文件 | 目标产品 | 推荐声明范围 | 排除构建输出 | 目标语言 | 默认构建标签 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `profiles/secp.json` | `secp` | `src/**` | `build/**`, `build_*/**` | `C++` | `cmake-existing-build` |
| `profiles/tos_hub.json` | `tos_hub` | `src/**`, `pubcode/**` | `build/**`, `build_*/**` | `C++` | `cmake-existing-build` |

> **说明**：Profile 中的 `target_id` 与 `configuration` 仅作为元数据追踪与审计批次标签，不代表对产品现场部署状态（Release/Debug）做出法定业务判定。若智港通构建负责人有确定的目标命名体系，可按需微调这两个字段。源码范围（`included_paths`）与排除规则（`excluded_paths`）建议保持预置以避免运行门禁错误。

## 使用示例

从仓库根目录执行：

```bash
# SECP 交付扫描
uv run --frozen --offline python scan/deliver.py \
  --profile profiles/secp.json \
  --scan-path /path/to/secp/src \
  --result-path /path/to/results/run-secp \
  --output /path/to/deliveries/secp-delivery.zip

# TOS-Hub 交付扫描
uv run --frozen --offline python scan/deliver.py \
  --profile profiles/tos_hub.json \
  --scan-path /path/to/tos-hub/src \
  --scan-path /path/to/tos-hub/pubcode \
  --result-path /path/to/results/run-tos_hub \
  --output /path/to/deliveries/tos_hub-delivery.zip
```
