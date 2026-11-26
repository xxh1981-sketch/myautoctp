"""feishu_event_receiver / feishu_event_patch 单元测试。"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import types
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _FakeBaseReceiver:
    def __init__(self, config: dict):
        cmd_cfg = ((config or {}).get('notifications') or {}).get('feishu_command') or {}
        self.app_id = cmd_cfg.get('app_id', '')
        self.app_secret = cmd_cfg.get('app_secret', '')
        self.chat_id = cmd_cfg.get('chat_id', '')
        self.enabled = bool(cmd_cfg.get('enabled', True) and self.app_id and self.app_secret and self.chat_id)
        self._stop_event = threading.Event()
        self._poll_thread = None
        self.poll_started = 0
        self.poll_stopped = 0
        self.handled = []

    def start(self):
        self._stop_event.clear()
        self.poll_started += 1

    def stop(self):
        self._stop_event.set()
        self.poll_stopped += 1

    def _handle_command(self, text: str):
        self.handled.append(text)


def _make_fake_auto_feishu_command():
    mod = types.ModuleType('auto_feishu_command')
    mod.PAUSE_COMMANDS = {'暂停', '停止交易'}
    mod.RESUME_COMMANDS = {'恢复', '恢复交易'}
    mod.STATUS_COMMANDS = {'状态'}
    mod.HELP_COMMANDS = {'帮助'}
    mod.POSITION_COMMANDS = {'持仓查询'}
    mod.FeishuCommandReceiver = _FakeBaseReceiver
    mod._receiver = None
    mod.start_calls = []

    def start_command_receiver(cfg=None):
        mod.start_calls.append(cfg)
        return None

    def stop_command_receiver():
        if mod._receiver is not None:
            mod._receiver.stop()

    mod.start_command_receiver = start_command_receiver
    mod.stop_command_receiver = stop_command_receiver
    return mod


def _ws_config(mode: str = 'ws') -> dict:
    return {
        'notifications': {
            'feishu_command': {
                'enabled': True,
                'mode': mode,
                'app_id': 'cli_test',
                'app_secret': 'sec_test',
                'chat_id': 'oc_chat_1',
            },
        },
    }


class TestFeishuEventReceiverAndPatch(unittest.TestCase):

    def setUp(self):
        self._saved_modules = {
            name: sys.modules.get(name)
            for name in ('auto_feishu_command', 'feishu_event_receiver', 'feishu_event_patch')
        }
        self._fake_cmd = _make_fake_auto_feishu_command()
        sys.modules['auto_feishu_command'] = self._fake_cmd
        sys.modules.pop('feishu_event_receiver', None)
        sys.modules.pop('feishu_event_patch', None)

    def tearDown(self):
        for name, mod in self._saved_modules.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod

    def test_patch_skips_when_mode_is_poll(self):
        import feishu_event_patch as patch_mod

        orig_start = self._fake_cmd.start_command_receiver
        self.assertTrue(patch_mod.install_feishu_event_receiver(_ws_config('poll')))
        self.assertIs(self._fake_cmd.start_command_receiver, orig_start)

    def test_patch_restarts_stopped_singleton_after_stop_command_receiver(self):
        import feishu_event_patch as patch_mod
        from feishu_event_receiver import FeishuEventReceiver

        cfg = _ws_config('ws')
        self.assertTrue(patch_mod.install_feishu_event_receiver(cfg))

        start_count = 0

        def _fake_start(self_rec):
            nonlocal start_count
            start_count += 1
            self_rec._stop_event.clear()
            self_rec._ws_thread = MagicMock(is_alive=MagicMock(return_value=True))

        with patch.object(FeishuEventReceiver, 'start', _fake_start):
            r1 = self._fake_cmd.start_command_receiver(cfg)
            self.assertIsInstance(r1, FeishuEventReceiver)
            self.assertEqual(start_count, 1)

            # 活跃时重复调用保持幂等，不重复建连
            r2 = self._fake_cmd.start_command_receiver(cfg)
            self.assertIs(r2, r1)
            self.assertEqual(start_count, 1)

            # 模拟主循环异常退出 finally 调用 stop_command_receiver()
            self._fake_cmd.stop_command_receiver()
            self.assertFalse(r1.is_running())

            # 进程内重启后再次 start_command_receiver 必须重新拉起接收器
            r3 = self._fake_cmd.start_command_receiver(cfg)
            self.assertIs(r3, r1)
            self.assertEqual(start_count, 2)

    def test_stop_disables_auto_reconnect_before_disconnect(self):
        from feishu_event_receiver import FeishuEventReceiver

        rec = FeishuEventReceiver(_ws_config('ws'))
        events = []

        class _FakeClient:
            def __init__(self):
                self._auto_reconnect = True

            def _disconnect(self):
                events.append(('disconnect', self._auto_reconnect))

        rec._client = _FakeClient()
        rec.stop()
        self.assertFalse(rec._client._auto_reconnect)
        self.assertEqual(events, [('disconnect', False)])
        self.assertTrue(rec._stop_event.is_set())

    def test_missing_lark_oapi_falls_back_to_polling(self):
        from feishu_event_receiver import FeishuEventReceiver

        rec = FeishuEventReceiver(_ws_config('ws'))
        with patch.dict(sys.modules, {'lark_oapi': None}):
            rec.start()
        self.assertTrue(rec._fallback_polling)
        self.assertEqual(rec.poll_started, 1)

    def test_on_im_message_dedupe_strip_mention_and_drop_stale(self):
        from feishu_event_receiver import FeishuEventReceiver

        rec = FeishuEventReceiver(_ws_config('ws'))

        def _make_event(msg_id: str, text: str, *, chat_id: str = 'oc_chat_1', age_sec: float = 1.0):
            msg = types.SimpleNamespace(
                chat_id=chat_id,
                message_id=msg_id,
                message_type='text',
                content=json.dumps({'text': text}),
                create_time=str(int((time.time() - age_sec) * 1000)),
            )
            return types.SimpleNamespace(event=types.SimpleNamespace(message=msg))

        # 正常消息（含 @机器人 占位符）
        rec._on_im_message(_make_event('m1', '@_user_1 停止交易'))
        # 重复 message_id 应去重
        rec._on_im_message(_make_event('m1', '@_user_1 停止交易'))
        # 其它群聊忽略
        rec._on_im_message(_make_event('m2', '恢复交易', chat_id='oc_other'))
        # 超过 300s 的旧积压事件丢弃
        rec._on_im_message(_make_event('m3', '恢复交易', age_sec=600.0))
        # 正常恢复指令
        rec._on_im_message(_make_event('m4', '恢复交易'))

        self.assertEqual(rec.handled, ['停止交易', '恢复交易'])


if __name__ == '__main__':
    unittest.main()
