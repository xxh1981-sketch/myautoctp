"""housekeeping 长跑磁盘治理单测。"""

import logging
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import housekeeping  # noqa: E402


class _FakeLogger:
    def __init__(self):
        self.messages = []

    def info(self, msg, *a, **k):
        self.messages.append(('info', msg))

    def warning(self, msg, *a, **k):
        self.messages.append(('warning', msg))

    def debug(self, msg, *a, **k):
        self.messages.append(('debug', msg))


def _config(tmp: str, **over) -> dict:
    cfg = {
        'dual_strategy': {
            'journal_daily_shards': True,
            'journal_retain_days': 14,
            'spread_trade_journal': os.path.join(tmp, 'spread_journal.jsonl'),
            'strangle_trade_journal': os.path.join(tmp, 'strangle_journal.jsonl'),
            'fill_ledger_journal': os.path.join(tmp, 'fill_journal.jsonl'),
            'fill_ledger_csv': os.path.join(tmp, 'fill_ledger.csv'),
        },
    }
    cfg.update(over)
    return cfg


def _shard(tmp: str, base: str, d: date) -> str:
    path = os.path.join(tmp, f'{base}-{d.strftime("%Y%m%d")}.jsonl')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('{}\n')
    return path


class TestPruneJournalShards(unittest.TestCase):
    def test_deletes_old_keeps_recent(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(tmp)
            today = date.today()
            # keep_days = 14 + 3 grace = 17，cutoff = today-16
            recent = _shard(tmp, 'spread_journal', today)
            edge = _shard(tmp, 'spread_journal', today - timedelta(days=16))
            old = _shard(tmp, 'spread_journal', today - timedelta(days=40))
            removed = housekeeping.prune_journal_shards(cfg, _FakeLogger())
            self.assertTrue(os.path.exists(recent))
            self.assertTrue(os.path.exists(edge))
            self.assertFalse(os.path.exists(old))
            self.assertEqual(removed, 1)

    def test_noop_when_shards_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(tmp)
            cfg['dual_strategy']['journal_daily_shards'] = False
            old = _shard(tmp, 'spread_journal', date.today() - timedelta(days=99))
            removed = housekeeping.prune_journal_shards(cfg, None)
            self.assertEqual(removed, 0)
            self.assertTrue(os.path.exists(old))

    def test_ignores_non_dated_and_other_strategies(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(tmp)
            base = os.path.join(tmp, 'spread_journal.jsonl')
            with open(base, 'w', encoding='utf-8') as f:
                f.write('{}\n')
            old_strangle = _shard(tmp, 'strangle_journal',
                                  date.today() - timedelta(days=50))
            housekeeping.prune_journal_shards(cfg, None)
            self.assertTrue(os.path.exists(base))  # 无日期后缀不删
            self.assertFalse(os.path.exists(old_strangle))  # 各策略都清


class TestRotateFillLedger(unittest.TestCase):
    def _write_csv(self, path, mb):
        with open(path, 'wb') as f:
            f.write(b'x' * int(mb * 1024 * 1024))

    def test_rotates_when_over_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(tmp, fill_ledger_max_mb=1)
            csv_path = os.path.join(tmp, 'fill_ledger.csv')
            self._write_csv(csv_path, 1.5)
            rotated = housekeeping.rotate_fill_ledger(cfg, _FakeLogger())
            self.assertTrue(rotated)
            self.assertFalse(os.path.exists(csv_path))
            archives = [n for n in os.listdir(tmp) if 'archive' in n]
            self.assertEqual(len(archives), 1)

    def test_no_rotate_under_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(tmp, fill_ledger_max_mb=50)
            csv_path = os.path.join(tmp, 'fill_ledger.csv')
            self._write_csv(csv_path, 0.1)
            self.assertFalse(housekeeping.rotate_fill_ledger(cfg, None))
            self.assertTrue(os.path.exists(csv_path))

    def test_prunes_old_archives(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(tmp, fill_ledger_max_mb=1, fill_ledger_archive_keep=2)
            csv_path = os.path.join(tmp, 'fill_ledger.csv')
            for i in range(4):
                a = os.path.join(tmp, f'fill_ledger-archive-2026010{i}-000000.csv')
                with open(a, 'w') as f:
                    f.write('old')
            self._write_csv(csv_path, 1.2)
            housekeeping.rotate_fill_ledger(cfg, None)
            archives = sorted(n for n in os.listdir(tmp) if 'archive' in n)
            self.assertEqual(len(archives), 2)  # keep=2（含本次新归档）


class TestPruneLogs(unittest.TestCase):
    def test_deletes_old_keeps_active_and_recent(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {'_log_dir': tmp, 'log_retain_days': 30}
            active = os.path.join(tmp, 'autoctp.log')
            recent = os.path.join(tmp, '20260528.log')
            old = os.path.join(tmp, '20251201.log')
            for p in (active, recent, old):
                with open(p, 'w') as f:
                    f.write('log')
            old_ts = (datetime.now() - timedelta(days=99)).timestamp()
            os.utime(old, (old_ts, old_ts))
            # active 故意设为很旧，验证仍不删
            os.utime(active, (old_ts, old_ts))
            removed = housekeeping.prune_logs(cfg, _FakeLogger())
            self.assertTrue(os.path.exists(active))
            self.assertTrue(os.path.exists(recent))
            self.assertFalse(os.path.exists(old))
            self.assertEqual(removed, 1)

    def test_noop_without_log_dir(self):
        self.assertEqual(housekeeping.prune_logs({}, None), 0)


class TestRunHousekeeping(unittest.TestCase):
    def test_disabled_returns_zeros(self):
        out = housekeeping.run_housekeeping({'housekeeping_enabled': False}, None)
        self.assertEqual(out['journal_shards'], 0)
        self.assertFalse(out['fill_ledger_rotated'])

    def test_aggregates_and_isolates_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(tmp, fill_ledger_max_mb=1)
            _shard(tmp, 'spread_journal', date.today() - timedelta(days=40))
            with open(os.path.join(tmp, 'fill_ledger.csv'), 'wb') as f:
                f.write(b'x' * (2 * 1024 * 1024))
            out = housekeeping.run_housekeeping(cfg, _FakeLogger())
            self.assertEqual(out['journal_shards'], 1)
            self.assertTrue(out['fill_ledger_rotated'])


class TestRotatingLogHandler(unittest.TestCase):
    def test_upgrades_plain_filehandler(self):
        from logging.handlers import TimedRotatingFileHandler
        from merged_config import _install_rotating_log_handler

        with tempfile.TemporaryDirectory() as tmp:
            logger = logging.getLogger('test_rotate_upgrade')
            logger.handlers.clear()
            plain = logging.FileHandler(
                os.path.join(tmp, '20260529.log'), encoding='utf-8',
            )
            logger.addHandler(plain)
            cfg = {'log_retain_days': 7}
            _install_rotating_log_handler(logger, cfg)
            rotators = [h for h in logger.handlers
                        if isinstance(h, TimedRotatingFileHandler)]
            self.assertEqual(len(rotators), 1)
            self.assertEqual(rotators[0].backupCount, 7)
            self.assertEqual(cfg['_log_dir'], tmp)
            # 原普通 FileHandler 已移除
            self.assertFalse(any(
                type(h) is logging.FileHandler for h in logger.handlers
            ))
            for h in list(logger.handlers):
                h.close()
            logger.handlers.clear()

    def test_noop_without_filehandler(self):
        from merged_config import _install_rotating_log_handler
        logger = logging.getLogger('test_rotate_noop')
        logger.handlers.clear()
        cfg = {}
        _install_rotating_log_handler(logger, cfg)
        self.assertNotIn('_log_dir', cfg)


if __name__ == '__main__':
    unittest.main()
