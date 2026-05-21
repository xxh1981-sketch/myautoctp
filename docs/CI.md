# CI 配置说明

AutoCTP 的 GitHub Actions 分为 **unit**（必过）与 **full**（需 secrets）两层。

## Workflow 概览

| Job | 触发 | 依赖 | 说明 |
|-----|------|------|------|
| `lint` | push / PR | 无 | ruff + 敏感文件检查 |
| `pytest-unit` | push / PR | 无 autotrade | `python scripts/run_unit_tests.py` |
| `pytest-unit-windows` | push / PR | 无 autotrade | Windows 上跑同一 unit 套件 |
| `pytest-permissive` | push / PR | 无 autotrade | `pytest -m "not integration"` |
| `pytest-full` | push / PR / 手动 | autotrade + autostraggle | 全量 `pytest tests/` |

## 启用全量 CI（pytest-full）

1. 打开 GitHub 仓库 **Settings → Secrets and variables → Actions**
2. 添加 Repository secrets：

| Secret | 值示例 |
|--------|--------|
| `AUTOTRADE_REPO_URL` | `https://github.com/your-org/autotrade.git` |
| `AUTOSTRAGGLE_REPO_URL` | `https://github.com/your-org/autostraggle.git` |

私有仓库需在 URL 中嵌入 PAT，或使用 `https://x-access-token:TOKEN@github.com/...` 形式。

3. 配置完成后，每次 push/PR 会自动 clone 两仓库并跑全量测试；**失败会阻塞 PR**（已移除 `continue-on-error`）。

4. 建议在 **Settings → Branches → Branch protection**（或 **Settings → Rules → Rulesets**）中将以下 checks 设为 Required：
   - `Tests / lint`
   - `Tests / pytest-unit`
   - `Tests / pytest-full`（全量跑通后）

   > GitHub 显示格式为 **`{workflow 名} / {job 名}`**。本仓库 workflow 名为 `Tests`（见 `test.yml` 首行 `name: Tests`），因此不是单独的 `pytest-full`，而是 **`Tests / pytest-full`**。

## Branch protection 里找不到 pytest-full？

常见原因与处理：

### 1. Workflow 还没推到 GitHub（最常见）

Branch protection **只会列出已经在 Actions 里跑成功过的 check**。若本地 `.github/workflows/test.yml` 尚未 `git push`，远程从未执行过 `pytest-full`，下拉里就不会有这个选项。

**处理：**

```powershell
git add .github/workflows/test.yml docs/CI.md scripts/ ...
git commit -m "Add CI workflow and unit tests"
git push origin main   # 或 master，与默认分支一致
```

推送后打开 **Actions** 页，确认出现 **Tests** workflow 且 **pytest-full** job 为绿色。

### 2. 搜索名称不对

在 **Require status checks** 搜索框里试：

- `Tests / pytest-full`
- `pytest-full`
- `Tests`

不要只搜 `pytest-full` 若列表为空，先确认 Actions 里是否已有完成记录。

### 3. 手动触发一次

**Actions → Tests → Run workflow**（需 workflow 已含 `workflow_dispatch` 并已 push）。

secrets 配好后，日志里应能看到 **Clone autotrade & autostraggle** 与 **Run full suite** 步骤，而不是只有 *Full suite skipped hint*。

### 4. secrets 已配但 job 仍跳过

若 Actions 日志出现 `Skipping full suite: secrets ... not set`：

- 确认 secret 名称**完全一致**：`AUTOTRADE_REPO_URL`、`AUTOSTRAGGLE_REPO_URL`（区分大小写）
- 确认是 **Repository secrets**，不是 Environment secrets（除非 workflow 绑定了 environment）
- 改 secrets 后需 **重新跑一次** workflow（不会自动重跑旧 commit）

### 5. 私有依赖仓库 clone 失败

secrets 里的 URL 若指向私有库，需带 PAT，例如：

`https://x-access-token:ghp_xxxx@github.com/org/autotrade.git`

此时 job 会**失败**（红），check 仍会出现，可设为 Required；若 clone 失败需先修 URL 再勾必选。

### 6. 新旧 GitHub 界面

| 界面 | 路径 |
|------|------|
| 经典 | **Settings → Branches → Add rule / Edit** → Require status checks |
| Rulesets | **Settings → Rules → New ruleset** → Require status checks |

两者都只在 check **至少成功运行过一次** 后才会出现在列表中（有时需等几分钟刷新）。

## 手动触发全量测试

Actions 页选择 **Tests** workflow → **Run workflow**（`workflow_dispatch`）。

## 本地对应命令

```powershell
# unit（与 CI 一致）
$env:AUTOCTP_ALLOW_MISSING_DEPS = '1'
python scripts/run_unit_tests.py

# integration（需 D:\autotrade）
pytest tests/ -m integration -q

# 全量
pytest tests/ -q
```

## 未配置 secrets 时

`pytest-full` job 会跳过 clone 与测试步骤，job 仍显示 **成功**（仅执行 secrets 检测）。这不会阻塞 PR，但也不会跑 integration 用例。

检查本地是否已配置 secrets 可用：

```powershell
python scripts/check_ci_readiness.py
```
