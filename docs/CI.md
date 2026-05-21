# CI 配置说明

AutoCTP 的 GitHub Actions 分为 **unit**（必过）与 **full**（需 secrets）两层。

## Workflow 概览

| Job | 触发 | 依赖 | 说明 |
|-----|------|------|------|
| `lint` | push / PR | 无 | ruff + 敏感文件检查 |
| `pytest-unit` | push / PR | 无 autotrade | `python scripts/run_unit_tests.py` |
| `pytest-unit-windows` | push / PR | 无 autotrade | Windows 上跑同一 unit 套件 |
| `pytest-full` | push / PR / 手动 | autotrade（必选）；autostraggle（可选） | 全量或 autotrade-only integration |

## 启用全量 CI（pytest-full）

1. 打开 GitHub 仓库 **Settings → Secrets and variables → Actions**
2. 添加 Repository secrets：

| Secret | 必填 | 值示例 | 说明 |
|--------|------|--------|------|
| `AUTOTRADE_REPO_URL` | **是** | `https://github.com/your-org/autotrade.git` | 不含 token 的普通 HTTPS URL 即可 |
| `DEPENDENCY_REPO_PAT` | 私有 autotrade 时 | `ghp_xxxx` | workflow 自动注入 clone 认证 |
| `AUTOSTRAGGLE_REPO_URL` | **否** | `https://github.com/your-org/autostraggle.git` | 未建库时可不配；CI 只跑 autotrade integration |

**尚未创建 autostraggle 仓库时**：只配 `AUTOTRADE_REPO_URL` + `DEPENDENCY_REPO_PAT` 即可。`pytest-full` 会 clone autotrade，跑 `pytest -m "not autostraggle"`（跳过宽跨主循环 / `StrangleLedger` 等 5 个文件的用例）。autostraggle 建好后补上 `AUTOSTRAGGLE_REPO_URL`，即切换为全量 `pytest tests/`。

**私有库推荐做法**（三选一，优先第 1 种）：

1. URL secrets 保持 `https://github.com/...`，另加 **`DEPENDENCY_REPO_PAT`**（Classic PAT 勾选 `repo`，或 fine-grained 对两库只读）。
2. 把 token 写进 URL：`https://x-access-token:TOKEN@github.com/org/autotrade.git`（可不再配 `DEPENDENCY_REPO_PAT`）。
3. 依赖库为 **public** 时，仅 URL secrets 即可，无需 PAT。

若 clone 报 `could not read Username for 'https://github.com'`，说明私有库未带认证——补 `DEPENDENCY_REPO_PAT` 后重新跑 workflow。

3. 配置完成后，每次 push/PR 会自动 clone 两仓库并跑全量测试；**失败会阻塞 PR**（已移除 `continue-on-error`）。

4. 建议在 **Settings → Branches → Branch protection** 中将以下 checks 设为 Required：

   **现在（无 autostraggle 仓库）：**
   - `Tests / lint`
   - `Tests / pytest-unit`
   - `Tests / pytest-unit-windows`
   - `Tests / pytest-full`（配好 autotrade secrets 后）

   **autostraggle 建库并配 `AUTOSTRAGGLE_REPO_URL` 后**：同一批 checks 即覆盖全量双策略测试。

   > GitHub 显示格式为 **`Tests / {job 名}`**。若某 job 显示 **Skipped**，常见原因是上游 job 失败（旧版 workflow 中 pytest-full 依赖 pytest-unit，unit 红则 full 整 job 被跳过；现已改为仅依赖 lint）。

## pytest-full 显示 Skipped 的两种情况

| 现象 | 原因 |
|------|------|
| 整 job 灰色 Skipped | 旧 workflow：`needs: [pytest-unit]` 且 unit 失败；或 lint 失败 |
| job 跑了但只剩 notice | 未配 `AUTOTRADE_REPO_URL`，clone/测试步骤被跳过 |
| job 绿但日志有 autostraggle notice | 正常：未配 `AUTOSTRAGGLE_REPO_URL`，跑 autotrade-only 子集 |

推送最新 `.github/workflows/test.yml` 后，**pytest-full 会在 lint 通过后执行**（不再等 unit 绿）。

**pytest-full 不安装 `openctp_ctp`**（GitHub Actions 无 CTP 运行时，import 原生库会 `Aborted`）；测试通过 `tests/openctp_stubs.py` 注入 stub 模块。

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

典型日志：

```text
fatal: could not read Username for 'https://github.com': No such device or address
```

**处理**：在 Actions secrets 新增 **`DEPENDENCY_REPO_PAT`**（PAT 需能读 autotrade / autostraggle），保留现有 URL 为普通 `https://github.com/...` 即可。

或把 token 嵌入 URL：`https://x-access-token:ghp_xxxx@github.com/org/autotrade.git`

PAT 创建：**GitHub → Settings → Developer settings → Personal access tokens**（Classic 勾 `repo`，或 Fine-grained 对两库 Read）。

clone 失败时 job 为**红**，check 仍会出现；修 secrets 后重新跑，全绿再勾 Required。

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
