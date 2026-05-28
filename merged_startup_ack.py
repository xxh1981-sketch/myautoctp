"""启动前核对宽跨持仓账本，终端确认或确认文件后再交易。"""

import os
import sys
import time
from datetime import date
from typing import Optional

from atomic_io import atomic_write_text_with_newline
from env_utils import argv_has, env_truthy



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
    atomic_write_text_with_newline(ack_file, f'confirmed {date.today().isoformat()}')


def _save_ack_with_fingerprint(config: dict, ack_file: str) -> None:
    _save_ack_file(ack_file)
    try:
        from startup_ack_fingerprint import save_startup_ack_fingerprint

        save_startup_ack_fingerprint(config)
    except Exception:
        pass


def _read_ack_text(ack_file: str) -> str:
    try:
        with open(ack_file, 'r', encoding='utf-8') as f:
            return f.read()
    except OSError:
        return ''


def _file_ack_ok(config: dict, require_today: bool = False) -> bool:
    ack_file = _ack_path(config)
    if not os.path.isfile(ack_file):
        return False
    text = _read_ack_text(ack_file)
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


def _finish_unattended_startup_ack(
    config: dict, logger, ledger, conn=None,
) -> bool:
    """
    After skipping interactive steps (ack file / AUTOCTP_CONFIRM / auto-restart):
    restore external JSON with live CTP verification when possible.
    """
    from account_decomposition import restore_external_ack_from_file

    if not restore_external_ack_from_file(config, conn, ledger, logger):
        return False
    config['_startup_ack_done'] = True
    return True


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
    """Return (halt, issues, log_lines). Includes strangle + spread reconcile preview."""
    lines = []
    all_issues: list = []
    halt = False
    try:
        positions = None
        try:
            positions = conn.query_positions_sync(timeout=10, use_cache=False)
        except TypeError:
            positions = conn.query_positions_sync(timeout=10)

        from strangle_reconcile_dual import reconcile_strangle_positions_dual
        from straggle_reconcile import reconcile_strangle_positions

        symbols = {it['future'].lower() for it in config.get('strangle_tradeinfo', [])}
        spread_info = config.get('spread_tradeinfo') or []
        dual = config.get('dual_strategy') or {}
        if dual.get('exclude_spread_from_strangle_reconcile', True):
            str_halt, str_issues = reconcile_strangle_positions_dual(
                conn, ledger, symbols, spread_info, logger, config=config,
                positions=positions,
            )
        else:
            str_halt, str_issues = reconcile_strangle_positions(
                conn, ledger, symbols, logger, config=config,
            )
        all_issues.extend(str_issues or [])
        halt = halt or str_halt
        for msg in (str_issues or [])[:10]:
            lines.append(f'[对账预览·宽跨] {msg}')
            if logger:
                logger.warning(f'[对账预览·宽跨] {msg}')

        if dual.get('spread_execution_from_ledger', True):
            from spread_reconcile import reconcile_spread_positions

            sp_halt, sp_issues = reconcile_spread_positions(
                conn, spread_info, logger, config=config, positions=positions,
            )
            all_issues.extend(sp_issues or [])
            halt = halt or sp_halt
            for msg in (sp_issues or [])[:10]:
                lines.append(f'[对账预览·价差] {msg}')
                if logger:
                    logger.warning(f'[对账预览·价差] {msg}')

        if halt and logger:
            logger.warning(
                '对账预览不一致：宽跨可能 open_halted；价差可能 reconcile halt'
            )
        elif not all_issues and logger:
            logger.info('[对账预览] 宽跨与价差均无差异')
        return halt, all_issues, lines
    except Exception as e:
        msg = f'[对账预览] {e}'
        if logger:
            logger.warning(msg)
        return False, [str(e)], [msg]


