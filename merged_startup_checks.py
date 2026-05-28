"""Startup self-checks for merged_main (margin, compat, target_months).

Kept separate from merged_main so unit tests do not need the full autotrade
import chain (VIXEngine, CTP connection, etc.).
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any, Dict, List

from margin_check import check_margin_status

_DEFAULT_STARTUP_MARGIN_ALERT_COOLDOWN_SEC = 1800


def _notify_startup_margin(
    config: dict,
    logger,
    runtime: dict,
    body: str,
) -> None:
    """Send feishu for startup margin issues; cooldown avoids spam on restart loops."""
    import time

    cooldown = float(
        config.get('startup_margin_alert_cooldown_sec')
        or _DEFAULT_STARTUP_MARGIN_ALERT_COOLDOWN_SEC
    )
    now = time.time()
    last = float(runtime.get('_last_startup_margin_alert_time') or 0.0)
    if now - last < cooldown:
        return
    runtime['_last_startup_margin_alert_time'] = now
    try:
        from auto_feishu import send_feishu_message
        send_feishu_message(
            f'⚠️ **AutoCTP 启动保证金告警**\n\n{body}',
            config=config,
        )
    except Exception as notify_err:
        logger.warning(
            '启动保证金飞书通知失败: %s', notify_err, exc_info=True,
        )


def apply_startup_margin(
    conn,
    config: dict,
    logger,
    ledger,
    str_cfg: dict,
) -> bool:
    """Apply startup margin check.

    Returns ``True`` to proceed into the main loop in all cases. ``over_limit``
    and ``unknown`` (when limit enabled) set ``_margin_halt_open`` so opens are
    blocked while closes still run — same semantics as the periodic main-loop
    margin recheck.
    """
    status, reason = check_margin_status(conn, config, logger, context='启动')
    runtime = conn._runtime_state
    limit = config.get('global_margin_limit', 0)

    if status == 'ok':
        runtime['_margin_halt_open'] = False
        runtime['_margin_halt_reason'] = ''
        return True
    if status == 'over_limit':
        halt_reason = f'{reason} (限额 {limit})' if limit else reason
        runtime['_margin_halt_open'] = True
        runtime['_margin_halt_reason'] = halt_reason
        logger.error(
            '启动保证金超限（%s）；已设置 margin_halt_open=True，'
            '仍进入主循环（允许平仓、补A与平A，禁新开；保证金halt下不再补B）',
            reason,
        )
        _notify_startup_margin(
            config,
            logger,
            runtime,
            f'{halt_reason}\n\n'
            '程序将继续运行并扫描平仓；请减仓或调高限额后等待主循环复检解除。',
        )
        return True

    # unknown: cold-start prev should be conservative when margin limit is enabled
    if limit > 0:
        runtime['_margin_halt_open'] = True
        runtime['_margin_halt_reason'] = (
            f'启动保证金无法判定（{reason}），保守禁新开直至复检成功'
        )
        logger.warning(
            f'启动保证金检查无法判定（{reason}）；'
            f'已保守设置 margin_halt_open=True（限额 {limit}），'
            '主循环复检成功后解除；不阻塞启动本身'
        )
        _notify_startup_margin(
            config,
            logger,
            runtime,
            f'启动保证金无法判定（{reason}，限额 {limit}）。\n\n'
            '已保守禁新开；主循环复检成功后将自动解除。',
        )
    else:
        logger.warning(
            f'启动保证金检查无法判定（{reason}），global_margin_limit=0 未启用风控'
        )
    return True


def _project_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _compat_lock_path(config: dict) -> str:
    path = str(config.get('compat_lock_path') or 'docs/compat_lock.yaml')
    if not os.path.isabs(path):
        path = os.path.join(_project_dir(), path)
    return path


def _resolve_repo_root(config: dict, key: str) -> str:
    merged = config.get('merged') or {}
    if key == 'autoctp':
        return _project_dir()
    if key == 'autotrade':
        return os.path.abspath(
            os.environ.get('AUTOTRADE_ROOT') or merged.get('autotrade_root') or r'D:\autotrade',
        )
    if key == 'autostraggle':
        return os.path.abspath(
            os.environ.get('AUTOSTRAGGLE_ROOT') or merged.get('autostraggle_root') or r'D:\autostraggle',
        )
    return ''


def _git_short_commit(repo_root: str) -> str:
    proc = subprocess.run(
        ['git', '-C', repo_root, 'rev-parse', '--short', 'HEAD'],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return ''
    return (proc.stdout or '').strip()


def _git_is_dirty(repo_root: str) -> bool:
    proc = subprocess.run(
        ['git', '-C', repo_root, 'status', '--porcelain'],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return False
    return bool((proc.stdout or '').strip())


def audit_repo_compat_lock(config: dict, logger) -> bool:
    """Validate repo commits against compat lock file.

    Returns True when startup may proceed. When ``compat_lock_enforce`` is true,
    mismatch/missing lock can block startup.
    """
    lock_path = _compat_lock_path(config)
    enforce = bool(config.get('compat_lock_enforce', False))
    if not os.path.isfile(lock_path):
        msg = f'兼容锁文件不存在: {lock_path}'
        if enforce:
            logger.error('[启动自检] %s（compat_lock_enforce=true）', msg)
            return False
        logger.warning('[启动自检] %s（仅告警）', msg)
        return True

    try:
        import yaml
        with open(lock_path, 'r', encoding='utf-8') as f:
            lock_cfg = yaml.safe_load(f) or {}
    except Exception as e:
        msg = f'读取兼容锁失败: {e}'
        if enforce:
            logger.error('[启动自检] %s（compat_lock_enforce=true）', msg)
            return False
        logger.warning('[启动自检] %s（仅告警）', msg)
        return True

    expected = lock_cfg.get('expected_commits') or {}
    if not isinstance(expected, dict):
        expected = {}

    mismatches: List[str] = []
    infos: List[str] = []
    for repo in ('autoctp', 'autotrade', 'autostraggle'):
        root = _resolve_repo_root(config, repo)
        if not root or not os.path.isdir(root):
            mismatches.append(f'{repo}: 目录不存在({root})')
            continue
        actual = _git_short_commit(root)
        if not actual:
            mismatches.append(f'{repo}: 无法读取 git commit')
            continue
        exp = str(expected.get(repo) or '').strip()
        dirty = _git_is_dirty(root)
        infos.append(f'{repo}@{actual}{"(dirty)" if dirty else ""}')
        if exp and exp != actual:
            mismatches.append(f'{repo}: expected={exp} actual={actual}')
        if dirty and bool(config.get('compat_lock_warn_dirty', True)):
            logger.warning('[启动自检] %s 存在未提交改动(dirty)', repo)

    if infos:
        logger.info('[启动自检] 三仓版本: %s', '; '.join(infos))

    if not mismatches:
        return True

    msg = '兼容锁不匹配: ' + '; '.join(mismatches)
    if enforce:
        logger.error('[启动自检] %s（compat_lock_enforce=true，拒绝启动）', msg)
        return False
    logger.warning('[启动自检] %s（仅告警）', msg)
    return True


def audit_target_months(
    conn,
    config: dict,
    logger,
    spread_info: List[Dict[str, Any]],
    strangle_info: List[Dict[str, Any]],
    *,
    send_feishu=None,
) -> None:
    """Warn (and optionally exit) when conn.target_months is empty for tradeinfo symbols."""
    from order_whitelist_guard import audit_target_months_coverage

    missing = audit_target_months_coverage(conn, spread_info, strangle_info)
    if not missing:
        return
    msg = (
        f'以下品种 conn.target_months 为空，发单月白名单不校验邻月: {missing}。'
        '建议检查 tradeinfo 与 CTP 连接初始化。'
    )
    logger.error('[启动自检] %s', msg)
    if send_feishu is None:
        try:
            from auto_feishu import send_feishu_message
            send_feishu = send_feishu_message
        except Exception:
            send_feishu = None
    if send_feishu is not None:
        try:
            send_feishu(
                f'⚠️ **AutoCTP 启动自检告警**\n\n{msg}',
                config=config,
            )
        except Exception as notify_err:
            logger.warning(
                'target_months 自检飞书通知失败: %s', notify_err, exc_info=True,
            )
    if config.get('fail_fast_on_empty_target_months', False):
        logger.error('[启动自检] fail_fast_on_empty_target_months=true，拒绝启动')
        sys.exit(5)
