# AutoCTP — 单进程双策略（价差 + 宽跨）

在同一 CTP 账户、**一条连接、一个进程**内运行：

- **价差 Call Spread**（逻辑来自 `D:\autotrade`）
- **宽跨 Long Strangle**（逻辑来自 `D:\autostraggle`）

不修改上述两个仓库的入口与核心代码，仅在本目录组装调度。

---

## 文档

| 文档 | 内容 |
|------|------|
| **[docs/GUIDE.md](docs/GUIDE.md)** | **完整说明**（含 §15.4 三种 halt 与执行路径设计约定） |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 开发、测试与提交规范 |
| [docs/CI.md](docs/CI.md) | GitHub Actions 与全量 CI secrets 配置 |
| [data/strangle_positions.example.csv](data/strangle_positions.example.csv) | 宽跨启动前持仓 CSV 示例 |
| [data/position_startup_ack.txt.example](data/position_startup_ack.txt.example) | 启动确认文件示例 |

---

## 快速启动

```powershell
cd D:\autoctp
pip install -r requirements.txt
pip install -r D:\autotrade\requirements.txt

$env:USER_ID = "账号"
$env:PASSWORD = "密码"

# 维护宽跨物理持仓（无仓可空表）
# notepad data\strangle_positions.csv

D:\autotrade\.venv\Scripts\python.exe merged_main.py
```

启动后按提示 **确认持仓**（终端 `yes` 或弹窗）。详见 [docs/GUIDE.md §8](docs/GUIDE.md#8-启动流程)。

---

## 核心约束（必读）

| 项 | 说明 |
|----|------|
| **禁止双进程** | 勿与 `auto_main.py`、`straggle_main.py` 同账户同时运行 |
| **全局 1 在途** | 两策略共用，不会互撤抢单 |
| **order_ref** | 价差 1～499999；宽跨 ≥500000 |
| **宽跨日限** | 买入权利金 `daily_buy_limit_yuan`；达限仍允许平仓 |
| **宽跨持仓** | 开盘前维护 `data/strangle_positions.csv` |
| **价差认领** | `data/spread_positions.csv` 或启动「CTP−宽跨」推导；**建仓/再平衡/平仓均认该账本** |

---

## 目录一览

```
merged_main.py          # 入口
merged_main_loop.py     # 主循环
merged_config.yaml      # 本地配置
tradeinfo/              # spread.csv + strangle.csv（或 tradeinfo.xlsx）
data/                   # strangle_positions.csv、ledger_strangle.json
docs/GUIDE.md           # 完整说明
```

---

## 三个程序怎么选

| 程序 | 用途 |
|------|------|
| `D:\autotrade\auto_main.py` | 仅价差 |
| `D:\autostraggle\straggle_main.py` | 仅宽跨 |
| **`D:\autoctp\merged_main.py`** | **价差 + 宽跨（推荐）** |
