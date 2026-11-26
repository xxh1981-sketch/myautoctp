---
description: AutoCTP 双策略 halt 语义、持仓认领与主循环设计约定（勿误判为 bug）
---

# AutoCTP 交易逻辑约定

> 与 `.cursor/rules/dual-strategy-halt-semantics.mdc` 同步；修改时请一并更新。

## 三种 halt，两条执行路径（有意设计）

| 类型 | 含义 | 价差执行路径 |
|------|------|--------------|
| **对账 halt** | CSV/CTP 不一致，持仓真相不可信 | **close-only**（`_spread_open_halted` → 仅平仓） |
| **日限 halt** | 账本可信，仅限制新增成交 | 完整 `process_symbol`；`remaining_limit=0` 禁新开 |
| **保证金 halt** | 账本可信，仅限制新增风险 | 完整 `process_symbol`；stage3/再平衡 B 腿内检查 |

- 对账 halt 走 close-only：误开/误再平衡风险 > 单腿残留风险。
- 日限/保证金走全路径：仍需 autotrade 完整平仓条件、冷却、VIX/DTE；不是「紧急全平」模式。
- **不要**建议把日限/保证金 halt 统一改成 close-only。

## spread_positions.csv 为空

- 空表 = **本策略不认领任何价差腿**，不能反推「CTP 上一定有价差仓」。
- CTP 上的 Call 可能来自宽跨、手工仓或其它策略；需启动人工确认 / derive / 对账 halt。
- 勿实现「CSV 空 + CTP 有仓 → 自动禁止开仓」类启发式（会误伤外部持仓场景）。

## daily_trade_limit

- **定位：防操作风险（runaway）**，不是核心仓位/保证金风控。主要防止程序异常（死循环、逻辑 bug、重复发单）导致**不断乱下单并成交**；是「成交笔数熔断器」，不是头寸规模设计工具。
- 粗节流，非核心风控；**核心风控**在 `daily_buy_limit_yuan`、`global_margin_limit`、对账 halt、A/B 比例。
- 可设较大（如 200～500）；达限仍扫平仓，仅禁新开（与 autotrade 单策略主循环不同，是改进）。
- 优先 `count_spread_filled_open_orders`；降级全账户口径时仅打 warning。
- **勿**因 `open_clip` 等「少笔数、多手数」优化而削弱或绕开日限语义；clip 改变的是单次挂单量，日限仍是异常场景下的最后一道**成交笔数**保险。

## 飞书暂停

- **人工全停**：主循环 skip，价差/宽跨均不扫（**含平仓、再平衡、任何自动下单**）。这是预期行为，不是缺陷。
- **设计意图**：暂停本身就是为了**控制风险**；在人工介入场景下，**误平仓也是风险**（滑点、时机错误、打断策略结构、与人工意图冲突）。因此飞书暂停 = **零自动风控动作**，不是「只停开仓、继续自动平仓」的半停模式。
- **审核/排障时勿误判**：不要将「暂停期间不扫平仓」标为缺口、残余风险或改进建议；**不要**建议在飞书暂停下仍跑 close-only、再平衡或任何自动平仓路径。
- **恢复交易**：由用户通过飞书取消暂停或手工/命令介入；暂停期间的市场暴露由人工承担，属有意权衡。

## 修改主循环 / spread_ledger_execution 时

- 保持对账 halt 与 日限/保证金 halt 的路径区分。
- journal 检查更新 `_journal_halt_open` 后须**同轮**调用 `_sync_strangle_open_halt`（与价差 `spread_open_ok` 对齐，勿仅依赖 reconcile/margin 复检）。
- 启动确认：对账有差异弹专用确认窗（`merged_startup_ack._prompt_reconcile_mismatch_ack`）。

## 价差平仓机制（特别设计，勿改）

`auto_closer_executor.execute_close_orders_with_limit` + `auto_closer.process_close` 的平仓流程是 autotrade 经过实盘打磨的设计，**不要**按"宽跨第二腿无限重试到对手价"的范式去对齐：

- `close_max_retry=3`、urgent/normal 两套 `close_B_initial_offset` + `close_B_ladder_step` 梯子、失败后 30 分钟 cooldown，是有意权衡（控制滑点 / 防止价格踩踏 / 留时间给行情恢复）。
- `process_close` 失败时返回 True 触发 cooldown 是 H8 修复的正式行为（防止"刚发完平仓单又立刻开仓"）。
- **不要**仿宽跨 `close_chp_pending` 给价差平仓加"无限重试到对价"队列；不要建议把 `close_B_initial_offset` 抬高到接近 1.0；不要把 cooldown 缩短。
- 真要"必须成交"的极端场景，由用户手工/飞书命令介入，而非自动逼到对手价。

## 宽跨平仓机制（与价差不同，保留无限重试）

`StrangleExecutor.execute_close` + `close_chp_pending` 队列 + 主循环每轮 rebalance 的设计是"宽跨第二腿必须配齐"的硬约束（残留单腿 = 裸期权风险），允许 offset 梯子到 0（对手价）+ 永久重试，**不要**对齐成"3 次失败后冷却"的价差范式。

## 同品种同月与策略仓位逻辑隔离（不是问题）

- **同品种同月被两策略同时覆盖是正常、受支持的场景**，不是配置缺陷；**不要**建议启动时硬拒（如 `reject_overlap_same_month`）或要求错月才能跑双策略。
- **逻辑隔离靠认领维护，不靠错月**：`spread_positions.csv`（signed）+ `strangle_positions.csv`（多头）+ OrderRef 号段自动入账 + 对账互扣；各策略建仓/再平衡/平仓只读自己的账本。
- **CTP 持仓按 `PosiDirection` 分行**（多头 `2`、空头 `3` 各一条），同一合约可同时存在多、空两行；**不要**假设 CTP 会把同合约轧成一条净持仓导致对账必挂。
- **同一 strike 的 Call 两边都可能持**：宽跨 long + 价差 short（B 腿）或多头手数分摊，在对账公式下可自洽（宽跨侧扣 spread 多头 Call；价差侧 signed 比对）。
- **严格按程序运行、无人工 CSV 记账错误、无程序外改同一合约持仓时，仓位逻辑独立做得到**；对账 halt 是 CSV 与 CTP 不一致时的安全网，不是「同月双策略不可行」的证据。
- **勿将**「同覆盖品种」banner warning **误解为必须修复的配置错误**；它仅提醒 OrderRef + CSV 纪律，不是禁止同月。

## 主循环不变量与审查清单

完整 halt 路由、组合原子性、Review 问句见 **`docs/INVARIANTS_REVIEW_CHECKLIST.md`**。本节保留 halt 语义详述；改主循环时用该清单逐项 review。