def _build_startup_summary(ledger, conn, config=None) -> tuple:
    ledger_summary = format_ledger_summary(ledger)
    spread_summary = ''
    audit_summary = ''
    if conn:
        from spread_ledger import store_from_conn
        from spread_claims_guard import audit_spread_claims, format_spread_claims_audit

        store = store_from_conn(conn)
        spread_summary = format_spread_claims_summary(store)
        cfg = config or getattr(conn, 'config', None) or {}
        claims = store.list_leg_claims() if store else {}
        ctp_signed = None
        try:
            from spread_derive import query_ctp_signed_positions
            ctp_signed = query_ctp_signed_positions(conn, logger=None)
        except Exception:
            pass
        issues = audit_spread_claims(
            claims,
            cfg.get('spread_tradeinfo') or [],
            conn=conn,
            ctp_signed=ctp_signed,
        )
        audit_summary = format_spread_claims_audit(issues)
    return ledger_summary, spread_summary, audit_summary


def _should_prefer_gui(dual: dict) -> bool:
    if not dual.get('startup_ack_use_gui', True):
        return False
    if dual.get('startup_ack_force_terminal', False):
        return False
    if dual.get('startup_ack_prefer_gui', True):
        return True
    return not sys.stdin.isatty()


def _confirm_via_gui_yes_no(
    title: str,
    message: str,
    yes_text: str = '仍要启动',
    no_text: str = '取消',
    logger=None,
) -> Optional[bool]:
    """GUI 二选一。Returns True/False, or None if GUI unavailable."""
    try:
        import tkinter as tk
        from tkinter import scrolledtext

        choice = {'ok': False}

        root = tk.Tk()
        root.title(title)
        root.minsize(760, 520)
        try:
            root.attributes('-topmost', True)
        except Exception:
            pass

        txt = scrolledtext.ScrolledText(root, width=88, height=22, wrap=tk.WORD)
        txt.pack(padx=12, pady=(12, 8), fill=tk.BOTH, expand=True)
        txt.insert('1.0', message)
        txt.config(state=tk.DISABLED)

        btn_row = tk.Frame(root)
        btn_row.pack(pady=(0, 14))

        def _done(ok: bool) -> None:
            choice['ok'] = ok
            try:
                root.quit()
            except Exception:
                pass
            root.destroy()

        tk.Button(btn_row, text=yes_text, width=12, command=lambda: _done(True)).pack(
            side=tk.LEFT, padx=6,
        )
        tk.Button(btn_row, text=no_text, width=10, command=lambda: _done(False)).pack(
            side=tk.LEFT, padx=6,
        )

        root.protocol('WM_DELETE_WINDOW', lambda: _done(False))
        root.lift()
        root.focus_force()
        root.update_idletasks()
        root.mainloop()
        return choice['ok']
    except Exception as e:
        if logger:
            logger.warning(f'[启动] GUI 确认框失败: {e}', exc_info=True)
        return None


def _format_reconcile_issues(issues: list) -> str:
    if not issues:
        return '  (无明细)'
    lines = []
    for msg in issues[:15]:
        lines.append(f'  • {msg}')
    if len(issues) > 15:
        lines.append(f'  ... 共 {len(issues)} 条')
    return '\n'.join(lines)


def _reconcile_mismatch_auto_ok(config: dict, logger) -> bool:
    """自动化场景跳过对账差异确认。"""
    dual = config.get('dual_strategy') or {}
    if dual.get('allow_start_on_reconcile_mismatch', False):
        return True
    if _skip_interactive_ack(config, logger):
        return True
    return False


def _prompt_ledger_reconcile_step(
    config: dict,
    logger,
    summary: str,
    issues: list,
    reconcile_halt: bool = False,
) -> str:
    """
    冷启动第一步：账本核对 / derive（无论对账预览有无差异均弹出）。
    返回 yes | derive | no。
    """
    has_issues = bool(issues) or reconcile_halt
    if has_issues:
        header = (
            '【步骤 1/2 账本核对】对账预览存在差异。\n'
            '确认无误后进入账户分解；或使用 derive 从 CTP−宽跨 更新价差 CSV。\n'
            '继续后宽跨可能 open_halted 禁止新开（平仓仍允许）。\n'
        )
    else:
        header = (
            '【步骤 1/2 账本核对】对账预览无差异。\n'
            '请核对宽跨/价差账本；可直接确认进入下一步，或使用 derive 更新价差 CSV。\n'
        )
    return _prompt_reconcile_mismatch_ack(
        config, logger, summary, issues,
        context='账本核对',
        allow_derive=True,
        header=header,
    )


