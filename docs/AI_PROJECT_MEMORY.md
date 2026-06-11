# AutoCTP AI 项目记忆（Claude Code + Cursor 共用）

本文档汇总编排层**有意设计**与**审核确定不改项**。修改时请同步更新：

- `.cursor/rules/*.mdc`（Cursor，`alwaysApply: true`）
- `.claude/rules/*.md`（Claude Code）
- 本文件

---

## 1. AutoCTP 交易逻辑约定

（对应 `dual-strategy-halt-semantics`）

### 三种 halt，两条执行路径（有意设计）

| 类型 | 含义 | 价差执行路径 |
|------|------|--------------|
| **对账 halt** | CSV/CTP 不一致，持仓真相不可信 | **close-only**（`_spread_open_halted` → 仅平仓） |
| **日限 halt** | 账本可信，仅限制新增成交 | 完整 `process_symbol`；`remaining_limit=0` 禁新开 |
| **保证金 halt** | 账本可信，仅限制新增风险 | 完整 `process_symbol`；stage3/再平衡 B 腿内检查 |

- 对账 halt 走 close-only：误开/误再平衡风险 > 单腿残留风险。
- 日限/保证金走全路径：仍需 autotrade 完整平仓条件、冷却、VIX/DTE；不是「紧急全平」模式。
- **不要**建议把日限/保证金 halt 统一改成 close-only。

### spread_positions.csv 为空

- 空表 = **本策略不认领任何价差腿**，不能反推「CTP 上一定有价差仓」。
- CTP 上的 Call 可能来自宽跨、手工仓或其它策略；需启动人工确认 / derive / 对账 halt。
- 勿实现「CSV 空 + CTP 有仓 → 自动禁止开仓」类启发式（会误伤外部持仓场景）。

### daily_trade_limit

- **定位：防操作风险（runaway）**，不是核心仓位/保证金风控。
- 粗节流，非核心风控；**核心风控**在 `daily_buy_limit_yuan`、`global_margin_limit`、对账 halt、A/B 比例。
- 优先 `count_spread_filled_open_orders`；降级全账户口径时仅打 warning。
- **勿**因 `open_clip` 等优化而削弱或绕开日限语义。

### 飞书暂停

- **人工全停**：主循环 skip，价差/宽跨均不扫（**含平仓、再平衡、任何自动下单**）。
- 飞书暂停 = **零自动风控动作**；勿建议暂停下仍 close-only 或自动撤单。

### 修改主循环 / spread_ledger_execution 时

- 保持对账 halt 与 日限/保证金 halt 的路径区分。
- journal 检查更新 `_journal_halt_open` 后须**同轮**调用 `_sync_strangle_open_halt`。
- 启动确认：对账有差异弹专用确认窗（`merged_startup_ack._prompt_reconcile_mismatch_ack`）。

### 价差平仓机制（特别设计，勿改）

- `close_max_retry=3`、梯子 + 30 分钟 cooldown 是有意权衡。
- **不要**仿宽跨无限重试到对手价；不要缩短 cooldown 或抬高 offset 到接近 1.0。

### 宽跨平仓机制（与价差不同）

- `close_chp_pending` + 永久重试到对手价是硬约束（单腿 = 裸期权风险）。

### 同品种同月与策略仓位逻辑隔离

- 同品种同月双策略覆盖是**正常、受支持**的；逻辑隔离靠认领 CSV + OrderRef 号段，不靠错月。
- CTP 持仓按 `PosiDirection` 分行；勿假设同合约轧成净持仓。

---

## 2. 品种整体 VIX（价差/宽跨共用）

（对应 `regime-vix-unified`）

- **环境 VIX**：`calculate_vix(sym)`（OI 主力月）— 粗门闸，两策略共用算法。
- **合约执行**：tradeinfo 的 `month` — DTE、选约、流动性、下单。
- **不要**为宽跨单独维护「仅按 tradeinfo 月链」算 VIX 的旧路径。
- 整体 VIX 算不出 → tradeinfo 月几乎也算不出；整体能算出不保证 tradeinfo 月能算。
- 价差「触发交易」与宽跨「不建仓: VIX 高」方向相反是**阈值语义**不同，不是数据源不同。

代码落点：`straggle_vix.calculate_vix_for_month` 委托 `engine.calculate_vix`；`merged_main_loop` 用 `wrap_vix_engine` + 每轮缓存。

---

## 3. daily_trade_limit 实盘偏好

（对应 `daily-trade-limit-preference`）

- 本账户 **`daily_trade_limit=100` 已足够**；勿在审查中建议提高到 200～500。
- 若用户主动要求调整，再按实盘讨论；默认保持 100。

---

## 4. 无人值守审核：确定不改项

（对应 `unattended-audit-no-change`）

### 飞书暂停

- 循环内不 `cancel_all_pending_orders`；撤单仅在进程 `finally`。
- 勿建议暂停时仍撤单 / close-only。

### 启动 guard 补丁

- 原子保存 / 月白名单 / CTP 三补丁共享 `fail_fast_on_guard_install`。
- 周末抑制补丁失败仅 warning。

### 数据写入取舍

- `fill_ledger.append_fill_row` 非原子追加（分析日志，有意取舍）。
- 报平安 marker 读写方式勿大改。

### 日志与告警

- `LogNoiseFilter._seen` 超 4096 整表 clear：极端场景，可接受。
- `round_slow_warn_sec` 默认 60s 为运维调参，勿强推改代码默认。

### 配置与兼容

- `compat_lock_enforce: false` 日常默认；重大发布前可临时 true。
- `margin_unknown_streak` 跨函数约定有注释+单测，非现网 bug。

### 测试范围

- 不强制补 noise patch 幂等单测、三 halt E2E，除非用户点名。

### 已实现（勿重复建议）

- 单品种扫描超时：`max_symbol_scan_sec` + `symbol_scan_timeout.py`。
- `merged_config.example.yaml` 的 `log_noise` 与 defaults 对齐。
- housekeeping 磁盘空间预检：`disk_space_check_enabled` / `disk_space_warn_mb`。
- 无人值守可观测性三项：配置 drift 告警、报平安 enriched、halt 解除飞书（见 `unattended-observability` 规则 §5）。

---

## 5. 无人值守可观测性（已实现）

（对应 `unattended-observability`）

### merged_config.yaml drift 告警

- 启动记录 `merged_config.yaml` mtime；每 `config_drift_check_interval_sec`（默认 600s）检查；磁盘新于基准 → 飞书「已变更未重启」。
- 纯可观测性；`config_drift_alert_enabled: false` 可关。

### 每日报平安 enriched

- `stash_daily_heartbeat_metrics` 每轮扫描后缓存：价差日限、宽跨买入、保证金占用/限额、未配对腿。
- 报平安轮首发送，用上一轮缓存 + 当前 halt 位。

### halt 解除通知

- 跟踪 margin / journal / spread_reconcile / strangle_reconcile 的 True→False。
- 批量飞书 `💚 halt 已解除`；`halt_recovery_alert_cooldown_sec` 节流；**仅通知**。
