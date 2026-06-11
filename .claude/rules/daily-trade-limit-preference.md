---
description: 价差 daily_trade_limit 实盘偏好（勿建议提高到 200～500）
---

# daily_trade_limit 实盘偏好

> 与 `.cursor/rules/daily-trade-limit-preference.mdc` 同步；修改时请一并更新。

- 本账户 **`daily_trade_limit=100`（继承 autotrade `auto_config.yaml`）已足够**，勿在无人值守/配置审查中建议提高到 200～500。
- **原因（有意）**：一天内能**同时**满足 VIX/流动性/价差/DTE 等交易条件的品种并不多；100 笔成交笔数熔断仍远大于典型可开仓规模，且 `open_clip` 改变的是单次挂单量，不是日限语义。
- 日限定位仍是 **操作风险阀（防 runaway）**，非核心保证金/头寸风控；核心限额在 `global_margin_limit`、`daily_buy_limit_yuan`、对账 halt 等。
- 若用户主动要求调整日限，再按其实盘成交频率讨论；默认保持 100。