def _prompt_ctp_query_failed_ack(config: dict, logger, summary: str) -> str:
    """步骤 2：CTP 查询失败时的确认。返回 yes | no。"""
    dual = config.get('dual_strategy') or {}
    if not dual.get('startup_ack_interactive', True):
        logger.error('[启动] CTP 查询失败且非交互模式，拒绝启动')
        return 'no'
    if _reconcile_mismatch_auto_ok(config, logger):
        return 'yes'

    prompt = (
        '【步骤 2/2 账户分解】CTP 持仓查询失败，无法校验 CTP=价差+宽跨。\n\n'
        '  yes → 仍要启动（不校验账户分解）\n'
        '  no  → 取消，请检查连接后重试\n'
    )
    message = summary.strip() + '\n\n' + prompt
    title = 'AutoCTP 账户分解（查询失败）'
    use_gui = dual.get('startup_ack_use_gui', True)

    if use_gui and _should_prefer_gui(dual):
        ok = _confirm_via_gui_yes_no(
            title, message,
            yes_text='仍要启动',
            no_text='取消',
            logger=logger,
        )
        if ok is True:
            return 'yes'
        if ok is False:
            config['_startup_ack_retry'] = False
            return 'no'

    if sys.stdin.isatty():
        logger.warning('[启动] CTP 查询失败，请终端确认')
        logger.info(message)
        try:
            ans = input('CTP 查询失败，仍要启动? [yes/no]: ').strip().lower()
            if ans in ('yes', 'y', 'ok', 'confirmed'):
                return 'yes'
            config['_startup_ack_retry'] = False
            return 'no'
        except (EOFError, KeyboardInterrupt):
            pass

    if use_gui:
        ok = _confirm_via_gui_yes_no(title, message, logger=logger)
        if ok is True:
            return 'yes'
        if ok is False:
            config['_startup_ack_retry'] = False
            return 'no'

    logger.error('[启动] CTP 查询失败且无法交互确认，拒绝启动')
    config['_startup_ack_retry'] = False
    return 'no'


def _prompt_account_decomposition_ack(
    config: dict,
    logger,
    summary: str,
    decomp_issues: list,
) -> str:
    """
    冷启动第二步：CTP = 价差 + 宽跨 存在外部差额时的确认。
    返回 yes | no。
    """
    dual = config.get('dual_strategy') or {}
    if not dual.get('startup_ack_interactive', True):
        logger.error('[启动] 账户分解不一致且非交互模式，拒绝启动')
        return 'no'
    if _reconcile_mismatch_auto_ok(config, logger):
        return 'yes'

    issue_block = _format_reconcile_issues(decomp_issues)
    prompt = (
        '【步骤 2/2 账户分解】CTP ≠ 价差 + 宽跨（存在外部差额）。\n\n'
        f'{issue_block}\n\n'
        '外部差额通常表示同一账户上还有其他策略/手工持仓。\n'
        '确认启动后：本程序仅按价差/宽跨各自账本运行，不会管理外部仓；\n'
        '周期对账对「与已确认外部差额一致」的 CTP 超前差异不再 halt。\n\n'
        '  yes → 已知外部仓，进入交易\n'
        '  no  → 取消\n'
    )
    message = summary.strip() + '\n\n' + prompt
    title = 'AutoCTP 账户分解确认'

    use_gui = dual.get('startup_ack_use_gui', True)
    if use_gui and _should_prefer_gui(dual):
        logger.info('[启动] 账户分解有外部差额，弹出确认对话框…')
        ok = _confirm_via_gui_yes_no(
            title, message,
            yes_text='已知外部仓，启动',
            no_text='取消',
            logger=logger,
        )
        if ok is True:
            return 'yes'
        if ok is False:
            config['_startup_ack_retry'] = False
            return 'no'

    if sys.stdin.isatty():
        logger.warning('[启动] 账户分解存在外部差额，请终端确认')
        logger.info(message)
        try:
            ans = input('存在外部仓差额，仍要启动? [yes/no]: ').strip().lower()
            if ans in ('yes', 'y', 'ok', 'confirmed'):
                return 'yes'
            config['_startup_ack_retry'] = False
            return 'no'
        except (EOFError, KeyboardInterrupt):
            pass

    if use_gui:
        ok = _confirm_via_gui_yes_no(
            title, message,
            yes_text='已知外部仓，启动',
            no_text='取消',
            logger=logger,
        )
        if ok is True:
            return 'yes'
        if ok is False:
            config['_startup_ack_retry'] = False
            return 'no'

    logger.error('[启动] 账户分解不一致且无法交互确认，拒绝启动')
    config['_startup_ack_retry'] = False
    return 'no'


