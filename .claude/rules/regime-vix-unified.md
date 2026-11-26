---
description: 双策略共用品种整体 VIX（价差算法），合约流动性/DTE/选约仍按 tradeinfo 月
---

# 品种整体 VIX（有意设计）

> 与 `.cursor/rules/regime-vix-unified.mdc` 同步；修改时请一并更新。

> **更新（宽跨已改平价溢价率）**：宽跨建仓/平仓信号已从环境 VIX 改为
> **平价溢价率**（`rate = C_atm/F` 与 `coef·sqrt(t)·sigma` 比较，`sigma=min(HV1y,HV3y)`，
> 见 `autostraggle/strangle_hv.py` + `straggle_signals.py`，HV 数据 `tradeinfo/futures.xlsx`
> 由用户手动维护）。本规则的「价差/宽跨共用 VIX」现**仅适用于价差**；宽跨不再读环境
> VIX（`process_strangle_symbol` 仍接收 `round_vix_engine` 但忽略，签名兼容）。两策略信号源
> **有意分裂**：价差=VIX、宽跨=平价溢价率（HV），勿建议「合回统一 VIX」。下文 VIX 段落
> 仅对价差有效。

## 设计意图

- **VIX 只回答**：这个期货品种当前波动环境偏高还是偏低（粗门闸），**不追求**与某一档期权定价一致。
- **价差与宽跨共用同一套环境 VIX**，算法与 `autotrade.auto_vix.VIXEngine.calculate_vix` 一致（按 OI 选近月/次近月，可近次月插值到约 30 日）。
- **不要**为宽跨单独维护「仅按 tradeinfo 单月期权链」算 VIX 的旧路径；勿建议两套环境指标算法分裂。

## 两层分离（必须保持）

| 层次 | 数据来源 | 用途 |
|------|----------|------|
| **环境 VIX** | `calculate_vix(sym)`（OI 主力月，可与 tradeinfo 月不同） | 价差：VIX 高触发交易；宽跨：VIX 低允许建仓 / 高可波动率平仓 |
| **合约执行** | tradeinfo 的 `month` | DTE、`select_strangle_strikes`、流动性检查、实际下单合约 |

- 近月 OI ≠ tradeinfo 月时：环境 VIX 仍用主力月；选约/流动性/持仓认领**仍按 tradeinfo 月**。可打一次对照日志，不是数据缺失。
- 阈值仍各策略独立（如宽跨 `vol_basis×benchmark_multiplier`、价差 `VIX_TRIGGER_MULTIPLIER`），**只统一算法，不统一阈值语义**。

## 代码落点（修改时勿拆回）

- **宽跨**：`autostraggle/straggle_vix.py` → `calculate_vix_for_month` 委托 `engine.calculate_vix`；`month` 仅日志与主力月对照。
- **合并主循环**：`merged_main_loop` 宽跨扫描传入 `round_vix_engine`（与价差同 `wrap_vix_engine` + 每轮缓存）。
- **缓存键**：`merged_vix_cache.SPREAD_ROUND_VIX_CACHE_KEY`（`_spread_round_vix_cache`）；宽跨独立运行可读 `_round_vix_cache` 或同一合并键。

## 可算性（整体 vs 单月 tradeinfo 月）

- **整体 VIX 算不出 → 按 tradeinfo 单月几乎必然也算不出**：整体算法用 OI 主力近月（及次近月），已是该品种最容易凑齐期权链的月份；若仍不够，tradeinfo 指定月通常更稀、更偏，更难单独合成 σ。
- **反过来的推论不成立**：整体 VIX **能**算出，不代表 tradeinfo 月单月链也能算（建仓月可能是远月/非主力；整体看 608 有链，609 仍可能 `无有效双边报价` 或 DTE/选约失败）。
- 因此环境门闸以**整体 VIX**为准；单月只服务执行层，勿用「单月算不出」去推翻整体结论，也勿在整体已 None 时仍指望换单月算法救活环境判断。

## 勿误判

- 一边 `VIX无法计算`、另一边曾有单月 VIX，**不等于**行情只服务一侧；多半是旧「按月链」与 OI 近月算法差异，现已统一。
- 价差「触发交易」与宽跨「不建仓: VIX 高」日志方向相反，是**策略阈值语义**不同，不是 VIX 数据源不同。
