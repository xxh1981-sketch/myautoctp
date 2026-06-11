---
description: 无人值守可观测性增强（配置 drift / 报平安 enriched / halt 解除通知）
---

# 无人值守可观测性（已实现）

> 与 `.cursor/rules/unattended-observability.mdc` 及 `docs/AI_PROJECT_MEMORY.md` §5 同步。

以下三项为**纯可观测性**：不改变 halt / 交易 / 飞书暂停语义；失败仅日志。

## 1. merged_config.yaml drift 告警

- 主循环启动时记录 `merged_config.yaml` mtime 基准（`init_config_drift_baseline`）。
- 每 `config_drift_check_interval_sec`（默认 600s）检查磁盘 mtime；若晚于基准 → 飞书提醒「已变更但未重启」。
- 同一 mtime 只告警一次；`config_drift_alert_enabled: false` 可禁用。

## 2. 每日报平安 enriched

- 每轮完整扫描结束后 `stash_daily_heartbeat_metrics` 写入 `_daily_heartbeat_metrics`（价差日限、宽跨买入、保证金占用、未配对腿）。
- 报平安在轮首发送，展示**上一轮**缓存指标 + 当前 halt 标志位。
- 保证金占用来自 `margin_check` 成功查询时的 `_last_margin_total`。

## 3. halt 解除飞书通知

- 对 `margin / journal / spread_reconcile / strangle_reconcile` 四类 halt 跟踪 True→False。
- 同轮批量发送 `💚 AutoCTP halt 已解除`；`halt_recovery_alert_cooldown_sec`（默认 300s）节流。
- **仅通知**，不自动改 halt 或恢复交易；飞书暂停期间若 halt 变化，恢复后下轮完整路径仍会通知。

## 修改时

- 保持与 `runtime_risk_alerts` 一致：告警不改变交易语义。
- 勿在 drift / halt 恢复路径引入 `cancel_all_pending_orders` 或自动开仓/平仓。