def _prompt_reconcile_mismatch_ack(
    config: dict,
    logger,
    summary: str,
    issues: list,
    context: str = '启动前对账',
    allow_derive: bool = True,
    header: str = None,
) -> str:
    """
    对账有差异时弹出确认（GUI 优先）。
    返回 yes | derive | no。
    """
    dual = config.get('dual_strategy') or {}
    if not dual.get('startup_ack_interactive', True):
        logger.error(f'[启动] {context}不一致且非交互模式，拒绝启动')
        return 'no'
    if _reconcile_mismatch_auto_ok(config, logger):
        return 'yes'

    issue_block = _format_reconcile_issues(issues)
    has_issues = bool(issues)
    if header:
        prompt = header
        if issues:
            prompt += f'\n{issue_block}\n'
    elif issues:
        prompt = (
            f'⚠ 【{context}差异】\n\n'
            f'{issue_block}\n\n'
            '继续启动后宽跨可能 open_halted 禁止新开（平仓仍允许）。\n'
        )
    else:
        prompt = f'【{context}】对账预览无差异。\n'
    if allow_derive:
        prompt += (
            '  确认 → 进入下一步\n'
            f'  derive → {_DERIVE_SPREAD_LABEL}\n'
            '  取消 → 不启动\n'
        )
    else:
        prompt += '  yes → 仍要启动    no → 取消\n'

    message = summary.strip() + '\n\n' + prompt
    title = (
        f'AutoCTP {context}差异确认'
        if has_issues else f'AutoCTP {context}'
    )

    use_gui = dual.get('startup_ack_use_gui', True)
    if use_gui and (_should_prefer_gui(dual) or allow_derive):
        log_hint = '对账有差异' if has_issues else '账本核对'
        logger.info(f'[启动] {log_hint}，弹出确认对话框 ({context})…')
        if allow_derive:
            action = _confirm_via_gui_choice(title, message, logger=logger)
            if action is not None:
                if action == 'no':
                    config['_startup_ack_retry'] = False
                return action
        else:
            ok = _confirm_via_gui_yes_no(title, message, logger=logger)
            if ok is True:
                return 'yes'
            if ok is False:
                config['_startup_ack_retry'] = False
                return 'no'

    if sys.stdin.isatty():
        if has_issues:
            logger.warning(f'[启动] {context}存在差异，请终端确认')
        else:
            logger.info(f'[启动] {context}，请终端确认')
        logger.info(message)
        try:
            if allow_derive:
                q = (
                    '对账有差异，仍要启动? [yes/derive/no]: '
                    if has_issues else '确认进入下一步? [yes/derive/no]: '
                )
                ans = input(q).strip().lower()
                if ans in ('yes', 'y', 'ok', 'confirmed'):
                    return 'yes'
                if ans in ('derive', 'spread', 'ctp', 'auto', 'd'):
                    return 'derive'
            else:
                ans = input('对账有差异，仍要启动? [yes/no]: ').strip().lower()
                if ans in ('yes', 'y', 'ok', 'confirmed'):
                    return 'yes'
            config['_startup_ack_retry'] = False
            return 'no'
        except (EOFError, KeyboardInterrupt):
            pass

    if use_gui:
        log_hint = '对账有差异' if has_issues else '账本核对'
        logger.info(f'[启动] {log_hint}，弹出确认对话框 ({context})…')
        if allow_derive:
            action = _confirm_via_gui_choice(title, message, logger=logger)
            if action is not None:
                if action == 'no':
                    config['_startup_ack_retry'] = False
                return action
        else:
            ok = _confirm_via_gui_yes_no(title, message, logger=logger)
            if ok is True:
                return 'yes'
            if ok is False:
                config['_startup_ack_retry'] = False
                return 'no'

    logger.error('[启动] 无法交互确认，拒绝启动')
    config['_startup_ack_retry'] = False
    return 'no'


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
    persist = dual.get('startup_ack_persist', True)
    if dual.get('startup_ack_each_run', False):
        persist = False
    if persist:
        _save_ack_with_fingerprint(config, ack_file)
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
    if env_truthy('AUTOCTP_AUTO_RESTART') or argv_has('--auto-restart'):
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


