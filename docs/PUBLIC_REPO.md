# 公开仓库说明

本仓库（**AutoCTP**）可在 GitHub 上 **Public**，用于展示双策略 CTP 编排、测试与 CI 工程化。

## 仓库边界

| 仓库 | 建议可见性 | 内容 |
|------|------------|------|
| **autoctp**（本仓） | Public 可选 | 单进程调度、对账、入账、halt 路径、测试 |
| **autotrade** | **Private** | 价差策略实现（选约、开平仓、再平衡等） |
| **autostraggle** | **Private** | 宽跨策略实现（信号、选 strike、执行等） |

公开本仓 **不会** 附带两个私有库的源码。他人 clone 本仓后：

- 可跑 **unit 测试**（`AUTOCTP_ALLOW_MISSING_DEPS=1`）
- **无法** 在 CI/本地跑全量 integration（除非自备 autotrade/autostraggle）

## 公开前自检

```powershell
python scripts/check_sensitive_files.py
git status   # 勿提交 merged_config.yaml、data/* 运行时文件、.cursor/
```

确认 git 历史中无 token、webhook、实盘配置（见 CONTRIBUTING.md）。

## 敏感信息

**不要提交：**

- `merged_config.yaml`、`.env`
- `docs/GUIDE.md`、`docs/LOCAL完整说明.md`（本地完整说明）
- `tradeinfo/*.csv`（非 example）
- `data/` 下运行时 CSV、jsonl、pid、ledger

**Actions secrets**（`DEPENDENCY_REPO_PAT`、私有库 URL）仅存在于 GitHub Settings，不会随 Public 泄露。

## 公开后仍会可见的内容

- 双策略 **编排** 与 **对账/halt** 设计（概要见 `README.md`、`.cursor/rules/`）
- OrderRef 分段、CSV 认领模型等 **架构约定**
- **不会** 直接暴露私有库内的完整 alpha 实现
- **不会** 包含 `docs/LOCAL完整说明.md` / `docs/GUIDE.md`（本地专用，已 `.gitignore`）

具体参数请以本地 `merged_config.yaml` 为准，勿把实盘数值写进文档或 example 模板。
