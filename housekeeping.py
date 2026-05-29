"""长跑磁盘治理（7×24 无人值守）。

三类文件在长跑下会无界增长，全仓原本无任何磁盘清理：

1. **journal 分片**：``trade_journal`` 按天分片，但 ``_journal_glob_paths`` 只在
   *读取* 时按 ``journal_retain_days`` 过滤，旧分片文件从不删除（spread/strangle/
   fill 三套）。
2. **fill_ledger.csv**：append-only 分析日志，无轮转；autoctp 内无读取方。
3. **日志**：``info/*.log`` 按天命名但从不清理（轮转由 ``setup_merged_logger``
   升级的 TimedRotatingFileHandler 负责，本模块只兜底清理遗留 ``*.log*``）。

本模块提供幂等、可反复安全调用的清理：启动跑一次、主循环按
``housekeeping_interval_sec`` 周期跑。所有操作只删除/归档"明显过期"的文件，
绝不触碰保留窗口内的活动文件；任何子步骤失败只告警，不影响交易。
"""

from __future__ import annotations

import glob
import os
from datetime import date, datetime, timedelta

# journal 分片在 read 用 retain_days 过滤之外，多保留的安全余量（天）。
_JOURNAL_SHARD_GRACE_DAYS = 3


def _shard_date(base_path: str, path: str):
    """从 ``<root>-YYYYMMDD<ext>`` 解析分片日期；非分片返回 None。"""
    root, ext = os.path.splitext(base_path)
    ext = ext or '.jsonl'
    root_base = os.path.basename(root)
    name = os.path.basename(path)
    suffix = name[len(root_base):]
    if not suffix.startswith('-'):
        return None
    day_part = suffix[1:9]
    if not day_part.isdigit() or len(day_part) != 8:
        return None
    try:
        return date(int(day_part[:4]), int(day_part[4:6]), int(day_part[6:8]))
    except ValueError:
        return None


def prune_journal_shards(config: dict, logger=None) -> int:
    """删除超出保留窗口（retain_days + grace）的 journal 分片文件。

    与 ``trade_journal._journal_glob_paths`` 的读取过滤语义一致：被删除的分片
    早已不参与 scan/dedupe，删除只回收磁盘，不改变行为。返回删除文件数。
    """
    from trade_journal import journal_daily_shards_enabled, journal_retain_days

    if not journal_daily_shards_enabled(config):
        return 0

    keep_days = journal_retain_days(config) + _JOURNAL_SHARD_GRACE_DAYS
    cutoff = date.today() - timedelta(days=keep_days - 1)

    bases = []
    try:
        from spread_fill_sync import _journal_path as _spread_jp
        from strangle_fill_sync import _journal_path as _strangle_jp
        from fill_ledger import fill_ledger_journal_path
        bases = [
            _spread_jp(config),
            _strangle_jp(config),
            fill_ledger_journal_path(config),
        ]
    except Exception as e:
        if logger:
            logger.debug(f'[housekeeping] 解析 journal 路径失败: {e}')
        return 0

    removed = 0
    for base in bases:
        root, ext = os.path.splitext(base)
        ext = ext or '.jsonl'
        for path in glob.glob(f'{root}-*{ext}'):
            shard_day = _shard_date(base, path)
            if shard_day is None or shard_day >= cutoff:
                continue
            try:
                os.remove(path)
                removed += 1
            except OSError as e:
                if logger:
                    logger.debug(f'[housekeeping] 删除分片失败 {path}: {e}')
    if removed and logger:
        logger.info(
            f'[housekeeping] 清理过期 journal 分片 {removed} 个'
            f'（保留 {keep_days} 天）'
        )
    return removed


def rotate_fill_ledger(config: dict, logger=None) -> bool:
    """fill_ledger.csv 超过上限则归档为带时间戳副本，并保留最近 N 份。

    分析日志、非持仓真相：归档后下次 append 会自动重建表头。返回是否归档。
    """
    if not config.get('fill_ledger_rotate_enabled', True):
        return False
    from fill_ledger import fill_ledger_csv_path

    try:
        path = fill_ledger_csv_path(config)
    except Exception:
        return False
    if not os.path.isfile(path):
        return False

    max_mb = float(config.get('fill_ledger_max_mb', 50) or 0)
    if max_mb <= 0:
        return False
    try:
        size_mb = os.path.getsize(path) / (1024 * 1024)
    except OSError:
        return False
    if size_mb < max_mb:
        return False

    root, ext = os.path.splitext(path)
    ext = ext or '.csv'
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    archive = f'{root}-archive-{stamp}{ext}'
    try:
        os.replace(path, archive)
    except OSError as e:
        if logger:
            logger.warning(f'[housekeeping] fill_ledger 归档失败: {e}')
        return False
    if logger:
        logger.info(
            f'[housekeeping] fill_ledger.csv 达 {size_mb:.1f}MB(>= {max_mb}MB)，'
            f'已归档为 {os.path.basename(archive)}'
        )

    keep = int(config.get('fill_ledger_archive_keep', 10) or 0)
    if keep > 0:
        archives = sorted(glob.glob(f'{root}-archive-*{ext}'))
        for old in archives[:-keep]:
            try:
                os.remove(old)
            except OSError:
                pass
    return True


def prune_logs(config: dict, logger=None) -> int:
    """按 mtime 清理日志目录内超过 log_retain_days 的 ``*.log*`` 文件。

    日志主轮转由 ``setup_merged_logger`` 的 TimedRotatingFileHandler 负责
    （backupCount 自动删轮转副本）；本函数兜底清理遗留的按天命名旧文件。
    日志目录取 ``config['_log_dir']``（由 logger 升级写入）或 ``config['log_dir']``。
    """
    log_dir = config.get('_log_dir') or config.get('log_dir')
    if not log_dir or not os.path.isdir(log_dir):
        return 0
    retain = int(config.get('log_retain_days', 30) or 0)
    if retain <= 0:
        return 0
    cutoff = datetime.now().timestamp() - retain * 86400
    removed = 0
    # 当前活动轮转文件（autoctp.log）不删；只清旧文件。
    active = os.path.join(log_dir, 'autoctp.log')
    for path in glob.glob(os.path.join(log_dir, '*.log')) + glob.glob(
        os.path.join(log_dir, '*.log.*')
    ):
        if os.path.abspath(path) == os.path.abspath(active):
            continue
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            pass
    if removed and logger:
        logger.info(f'[housekeeping] 清理过期日志 {removed} 个（保留 {retain} 天）')
    return removed


def run_housekeeping(config: dict, logger=None) -> dict:
    """运行全部清理步骤；各步独立 try/except，返回汇总。"""
    if not config.get('housekeeping_enabled', True):
        return {'journal_shards': 0, 'fill_ledger_rotated': False, 'logs': 0}
    result = {'journal_shards': 0, 'fill_ledger_rotated': False, 'logs': 0}
    try:
        result['journal_shards'] = prune_journal_shards(config, logger)
    except Exception as e:
        if logger:
            logger.warning(f'[housekeeping] 清理 journal 分片异常: {e}', exc_info=True)
    try:
        result['fill_ledger_rotated'] = rotate_fill_ledger(config, logger)
    except Exception as e:
        if logger:
            logger.warning(f'[housekeeping] 轮转 fill_ledger 异常: {e}', exc_info=True)
    try:
        result['logs'] = prune_logs(config, logger)
    except Exception as e:
        if logger:
            logger.warning(f'[housekeeping] 清理日志异常: {e}', exc_info=True)
    return result
