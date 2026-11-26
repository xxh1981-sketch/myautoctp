"""飞书指令接收：长连接（事件订阅）模式。

用飞书官方 SDK ``lark-oapi`` 的 WebSocket 长连接接收 ``im.message.receive_v1``
事件，替代每 10s 一次的消息轮询；不需要公网回调地址。

对外接口与原轮询实现（``auto_feishu_command.FeishuCommandReceiver``）一致，
直接继承以复用指令解析/暂停状态/webhook 回复逻辑。SDK 缺失或连接失败时
自动回退到轮询，避免飞书暂停指令失效。

由 ``feishu_event_patch`` 安装，仅在
``notifications.feishu_command.mode == 'ws'`` 时启用。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Optional

from auto_feishu_command import (
    HELP_COMMANDS,
    PAUSE_COMMANDS,
    POSITION_COMMANDS,
    RESUME_COMMANDS,
    STATUS_COMMANDS,
    FeishuCommandReceiver,
)

logger = logging.getLogger('AutoCTP')

_ALL_COMMANDS = (
    PAUSE_COMMANDS | RESUME_COMMANDS | STATUS_COMMANDS | HELP_COMMANDS
    | POSITION_COMMANDS
)


def _event_age_sec(msg) -> float:
    """事件距现在的秒数（飞书 create_time 为毫秒时间戳字符串）。"""
    try:
        ts = int(getattr(msg, 'create_time', '0') or 0) / 1000.0
    except (TypeError, ValueError):
        return 0.0
    if ts <= 0:
        return 0.0
    return max(0.0, time.time() - ts)


_DEDUP_MAX = 500
# 飞书会把断连期间未确认的事件在重连后补推；超过该时长的旧指令一律丢弃，
# 避免昨日的「暂停/停止」被当成新指令执行。
_MAX_EVENT_AGE_SEC = 300


def _disconnect_ws_client(client: Any, ws_loop: Any = None) -> None:
    """lark-oapi 的 _disconnect 是协程，需要投递到 SDK 的事件循环执行。"""
    import asyncio
    import inspect

    if hasattr(client, '_auto_reconnect'):
        try:
            client._auto_reconnect = False
        except Exception:
            pass
    fn = getattr(client, '_disconnect', None)
    if not callable(fn):
        return
    result = fn()
    if not inspect.iscoroutine(result):
        return
    try:
        if ws_loop is None:
            from lark_oapi.ws.client import loop as ws_loop

        asyncio.run_coroutine_threadsafe(result, ws_loop).result(timeout=3)
    except Exception:
        result.close()


class FeishuEventReceiver(FeishuCommandReceiver):
    """长连接事件接收器（接口与原轮询接收器保持一致）。"""

    def __init__(self, config: dict):
        super().__init__(config)
        self._ws_thread: Optional[threading.Thread] = None
        self._ws_loop: Any = None
        self._client: Any = None
        self._seen: "OrderedDict[str, None]" = OrderedDict()
        self._seen_lock = threading.Lock()
        self._fallback_polling = False

    def is_running(self) -> bool:
        """Return True when the WS or fallback polling thread is active."""
        if self._stop_event.is_set():
            return False
        if self._fallback_polling:
            t = getattr(self, '_poll_thread', None)
            return bool(t is not None and t.is_alive())
        return bool(self._ws_thread is not None and self._ws_thread.is_alive())

    # ---------- 生命周期 ----------

    def start(self):
        if not self.enabled:
            logger.info('[飞书指令] 长连接未启用（缺少 app_id/app_secret/chat_id）')
            return
        if self.is_running():
            return
        try:
            import lark_oapi  # noqa: F401
        except Exception as e:
            logger.error('[飞书指令] 缺少 lark-oapi(%s)，回退轮询模式', e)
            self._start_polling_fallback()
            return

        self._fallback_polling = False
        self._stop_event.clear()
        self._ws_thread = threading.Thread(
            target=self._run_ws, name='FeishuCommandWS', daemon=True,
        )
        self._ws_thread.start()
        logger.info(
            '[飞书指令] 长连接已启动 chat_id=%s（事件订阅，无轮询）', self.chat_id,
        )

    def _run_ws(self):
        import asyncio

        import lark_oapi as lark
        import lark_oapi.ws.client as ws_client_mod

        ws_loop = None
        try:
            # 必须使用独立事件循环：lark-oapi 模块级 loop 默认取主线程的
            # asyncio loop，在子线程里 run 会占用主循环，导致依赖主循环的
            # 组件（如 autoib 的 ib_insync）报 "This event loop is already running"。
            ws_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(ws_loop)
            ws_client_mod.loop = ws_loop
            self._ws_loop = ws_loop

            handler = (
                lark.EventDispatcherHandler.builder('', '')
                .register_p2_im_message_receive_v1(self._on_im_message)
                .build()
            )
            client = lark.ws.Client(
                self.app_id,
                self.app_secret,
                event_handler=handler,
                log_level=lark.LogLevel.WARNING,
            )
            self._client = client
            if self._stop_event.is_set():
                return
            # 阻塞调用；SDK 内部 auto_reconnect 断线重连
            client.start()
        except Exception as e:
            logger.error('[飞书指令] 长连接异常，回退轮询: %s', e)
            if not self._stop_event.is_set():
                self._start_polling_fallback()
        finally:
            self._ws_loop = None
            if ws_loop is not None:
                try:
                    if not ws_loop.is_running():
                        ws_loop.close()
                except Exception:
                    pass

    def _start_polling_fallback(self):
        self._fallback_polling = True
        super().start()

    def stop(self):
        self._stop_event.set()
        client = self._client
        ws_loop = self._ws_loop
        if client is not None:
            try:
                _disconnect_ws_client(client, ws_loop=ws_loop)
            except Exception as e:
                logger.debug('[飞书指令] 长连接断开异常: %s', e)
        ws_thread = self._ws_thread
        if (
            ws_thread is not None
            and ws_thread.is_alive()
            and ws_thread is not threading.current_thread()
        ):
            ws_thread.join(timeout=3)
        if self._fallback_polling:
            super().stop()
        logger.info('[飞书指令] 长连接已停止')

    # ---------- 事件处理 ----------

    def _on_im_message(self, data) -> None:
        try:
            event = getattr(data, 'event', None)
            msg = getattr(event, 'message', None)
            if msg is None:
                return
            if getattr(msg, 'chat_id', '') != self.chat_id:
                return
            msg_id = str(getattr(msg, 'message_id', '') or '')
            if not msg_id or self._is_duplicate(msg_id):
                return
            text = self._extract_text(msg)
            if text and text in _ALL_COMMANDS:
                age = _event_age_sec(msg)
                if age > _MAX_EVENT_AGE_SEC:
                    logger.warning(
                        "[飞书指令] 忽略积压旧事件: '%s'（%.0f 秒前发出）", text, age,
                    )
                    return
                logger.info("[飞书指令] 收到: '%s'", text)
                self._handle_command(text)
        except Exception as e:
            logger.warning('[飞书指令] 事件处理异常: %s', e)

    def _is_duplicate(self, msg_id: str) -> bool:
        """飞书事件可能重复投递，按 message_id 去重。"""
        with self._seen_lock:
            if msg_id in self._seen:
                return True
            self._seen[msg_id] = None
            while len(self._seen) > _DEDUP_MAX:
                self._seen.popitem(last=False)
            return False

    @staticmethod
    def _extract_text(msg) -> str:
        # 事件推送(p2)字段是 message_type；轮询接口返回的是 msg_type
        msg_type = getattr(msg, 'message_type', '') or getattr(msg, 'msg_type', '')
        if msg_type != 'text':
            return ''
        content = getattr(msg, 'content', '') or ''
        try:
            text = json.loads(content).get('text', '').strip()
        except (ValueError, TypeError):
            text = str(content).strip()
        # 去除 @机器人 占位符（如 "@_user_1 停止交易"）
        return re.sub(r'@_user_\d+\s*', '', text).strip()
