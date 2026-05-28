# AutoCTP — 单进程双策略 CTP 编排层

在同一 CTP 账户、**一条连接、一个进程**内调度：

- **价差 Call Spread**（策略实现位于 **私有** autotrade 仓库）
- **宽跨 Long Strangle**（策略实现位于 **私有** autostraggle 仓库）

本仓库 **不修改** 上述两库的核心策略代码，仅负责路径注入、主循环、对账、入账与 halt 路径选择。

> 若本仓公开：autotrade / autostraggle 请保持 **Private**。说明见 [docs/PUBLIC_REPO.md](docs/PUBLIC_REPO.md)。

---

## 文档

| 文档 | 内容 |
|------|------|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 双策略编排、halt 路径、认领模型（公开脱敏） |
| [docs/COMPAT.md](docs/COMPAT.md) | 三仓版本配套与 CI 说明 |
| [docs/UNATTENDED_CHECKLIST.md](docs/UNATTENDED_CHECKLIST.md) | 无人值守上线/巡检核对清单 |
| **`docs/LOCAL完整说明.md`** | 完整使用说明（**仅本地**，已 `.gitignore`，含 halt 与执行路径约定） |
| [docs/PUBLIC_REPO.md](docs/PUBLIC_REPO.md) | 公开本仓时的边界与自检 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 开发、测试与提交规范 |
| [docs/CI.md](docs/CI.md) | GitHub Actions 与私有依赖 clone |
| [merged_config.example.yaml](merged_config.example.yaml) | 本地配置模板 |
| [data/strangle_positions.example.csv](data/strangle_positions.example.csv) | 宽跨启动前持仓 CSV 示例 |

> clone 公开仓库后无 `LOCAL完整说明.md`；请在本机 `docs/` 下自行维护，勿提交。

---

## 快速启动（需本地 autotrade / autostraggle）

```powershell
# 路径示例 — 请改为本机目录，或用环境变量 AUTOTRADE_ROOT / AUTOSTRAGGLE_ROOT
$env:AUTOTRADE_ROOT = "C:\path\to\autotrade"
$env:AUTOSTRAGGLE_ROOT = "C:\path\to\autostraggle"

cd C:\path\to\autoctp
pip install -r requirements.txt
pip install -r "$env:AUTOTRADE_ROOT\requirements.txt"

# CTP 账号通常在 autotrade 的 auto_config.yaml 或环境变量中配置
$env:USER_ID = "你的CTP账号"
$env:PASSWORD = "你的密码"

# 复制模板：merged_config.example.yaml → merged_config.yaml
# 维护 data\strangle_positions.csv（无仓可空表）
# 开盘前：python scripts/preview_startup_data.py

python merged_main.py
```

**7×24 无人值守（模板默认）**

| 项 | 行为 |
|----|------|
| 启动确认 | **人工冷启动**弹窗三选一（含 **价差=CTP−宽跨并确认**）；**进程内自动重启**跳过 |
| 改 CSV/ledger 后 | 运行 `python scripts/invalidate_startup_ack.py` 再启动，或设 `startup_ack_require_today: true` |
| 无显示器服务器 | `startup_ack_use_gui: false`（模板已设）；首次可用 `$env:AUTOCTP_CONFIRM='yes'` 一次性确认 |
| fail-fast | 邻月守卫未安装、`target_months` 为空、`global_margin_limit=0` → **拒绝启动** |
| 飞书「暂停」 | **全停含平仓**；无人值守勿用暂停代替 halt，敞口需恢复后再扫 |

首次部署流程见本地 `docs/LOCAL完整说明.md` §8；模板见 [merged_config.example.yaml](merged_config.example.yaml)。

---

## 核心约束（必读）

| 项 | 说明 |
|----|------|
| **禁止双进程** | 勿与独立 `auto_main.py` / `straggle_main.py` 同账户同时运行 |
| **全局 1 在途** | 两策略共用，不会互撤抢单 |
| **order_ref 分段** | 价差与宽跨使用不同号段（见 `merged_config.example.yaml`） |
| **宽跨日限** | `daily_buy_limit_yuan`；达限仍允许平仓 |
| **宽跨持仓** | 开盘前维护 `data/strangle_positions.csv` |
| **价差认领** | `data/spread_positions.csv` 或启动 derive；建仓/再平衡/平仓均认该账本 |
| **7×24 默认** | 持久启动确认 + fail-fast 自检；改 CSV/ledger 运行 `invalidate_startup_ack.py` 重确认 |

---

## 目录一览

```
merged_main.py          # 入口
merged_main_loop.py     # 主循环
merged_config.yaml      # 本地配置（勿提交）
tradeinfo/              # 品种表（复制 *example* 后维护）
data/                   # 持仓 CSV、账本、确认文件
futuretrade/            # autotrade 执行统计（运行时，已 gitignore）
docs/LOCAL完整说明.md   # 本地完整说明（勿提交）
tests/                  # unit + integration（integration 需私有依赖库）
```

---

## 测试（无 autotrade 也可跑 unit）

```powershell
$env:AUTOCTP_ALLOW_MISSING_DEPS = '1'
python scripts/run_unit_tests.py
# 或一键: .\scripts\dev_check.ps1
```

CI 与私有库 clone 见 [docs/CI.md](docs/CI.md)。三仓版本见 [docs/COMPAT.md](docs/COMPAT.md)。

## 运维脚本（本地）

| 脚本 | 用途 |
|------|------|
| `scripts/dev_check.ps1` | ruff + 敏感文件检查 + unit 测试 |
| `scripts/preview_startup_data.py` | 开盘前离线检查 CSV/账本（不连 CTP） |
| `scripts/backup_data.ps1` | 备份 `data/` 运行时文件 |
| `scripts/invalidate_startup_ack.py` | 失效启动确认（可选 `--account-switch` 清理 journal/fill_ledger 并重置宽跨 runtime） |

---

## 三个程序怎么选

| 程序 | 用途 |
|------|------|
| autotrade 的 `auto_main.py` | 仅价差 |
| autostraggle 的 `straggle_main.py` | 仅宽跨 |
| **本仓 `merged_main.py`** | **价差 + 宽跨（同一账户推荐）** |
