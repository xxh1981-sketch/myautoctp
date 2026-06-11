---
description: 无人值守审核结论——有意设计/已知取舍，勿当缺陷反复建议修改
---

# 无人值守审核：确定不改项

> 与 `.cursor/rules/unattended-audit-no-change.mdc` 同步；修改时请一并更新。

以下经代码审核讨论后**有意保留**；后续审查、排障、改进建议时**不要**标为缺口或反复提议改掉（除非用户明确要求）。

## 飞书暂停

- 暂停期间**不**在循环内 `cancel_all_pending_orders`；撤单仅在进程 `finally`（退出）执行。
- 暂停 = **零自动动作**（含不扫平仓、不撤单）。误平仓/误撤单风险 > 挂单暴露。
- 勿建议「暂停时仍撤单/仍 close-only」。

## 启动 guard 补丁

- 原子保存 / 月白名单 / CTP 三补丁：共享 `fail_fast_on_guard_install`（不拆 per-patch 开关）。
- **周末抑制补丁**安装失败仅 warning（非致命，退回周末照常重连的噪音行为）。
- 勿建议把周末补丁改成 fail-fast，或拆成多个 fail-fast 开关（除非用户主动要）。

## 数据写入取舍

- `fill_ledger.append_fill_row`：**非原子追加**，注释已说明（性能 vs 完全原子性）；fill_ledger 是分析日志非持仓真相。
- 报平安 marker：写用 `atomic_write_text`，读用 `open`+`strip`；半行旧文件边缘场景可接受，勿大改读写路径。

## 日志与告警

- `LogNoiseFilter._seen` 超 4096 时整表 `clear()`：极端场景才触发，代价是可接受 polish，非可靠性缺陷。
- `feishu_alert_cooldown._state_bucket` 在 `conn` 无 `_runtime_state` 且 `config=None` 时返回空 dict：主路径走不到，勿当一级 bug。
- `round_slow_warn_sec` 默认 60s：**运维调参**（日盘品种多时可调到 120–180s），勿在无人值守审查中强推改代码默认值。

## 配置与兼容

- `compat_lock_enforce: false` 为日常默认；重大发布前用户可**临时**设 true，勿建议永久改 true。
- `daily_trade_limit=100` 对本账户足够（见 `daily-trade-limit-preference` 规则）。
- `margin_unknown_streak`：halt 决策与 `record_margin_check_result` 分函数是**有注释+单测的约定**；非现网 bug，勿为「架构洁癖」无需求重构。

## 测试范围

- 不强制补：`install_feishu_noise_patch` 幂等单测、三 halt 叠加 E2E，除非用户点名要测。

## 已实现（勿重复建议「再加一遍」）

- 单品种扫描超时：`max_symbol_scan_sec` + `symbol_scan_timeout.py`（价差/宽跨）；超时后后台线程可能仍跑，属 ThreadPool 固有限制。
- `merged_config.example.yaml` 的 `log_noise` 与 `MERGED_TOP_LEVEL_DEFAULTS` 对齐。
- housekeeping 磁盘空间预检：`disk_space_check_enabled` / `disk_space_warn_mb`。
- 无人值守可观测性三项（配置 drift / 报平安 enriched / halt 解除通知）：见 `unattended-observability` 规则，勿重复建议「再加一遍」。
