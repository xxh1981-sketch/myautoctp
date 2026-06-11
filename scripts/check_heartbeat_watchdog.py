#!/usr/bin/env python3
"""外部心跳 watchdog：检查主循环心跳文件 mtime，超时飞书告警。

供 Windows 计划任务 / cron 每 1–5 分钟调用：
  .\\.venv\\Scripts\\python scripts/check_heartbeat_watchdog.py

纯可观测性：不改变 AutoCTP 交易语义。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import ctp_bootstrap  # noqa: F401


def _project_root() -> str:
    return ROOT


def _abs_path(path: str) -> str:
    p = str(path or '').strip()
    if not p:
        return p
    if os.path.isabs(p):
        return p
    return os.path.join(_project_root(), p)


def _read_text(path: str) -> str:
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    except OSError:
        return ''


def _is_stopped(content: str) -> bool:
    for line in content.splitlines():
        line = line.strip()
        if line == 'status=STOPPED' or line.startswith('status=STOPPED'):
            return True
    return False


def _cooldown_marker_path(config: dict) -> str:
    wd = config.get('heartbeat_watchdog') or {}
    custom = str(wd.get('alert_marker_file') or '').strip()
    if custom:
        return _abs_path(custom)
    return _abs_path('data/heartbeat_watchdog_alert.txt')


def _in_cooldown(marker_path: str, cooldown_sec: float) -> bool:
    if cooldown_sec <= 0:
        return False
    raw = _read_text(marker_path).strip()
    if not raw:
        return False
    try:
        last = float(raw)
    except ValueError:
        return False
    return (time.time() - last) < cooldown_sec


def _write_cooldown_marker(marker_path: str) -> None:
    parent = os.path.dirname(marker_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(marker_path, 'w', encoding='utf-8') as f:
        f.write(f'{time.time():.3f}\n')


def _send_alert(msg: str, config: dict) -> bool:
    wd = config.get('heartbeat_watchdog') or {}
    webhook = str(wd.get('feishu_webhook') or '').strip()
    cfg = dict(config)
    if webhook:
        cfg = dict(config)
        cfg['feishu_webhook'] = webhook
    try:
        from auto_feishu import send_feishu_message
        return bool(send_feishu_message(msg, config=cfg))
    except Exception as e:
        print(f'[watchdog] 飞书发送失败: {e}', file=sys.stderr)
        return False


def run_check(*, dry_run: bool = False) -> int:
    from merged_config import load_merged_config

    config = load_merged_config()
    wd = config.get('heartbeat_watchdog') or {}
    alert_after = float(wd.get('alert_after_sec', 300) or 300)
    cooldown = float(wd.get('alert_cooldown_sec', 600) or 600)

    hb_rel = str(config.get('heartbeat_file') or 'data/main_loop_heartbeat.txt')
    hb_path = _abs_path(hb_rel)
    marker_path = _cooldown_marker_path(config)

    if not hb_path:
        print('[watchdog] heartbeat_file 未配置，跳过')
        return 0

    if not os.path.isfile(hb_path):
        msg = (
            '🔴 **AutoCTP 主循环心跳停止**\n\n'
            f'心跳文件不存在: `{hb_path}`\n'
            '请检查 merged_main.py 进程是否在运行。'
        )
        if dry_run:
            print(f'[watchdog] DRY-RUN 告警: {msg}')
            return 1
        if _in_cooldown(marker_path, cooldown):
            print('[watchdog] 心跳缺失告警冷却中，跳过')
            return 0
        if _send_alert(msg, config):
            _write_cooldown_marker(marker_path)
        return 1

    content = _read_text(hb_path)
    if _is_stopped(content):
        print('[watchdog] status=STOPPED，静默跳过')
        return 0

    age = time.time() - os.path.getmtime(hb_path)
    if age <= alert_after:
        print(f'[watchdog] 心跳正常（{age:.0f}s < {alert_after:.0f}s）')
        return 0

    ts = datetime.fromtimestamp(os.path.getmtime(hb_path)).isoformat(timespec='seconds')
    pid_line = ''
    for line in content.splitlines():
        if 'pid=' in line:
            pid_line = line.strip()
            break
    msg = (
        '🔴 **AutoCTP 主循环心跳停止**\n\n'
        f'心跳文件: `{hb_path}`\n'
        f'最后更新: {ts}（{age:.0f}s 前）\n'
        f'阈值: {alert_after:.0f}s\n'
    )
    if pid_line:
        msg += f'{pid_line}\n'
    msg += '\n进程可能挂死、被强杀或主循环长时间未推进；请检查日志与 CTP 连接。'

    if dry_run:
        print(f'[watchdog] DRY-RUN 告警: {msg}')
        return 1
    if _in_cooldown(marker_path, cooldown):
        print('[watchdog] 心跳超时告警冷却中，跳过')
        return 0
    if _send_alert(msg, config):
        _write_cooldown_marker(marker_path)
        print(f'[watchdog] 已发送告警（心跳停滞 {age:.0f}s）')
    else:
        print('[watchdog] 告警发送未成功', file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description='AutoCTP 外部心跳 watchdog')
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='只打印将要告警的内容，不发送飞书',
    )
    args = parser.parse_args()
    return run_check(dry_run=args.dry_run)


if __name__ == '__main__':
    raise SystemExit(main())