def _build_full_startup_summary(
    ledger, conn, config, ctp_summary='', reconcile_lines=None,
) -> str:
    ledger_summary, spread_summary, audit_summary = _build_startup_summary(
        ledger, conn, config,
    )
    parts = [ledger_summary, spread_summary]
    if audit_summary:
        parts.append(audit_summary)
    if ctp_summary:
        parts.append(ctp_summary)
    if reconcile_lines:
        parts.append('\n'.join(reconcile_lines))
    return '\n'.join(parts)


def _run_account_decomposition_step(
    config: dict,
    logger,
    ledger,
    conn,
) -> bool:
    """Step 2: CTP = spread + strangle (+ external). Returns True to proceed."""
    from account_decomposition import (
        compute_account_decomposition,
        format_account_decomposition_summary,
        register_acknowledged_external,
    )
    from spread_ledger import store_from_conn

    store = store_from_conn(conn)
    decomp = compute_account_decomposition(conn, ledger, store, config, logger)
    decomp_text = format_account_decomposition_summary(decomp)
    logger.info(decomp_text)

    if decomp.get('query_failed'):
        summary = _build_full_startup_summary(ledger, conn, config) + '\n' + decomp_text
        action = _prompt_ctp_query_failed_ack(config, logger, summary)
        if action == 'yes':
            logger.warning('[启动] 用户选择在 CTP 查询失败后仍启动（未校验账户分解）')
        return action == 'yes'

    if decomp.get('balanced'):
        register_acknowledged_external(config, {}, conn)
        logger.info('[启动] 账户分解一致（CTP = 价差 + 宽跨），进入交易')
        return True

    summary = _build_full_startup_summary(ledger, conn, config) + '\n' + decomp_text
    action = _prompt_account_decomposition_ack(
        config, logger, summary, decomp.get('issues') or [],
    )
    if action == 'yes':
        from account_decomposition import external_ack_path

        ext = decomp.get('external') or {}
        register_acknowledged_external(config, ext, conn)
        logger.warning(
            '[启动] 用户已确认外部仓差额；运行中各策略仅按自身账本执行，'
            f'已登记 {len(ext)} 个外部合约'
        )
        if ext:
            logger.info(
                f'[启动] 外部仓已持久化到 {external_ack_path(config)} '
                '(改 CSV/外部持仓后请运行 scripts/invalidate_startup_ack.py 再冷启动)'
            )
        return True
    return False


