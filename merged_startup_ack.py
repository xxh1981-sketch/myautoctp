"""启动前核对宽跨持仓账本，终端确认或确认文件后再交易。"""

import os
import sys
import time
from datetime import date
from typing import Optional


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, '').strip().lower() in ('1', 'yes', 'true', 'y', 'ok')

from import_strangle_positions import sync_strangle_leg_claims


def _ack_path(config: dict) -> str:
    return config.get('dual_strategy', {}).get(
        'startup_ack_file',
        os.path.join(os.path.dirname(__file__), 'data/position_startup_ack.txt'),
    )


def _ack_valid(text: str) -> bool:
    line = (text or '').strip().lower()
    return line.startswith('confirmed') or line in ('ok', 'yes', 'y')


def _ack_date(text: str):
    line = (text or '').strip()
    parts = line.split()
    if len(parts) >= 2 and parts[0].lower() == 'confirmed':
        try:
            from datetime import date as _date
            return _date.fromisoformat(parts[1])
        except ValueError:
            return None
    return None


def _save_ack_file(ack_file: str) -> None:
    os.makedirs(os.path.dirname(ack_file) or '.', exist_ok=True)
    with open(ack_file, 'w', encoding='utf-8') as f:
        f.write(f'confirmed {date.today().isoformat()}\n')


def _file_ack_ok(config: dict, require_today: bool = False) -> bool:
    ack_file = _ack_path(config)
    if not os.path.isfile(ack_file):
        return False
    with open(ack_file, 'r', encoding='utf-8') as f:
        text = f.read()
    if not _ack_valid(text):
        return False
    if require_today:
        ack_day = _ack_date(text)
        if ack_day is not None and ack_day != date.today():
            return False
    return True


def _env_auto_confirm() -> bool:
    return os.environ.get('AUTOCTP_CONFIRM', '').strip().lower() in (
        'yes', 'y', '1', 'true', 'ok',
    )


def format_spread_claims_summary(store) -> str:
    lines = ['【价差认领 CSV】']
    if store is None:
        lines.append('  (未加载 spread leg claims)')
        return '\n'.join(lines)
    claims = store.list_leg_claims()
    if not claims:
        lines.append('  无认领持仓')
    else:
        for inst, vol in sorted(claims.items()):
            side = '多' if vol > 0 else '空'
            lines.append(f"  {inst} {side} x{abs(vol)}")
    return '\n'.join(lines)


def format_ledger_summary(ledger) -> str:
    lines = ['【宽跨账本】']
    positions = ledger.list_positions()
    claims = ledger.list_leg_claims() if hasattr(ledger, 'list_leg_claims') else {}
    unmatched = ledger.list_unmatched_legs()
    if not positions and not claims and not unmatched:
        lines.append('  无持仓')
    for inst, vol in sorted(claims.items()):
        lines.append(f"  {inst} x{vol}")
    for p in positions:
        lines.append(
            f"  {p.get('symbol')}/{p.get('month')} {p.get('status')} "
            f"C{p.get('call_strike')}/P{p.get('put_strike')} x{p.get('groups', 1)}"
        )
    for u in unmatched:
        leg = u.get('leg') or {}
        lines.append(
            f"  [未配对] {u.get('symbol')}/{u.get('month')} "
            f"{leg.get('inst', u.get('filled_instrument', '?'))}"
        )
    lines.append(f"  当日买入累计: {ledger.get_daily_buy_amount():.0f} 元")
    return '\n'.join(lines)


