# 贡献指南

## 开发环境

```powershell
cd D:\autoctp
pip install -r requirements.txt
pip install -r requirements-dev.txt
pre-commit install   # 可选，提交前自动 ruff + 敏感文件检查
```

本地无 `D:\autotrade` 时，跑本仓独立单测：

```powershell
$env:AUTOCTP_ALLOW_MISSING_DEPS = '1'
python scripts/run_unit_tests.py
python scripts/run_unit_tests.py --cov=. --cov-report=term-missing
```

## 测试结构

| 类型 | 标记 | 运行方式 |
|------|------|----------|
| unit | `@pytest.mark.unit` | `python scripts/run_unit_tests.py` |
| integration | `@pytest.mark.integration` | 需 autotrade；`pytest -m integration` |

`tests/autotrade_stubs.py` 为 CI 提供最小 autotrade 模块 stub，使 `margin_check`、`spread_reconcile` 等核心逻辑可在无 `D:\autotrade` 时测试。

## 测试

- **unit**：不依赖 autotrade / autostraggle，带 `@pytest.mark.unit`（见 `scripts/run_unit_tests.py`）。
- **integration**：依赖外部仓库，带 `@pytest.mark.integration`；本地有 autotrade 时 `pytest -m integration`。

```powershell
$env:AUTOCTP_ALLOW_MISSING_DEPS = '1'
python scripts/run_unit_tests.py
pytest tests/ -m "not integration" -q        # 与 CI permissive 相同
pytest tests/ -m integration -q              # 本地有 autotrade 时
pytest tests/ -q                             # 全量
pytest tests/ --cov=. --cov-report=term-missing
ruff check .
python scripts/check_sensitive_files.py
```

CI 全量测试需在仓库 Settings → Secrets 配置 `AUTOTRADE_REPO_URL`、`AUTOSTRAGGLE_REPO_URL`（私有库用 PAT）。详见 [docs/CI.md](docs/CI.md)。

本地检查：

```powershell
python scripts/check_ci_readiness.py
```

## 提交前检查

**不要**提交以下内容：

- `merged_config.yaml`、`.env`（账号密码）
- `data/*.csv`、`data/*.jsonl`、`data/*.pid`（运行时账本/流水）

## 修改交易逻辑时

请先阅读 `docs/GUIDE.md` §15.4 与 `.cursor/rules/dual-strategy-halt-semantics.mdc`：

- 对账 halt → 价差 close-only
- 日限 / 保证金 halt → 完整 `process_symbol` 路径
- 勿统一 halt 语义，勿改价差 3 次平仓冷却设计