def require_startup_position_ack(config: dict, logger, ledger, conn=None) -> bool:
    dual = config.get('dual_strategy') or {}
    if not dual.get('require_startup_ack', True):
        logger.info("[启动] 已跳过持仓核对（require_startup_ack=false）")
        return True

    ack_file = _ack_path(config)
    each_run = dual.get('startup_ack_each_run', False)
    require_today = dual.get('startup_ack_require_today', False)
    manual_start = config.get('_manual_start', True)
    # 持久 ack 文件仅用于跳过「进程内自动重启」的确认；人工冷启动仍须交互核对账本。
    if (
        not each_run
        and not manual_start
        and _file_ack_ok(config, require_today=require_today)
    ):
        if dual.get('startup_ack_track_ledger_files', True):
            from account_decomposition import external_ack_path
            from startup_ack_fingerprint import check_startup_ack_fingerprint

            fp_ok, fp_reasons = check_startup_ack_fingerprint(config)
            if not fp_ok:
                logger.error(
                    '[启动] 持久确认与当前账本不一致，禁止无人值守跳过确认。'
                    f'原因: {"; ".join(fp_reasons)}'
                )
                logger.error(
                    '[启动] 请人工冷启动重新确认，或执行: '
                    'python scripts/invalidate_startup_ack.py'
                )
                logger.error(
                    '[启动] 相关文件: %s , %s',
                    ack_file,
                    external_ack_path(config),
                )
                return False
        ack_day = _ack_date(_read_ack_text(ack_file))
        if ack_day and ack_day != date.today() and not require_today:
            logger.info(
                f"[启动] 使用持久确认文件 {ack_file} "
                f"(确认日 {ack_day.isoformat()}，非今日；改 CSV/ledger 后请删文件或运行 "
                "scripts/invalidate_startup_ack.py 重确认)"
            )
        else:
            logger.info(f"[启动] 持仓已确认: {ack_file}")
        return _finish_unattended_startup_ack(config, logger, ledger, conn)

    if _env_auto_confirm():
        _save_ack_with_fingerprint(config, ack_file)
        logger.info('[启动] AUTOCTP_CONFIRM=yes，已自动确认')
        return _finish_unattended_startup_ack(config, logger, ledger, conn)

    if _skip_interactive_ack(config, logger):
        return _finish_unattended_startup_ack(config, logger, ledger, conn)

    logger.info("=" * 60)
    logger.info("启动前请核对持仓")
    logger.info("宽跨: data/strangle_positions.csv（程序运行中自动维护，启动时人工核对）")
    logger.info("价差: data/spread_positions.csv（signed volume：正=多，负=空）")
    ledger_summary, spread_summary, audit_summary = _build_startup_summary(
        ledger, conn, config,
    )
    logger.info(ledger_summary)
    if spread_summary:
        logger.info(spread_summary)
    if audit_summary:
        logger.warning(audit_summary)

    ctp_summary = ''
    reconcile_halt = False
    reconcile_issues: list = []
    reconcile_lines: list = []
    if conn:
        ctp_summary = _format_ctp_positions_preview(conn, config, logger)
        logger.info(ctp_summary)
        reconcile_halt, reconcile_issues, reconcile_lines = _preview_reconcile(
            conn, ledger, config, logger,
        )

    summary = _build_full_startup_summary(
        ledger, conn, config, ctp_summary, reconcile_lines,
    )

    # ── 步骤 1/2：账本核对 / derive（无论有无对账预览差异均弹出）──
    if conn is None:
        logger.error('[启动] 缺少 CTP 连接，无法完成账本核对与账户分解')
        return False

    while True:
        action = _prompt_ledger_reconcile_step(
            config, logger, summary, reconcile_issues, reconcile_halt,
        )
        if action == 'no':
            logger.error('=' * 60)
            logger.error('未完成账本核对，程序不进入交易。')
            logger.error('=' * 60)
            return False
        if action == 'derive':
            from spread_derive import apply_derived_spread_from_ctp
            from spread_ledger import store_from_conn

            store = store_from_conn(conn)
            claims = apply_derived_spread_from_ctp(
                conn, ledger, store, config, logger,
            )
            if claims is None:
                logger.error('[启动] 价差认领推导失败，可重试 derive 或取消')
                continue
            logger.info('[启动] 已按 CTP−宽跨 更新价差认领')
            logger.info(format_spread_claims_summary(store_from_conn(conn)))
            reconcile_halt, reconcile_issues, reconcile_lines = _preview_reconcile(
                conn, ledger, config, logger,
            )
            summary = _build_full_startup_summary(
                ledger, conn, config,
                _format_ctp_positions_preview(conn, config, logger),
                reconcile_lines,
            )
            continue
        if action == 'yes':
            break

    # ── 步骤 2/2：账户分解 CTP = 价差 + 宽跨 ──
    if not _run_account_decomposition_step(config, logger, ledger, conn):
        logger.error('=' * 60)
        logger.error('未完成账户分解确认，程序不进入交易。')
        logger.error('=' * 60)
        return False

    return _persist_ack(dual, ack_file, logger, config)