def _wait_conn_ready(conn, logger, timeout: float = 20.0) -> bool:
    """等待重连隔离结束、双通道就绪后再查持仓。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if (
            conn.td_logined and conn.md_logined and conn.td_api
            and not getattr(conn, '_reconnect_quarantine', False)
        ):
            return True
        time.sleep(0.3)
    ready = (
        conn.td_logined and conn.md_logined and conn.td_api
        and not getattr(conn, '_reconnect_quarantine', False)
    )
    if not ready:
        logger.warning('[启动] CTP 尚未完全就绪，持仓预览可能不完整')
    return ready


def _position_volume(pos: dict) -> int:
    for key in ('position', 'Position', 'volume', 'Volume'):
        val = pos.get(key)
        if val is not None:
            try:
                return int(val)
            except (TypeError, ValueError):
                continue
    return 0


def _format_ctp_positions_preview(conn, config: dict, logger) -> str:
    lines = ['【CTP 持仓预览】']
    _wait_conn_ready(conn, logger)

    try:
        positions = conn.query_positions_sync(timeout=10, use_cache=False)
    except TypeError:
        positions = conn.query_positions_sync(timeout=10)
    except Exception as e:
        lines.append(f'  查询失败: {e}')
        return '\n'.join(lines)
    if positions is None:
        lines.append('  查询失败（可能仍在重连隔离期，稍后会再校准）')
        return '\n'.join(lines)

    shown = 0
    for pos in positions:
        vol = _position_volume(pos)
        if vol <= 0:
            continue
        inst = (pos.get('instrument') or pos.get('InstrumentID') or '').strip()
        if not inst:
            continue
        direction = pos.get('direction') or pos.get('PosiDirection') or ''
        dir_label = {'2': '多', '3': '空', 2: '多', 3: '空'}.get(direction, str(direction))
        lines.append(f"  {inst} {dir_label} x{vol}")
        shown += 1
        if shown >= 30:
            lines.append('  ...')
            break
    if shown == 0:
        lines.append('  (无持仓)')
    lines.append('  宽跨请对照 strangle_positions.csv；价差请对照 spread_positions.csv')
    return '\n'.join(lines)


_DERIVE_SPREAD_LABEL = '价差=CTP−宽跨并确认'
_DERIVE_SPREAD_HELP = (
    '以当前 CTP 持仓减去宽跨账本认领，写入 spread_positions.csv，'
    '并视为已确认宽跨账本无误。'
)


def _preview_reconcile(conn, ledger, config: dict, logger) -> tuple:
    """Return (halt, issues, log_lines)."""
    lines = []
    try:
        from strangle_reconcile_dual import reconcile_strangle_positions_dual
        from straggle_reconcile import reconcile_strangle_positions

        symbols = {it['future'].lower() for it in config.get('strangle_tradeinfo', [])}
        spread_info = config.get('spread_tradeinfo') or []
        dual = config.get('dual_strategy') or {}
        if dual.get('exclude_spread_from_strangle_reconcile', True):
            halt, issues = reconcile_strangle_positions_dual(
                conn, ledger, symbols, spread_info, logger, config=config,
            )
        else:
            halt, issues = reconcile_strangle_positions(
                conn, ledger, symbols, logger, config=config,
            )
        for msg in issues[:10]:
            lines.append(f'[对账预览] {msg}')
            if logger:
                logger.warning(f'[对账预览] {msg}')
        if halt and logger:
            logger.warning('对账预览不一致，宽跨可能 open_halted 禁止新开')
        return halt, issues, lines
    except Exception as e:
        msg = f'[对账预览] {e}'
        if logger:
            logger.warning(msg)
        return False, [str(e)], [msg]


def _build_startup_summary(ledger, conn) -> str:
    ledger_summary = format_ledger_summary(ledger)
    spread_summary = ''
    if conn:
        from spread_ledger import store_from_conn
        spread_summary = format_spread_claims_summary(store_from_conn(conn))
    return ledger_summary, spread_summary


def _should_prefer_gui(dual: dict) -> bool:
    if not dual.get('startup_ack_use_gui', True):
        return False
    if dual.get('startup_ack_force_terminal', False):
        return False
    if dual.get('startup_ack_prefer_gui', True):
        return True
    return not sys.stdin.isatty()


def _confirm_via_gui_choice(title: str, message: str, logger=None) -> Optional[str]:
    """
    GUI 三选一：yes | derive | no
    Returns None if GUI unavailable (caller may fall back to terminal).
    """
    try:
        import tkinter as tk
        from tkinter import scrolledtext

        choice = {'action': 'no'}

        root = tk.Tk()
        root.title(title)
        root.minsize(760, 540)
        try:
            root.attributes('-topmost', True)
        except Exception:
            pass

        txt = scrolledtext.ScrolledText(root, width=88, height=24, wrap=tk.WORD)
        txt.pack(padx=12, pady=(12, 8), fill=tk.BOTH, expand=True)
        txt.insert('1.0', message)
        txt.config(state=tk.DISABLED)

        hint = tk.Label(
            root,
            text=_DERIVE_SPREAD_HELP,
            justify=tk.LEFT,
            wraplength=700,
        )
        hint.pack(padx=12, pady=(0, 8))

        btn_row = tk.Frame(root)
        btn_row.pack(pady=(0, 14))

        def _done(action: str) -> None:
            choice['action'] = action
            try:
                root.quit()
            except Exception:
                pass
            root.destroy()

        tk.Button(btn_row, text='确认启动', width=12, command=lambda: _done('yes')).pack(
            side=tk.LEFT, padx=6,
        )
        tk.Button(
            btn_row,
            text=_DERIVE_SPREAD_LABEL,
            width=24,
            command=lambda: _done('derive'),
        ).pack(side=tk.LEFT, padx=6)
        tk.Button(btn_row, text='取消', width=10, command=lambda: _done('no')).pack(
            side=tk.LEFT, padx=6,
        )

        root.protocol('WM_DELETE_WINDOW', lambda: _done('no'))
        root.lift()
        root.focus_force()
        root.update_idletasks()
        root.mainloop()
        return choice['action']
    except Exception as e:
        if logger:
            logger.warning(f'[启动] GUI 确认框失败: {e}', exc_info=True)
        return None


def _prompt_interactive_ack(
    config: dict,
    logger,
    ack_file: str,
    summary: str,
    conn=None,
    ledger=None,
) -> str:
    dual = config.get('dual_strategy') or {}
    if not dual.get('startup_ack_interactive', True):
        return 'no'

    use_gui = dual.get('startup_ack_use_gui', True)
    prompt_text = (
        '请对照 CTP 持仓与 strangle_positions.csv / spread_positions.csv。\n'
        '  yes    → 确认无误并开始交易\n'
        f'  derive → {_DERIVE_SPREAD_LABEL}（确认宽跨账本）\n'
        '  no     → 取消'
    )
    gui_message = summary.strip() + '\n\n' + prompt_text

    if _should_prefer_gui(dual):
        logger.info('[启动] 弹出确认对话框…')
        action = _confirm_via_gui_choice('AutoCTP 启动确认', gui_message, logger=logger)
        if action is not None:
            if action == 'yes':
                return 'yes'
            if action == 'derive':
                return 'derive'
            config['_startup_ack_retry'] = False
            return 'no'
        logger.warning('[启动] GUI 不可用，尝试终端输入')

    if sys.stdin.isatty():
        logger.info(
            '\n' + '=' * 60 + '\n'
            '请核对上方持仓摘要。\n'
            '  yes    → 开始交易\n'
            f'  derive → {_DERIVE_SPREAD_LABEL}\n'
            '  no     → 取消\n'
            + '=' * 60
        )
        try:
            ans = input('确认启动? [yes/derive/no]: ').strip().lower()
            if ans in ('yes', 'y', 'ok', 'confirmed'):
                return 'yes'
            if ans in ('derive', 'spread', 'ctp', 'auto', 'd'):
                return 'derive'
            if ans in ('no', 'n', 'q', 'quit', 'exit'):
                config['_startup_ack_retry'] = False
                return 'no'
            logger.warning(f'[启动] 未识别输入 "{ans}"，视为取消')
            config['_startup_ack_retry'] = False
            return 'no'
        except (EOFError, KeyboardInterrupt):
            pass

    if use_gui and not _should_prefer_gui(dual):
        logger.info('[启动] 弹出确认对话框…')
        action = _confirm_via_gui_choice('AutoCTP 启动确认', gui_message, logger=logger)
        if action == 'yes':
            return 'yes'
        if action == 'derive':
            return 'derive'
        if action is not None:
            logger.info('[启动] 用户取消（对话框）')
            config['_startup_ack_retry'] = False
            return 'no'

    logger.info('[启动] 无法交互确认（请设 startup_ack_use_gui: true 或用 PowerShell 运行）')
    return 'no'


def _persist_ack(dual: dict, ack_file: str, logger, config: dict = None) -> bool:
    persist = dual.get('startup_ack_persist', False)
    if dual.get('startup_ack_each_run', True):
        persist = False
    if persist:
        _save_ack_file(ack_file)
        logger.info(f"[启动] 已确认，记录到 {ack_file}")
    else:
        logger.info('[启动] 已确认（本次有效，下次启动仍会询问）')
    if config is not None:
        config['_startup_ack_done'] = True
    return True


def _is_auto_restart(config: dict) -> bool:
    """进程内异常/自动重试外层循环，或显式 AUTOCTP_AUTO_RESTART / --auto-restart。"""
    if config.get('_auto_restart'):
        return True
    if _env_truthy('AUTOCTP_AUTO_RESTART') or '--auto-restart' in sys.argv:
        return True
    return False


def _skip_interactive_ack(config: dict, logger) -> bool:
    """异常/自动重启：不弹持仓确认框（人工每次启动仍须确认）。"""
    if not _is_auto_restart(config):
        return False
    if config.get('_startup_ack_done'):
        logger.info('[启动] 自动重启，沿用进程内已确认，跳过持仓对话框')
    else:
        logger.info('[启动] 自动重启，跳过持仓确认对话框')
    config['_startup_ack_done'] = True
    return True


def require_startup_position_ack(config: dict, logger, ledger, conn=None) -> bool:
    dual = config.get('dual_strategy') or {}
    if not dual.get('require_startup_ack', True):
        logger.info("[启动] 已跳过持仓核对（require_startup_ack=false）")
        return True

    ack_file = _ack_path(config)
    each_run = dual.get('startup_ack_each_run', True)
    if not each_run and _file_ack_ok(config, require_today=True):
        logger.info(f"[启动] 持仓已确认: {ack_file}")
        config['_startup_ack_done'] = True
        return True

    if _env_auto_confirm():
        _save_ack_file(ack_file)
        logger.info('[启动] AUTOCTP_CONFIRM=yes，已自动确认')
        config['_startup_ack_done'] = True
        return True

    if _skip_interactive_ack(config, logger):
        return True

    logger.info("=" * 60)
    logger.info("启动前请核对持仓")
    logger.info("宽跨: data/strangle_positions.csv（程序运行中自动维护，启动时人工核对）")
    logger.info("价差: data/spread_positions.csv（signed volume：正=多，负=空）")
    ledger_summary, spread_summary = _build_startup_summary(ledger, conn)
    logger.info(ledger_summary)
    if spread_summary:
        logger.info(spread_summary)

    ctp_summary = ''
    if conn:
        ctp_summary = _format_ctp_positions_preview(conn, config, logger)
        logger.info(ctp_summary)
        _preview_reconcile(conn, ledger, config, logger)

    summary = ledger_summary + '\n' + spread_summary + '\n' + ctp_summary
    action = _prompt_interactive_ack(
        config, logger, ack_file, summary, conn=conn, ledger=ledger,
    )
    if action == 'derive':
        if conn is None or ledger is None:
            logger.error('[启动] 无法推导价差认领：缺少 CTP 连接或宽跨账本')
            return False
        from spread_derive import apply_derived_spread_from_ctp
        from spread_ledger import store_from_conn

        store = store_from_conn(conn)
        claims = apply_derived_spread_from_ctp(conn, ledger, store, config, logger)
        if claims is None:
            logger.error('[启动] 价差认领推导失败，未进入交易')
            return False
        logger.info('[启动] 已确认宽跨账本，并按 CTP−宽跨 更新价差认领')
        logger.info(format_spread_claims_summary(store_from_conn(conn)))

        logger.info('=' * 60)
        logger.info('[启动] 推导后再次对账预览')
        halt, issues, _ = _preview_reconcile(conn, ledger, config, logger)
        if halt:
            logger.warning(
                '[启动] 推导后宽跨对账仍有差异（可能为手工/external 仓），'
                '宽跨新开可能 open_halted；价差认领已更新'
            )
        elif not issues:
            logger.info('[启动] 推导后宽跨对账通过')
        return _persist_ack(dual, ack_file, logger, config)

    if action == 'yes':
        return _persist_ack(dual, ack_file, logger, config)

    logger.error("=" * 60)
    logger.error("未完成持仓确认，程序不进入交易。")
    logger.error(f"也可创建 {ack_file}，内容: confirmed {date.today().isoformat()}")
    logger.error("=" * 60)
    return False
