# 贡献指南

> 本仓可公开；策略实现仓库（autotrade / autostraggle）请保持私有。见 [docs/PUBLIC_REPO.md](docs/PUBLIC_REPO.md)。

## 开发环境

```powershell
cd C:\path\to\autoctp
pip install -r requirements-ci.txt
pip install -r requirements-dev.txt
pre-commit install   # 可选，提交前自动 ruff + 敏感文件检查
```

本地无 autotrade 源码时，跑本仓独立单测：

```powershell
$env:AUTOCTP_ALLOW_MISSING_DEPS = '1'
python scripts/run_unit_tests.py
python scripts/run_unit_tests.py --cov=. --cov-report=term-missing
# Windows 一键（与 CI unit + lint 对齐）:
.\scripts\dev_check.ps1
```

架构与 halt 约定见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)；三仓版本见 [docs/COMPAT.md](docs/COMPAT.md)。

## 测试结构

| 类型 | 标记 | 运行方式 |
|------|------|----------|
| unit | `@pytest.mark.unit` | `python scripts/run_unit_tests.py` |
| integration | `@pytest.mark.integration` | 需 autotrade；`pytest -m integration` |

**unit 与 integration 边界**

- **unit**（`scripts/run_unit_tests.py` 清单内）：无私有库即可跑；`tests/autotrade_stubs.py` 注入最小 `auto_*` / `straggle_*` stub。
- **integration**（清单外、或 `@pytest.mark.integration`）：需 `AUTOTRADE_ROOT`；宽跨相关另需 `AUTOSTRAGGLE_ROOT` 或 CI secrets。
- 成对文件：`test_spread_reconcile_unit.py`（unit） vs `test_spread_reconcile.py`（integration 全量对账）；`test_spread_derive_unit.py` vs `test_spread_derive.py` 等同理。
- 勿对 `tests/` 全目录跑 `pytest` 而无 `AUTOCTP_ALLOW_MISSING_DEPS=1` 且无 autotrade——会误 collect integration 文件。

`tests/autotrade_stubs.py` 为 CI 提供最小 autotrade 模块 stub，使部分核心逻辑可在无 autotrade 源码时测试。

## 测试

- **unit**：不依赖 autotrade / autostraggle，带 `@pytest.mark.unit`（见 `scripts/run_unit_tests.py`）。
- **integration**：依赖外部仓库，带 `@pytest.mark.integration`；本地有 autotrade 时 `pytest -m integration`。

```powershell
$env:AUTOCTP_ALLOW_MISSING_DEPS = '1'
python scripts/run_unit_tests.py
pytest tests/ -m integration -q              # 本地有 autotrade 时（勿用 -m "not integration" 扫全目录，会误 import integration 文件）
pytest tests/ -q                             # 全量
pytest tests/ --cov=. --cov-report=term-missing
ruff check .
python scripts/check_sensitive_files.py
```

CI 全量测试需在仓库 Settings → Secrets 配置私有库 URL 与 PAT（勿写入文档或代码）。详见 [docs/CI.md](docs/CI.md)。

本地检查：

```powershell
python scripts/check_ci_readiness.py --check-unit-manifest
python scripts/preview_startup_data.py   # 开盘前 CSV/账本（需 merged_config.yaml）
```

`scripts/run_unit_tests.py` 现含 **38** 个测试文件（主循环重连/ halt、启动确认、入账、OrderRef、飞书等）；依赖 autotrade 的用例仍在 `pytest-full`。

## 提交前检查

**不要**提交以下内容：

- `merged_config.yaml`、`.env`（账号密码）
- `data/*.csv`、`data/*.jsonl`、`data/*.pid`（运行时账本/流水）

## 修改交易逻辑时

请先阅读本地 `docs/LOCAL完整说明.md` §15.4 与 `.cursor/rules/dual-strategy-halt-semantics.mdc`：

- 对账 halt → 价差 close-only
- 日限 / 保证金 halt → 完整 `process_symbol` 路径
- 勿统一 halt 语义，勿改价差 3 次平仓冷却设计
