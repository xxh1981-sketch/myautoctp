# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## Project Overview

**AutoCTP** — single-process, single-CTP-connection orchestration for **Call Spread** (autotrade) + **Long Strangle** (autostraggle). Strategy logic lives in private sibling repos; this repo handles path injection, merged main loop, reconcile, ledger, and halt routing.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for halt paths and position claiming.

## Commands

```powershell
# Tests (no live CTP)
.\.venv\Scripts\python -m pytest tests/ -q

# Run merged dual-strategy loop
.\.venv\Scripts\python merged_main.py
```

Config: `merged_config.yaml` (from `merged_config.example.yaml`). Credentials via autotrade `auto_config.yaml` or `USER_ID` / `PASSWORD` env vars.

## AI project memory (Claude + Cursor)

Design rules and audit conclusions are **mirrored in three places** — keep them in sync when editing:

| Location | Role |
|----------|------|
| `docs/AI_PROJECT_MEMORY.md` | Human-readable combined reference |
| `.cursor/rules/*.mdc` | Cursor (`alwaysApply: true`) |
| `.claude/rules/*.md` | Claude Code project rules |

Imported modular rules:

@.claude/rules/dual-strategy-halt-semantics.md
@.claude/rules/regime-vix-unified.md
@.claude/rules/daily-trade-limit-preference.md
@.claude/rules/unattended-audit-no-change.md
@.claude/rules/unattended-observability.md

## Critical constraints (summary)

- **Never** run `auto_main.py` / `straggle_main.py` on the same account in parallel with `merged_main.py`.
- **Global 1 in-flight order** across both strategies — enforced at **send time** by autotrade `auto_risk.ensure_no_inflight` / `safe_send_order` (and strangle `pre_trade_inflight`), not by `merged_main_loop` serial scanning alone.
- **Feishu pause** = zero automated actions (no close-only, no cancel during pause loop).
- **Reconcile halt** → spread close-only; **daily limit / margin halt** → full `process_symbol` with open gated.
- **Unified regime VIX** for both strategies; execution month still from tradeinfo.

Details: imported rules above and `docs/AI_PROJECT_MEMORY.md`.
