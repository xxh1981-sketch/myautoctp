"""日志降噪过滤器：节流高频重复 INFO + 降级预期内的撤单错误回报。

背景
----
- VIX 月份提升 / "VIX 无法计算" 等提示来自 autotrade ``auto_vix.VIXEngine``：
  宽跨路径会对同一品种按组多次调用 ``calculate_vix``，导致同一条日志在同一轮
  （甚至同一秒）重复打印十余次（实测单条 "提升次近月为近月" 一天上万行）。
- ``[撤单] 错误回报: ...当前状态禁止此项操作`` 来自 autotrade，在**非交易状态**
  下清理残单时必然出现，是预期摩擦而非真实故障，却被记为 ERROR。

本模块只对**日志输出**做收敛，**不触碰 VIX 算法 / 每轮缓存 / autotrade 代码**：
- 节流：匹配指定子串的 INFO/WARNING 记录，在窗口内"同一条完整文本"只输出一次。
- 降级：匹配指定子串的记录将日志级别下调（默认 ERROR→WARNING），并可一并节流。

安全约束（务必保持）
--------------------
- 绝不丢弃未经显式降级的 ERROR/CRITICAL 记录——真实故障不会被静默吞掉。
- 仅按"完整消息文本"去重：不同品种 / 不同数值互不影响；交易动作类日志
  （触发交易 / 开仓 / 平仓 / 账本选中 等）不在匹配范围，永不被节流。
- 线程安全：CTP 回调与主循环在不同线程写同一 logger，去重表加锁保护。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

# 过滤器实例标记：用于幂等安装（重复调用先摘除旧实例）。
_FILTER_MARK = '_autoctp_log_noise_filter'

DEFAULT_THROTTLE_SUBSTRINGS: Tuple[str, ...] = (
    '提升次近月为近月',
    '品种整体 VIX 无法计算',
    'VIX无法计算',
)
# (子串, 目标级别)
DEFAULT_DOWNGRADE: Tuple[Tuple[str, int], ...] = (
    ('当前状态禁止此项操作', logging.WARNING),
)
DEFAULT_WINDOW_SEC = 60.0
_MAX_KEYS = 4096

_install_error: Optional[str] = None


def get_install_error() -> Optional[str]:
    """返回最近一次安装失败原因（成功为 None），与其它守卫模块约定一致。"""
    return _install_error


def _coerce_level(value: Any, default: int = logging.WARNING) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        lvl = logging.getLevelName(value.strip().upper())
        if isinstance(lvl, int):
            return lvl
    return default


class LogNoiseFilter(logging.Filter):
    """节流重复日志 + 降级预期错误。挂在 logger 上（单点执行，避免多 handler 重复计数）。"""

    def __init__(
        self,
        throttle_substrings,
        window_sec,
        downgrade_rules,
        max_keys: int = _MAX_KEYS,
    ) -> None:
        super().__init__()
        self._subs: Tuple[str, ...] = tuple(s for s in (throttle_substrings or ()) if s)
        try:
            w = float(window_sec)
        except (TypeError, ValueError):
            w = 0.0
        self._window: float = w if w > 0 else 0.0
        self._downgrade: Tuple[Tuple[str, int], ...] = tuple(
            (sub, int(lvl)) for sub, lvl in (downgrade_rules or ()) if sub
        )
        self._max_keys = int(max_keys) if max_keys and max_keys > 0 else _MAX_KEYS
        self._seen: Dict[str, float] = {}
        self._lock = threading.Lock()
        setattr(self, _FILTER_MARK, True)

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            msg = record.getMessage()
        except Exception:
            # 取不到文本就放行，绝不因降噪逻辑吞日志。
            return True

        # 1) 降级：仅修改级别，绝不在此处丢弃。
        if self._downgrade:
            for sub, lvl in self._downgrade:
                if sub in msg:
                    if record.levelno > lvl:
                        record.levelno = lvl
                        record.levelname = logging.getLevelName(lvl)
                    break

        # 2) 节流：窗口内同一文本只过一次；绝不节流 ERROR 及以上（降级后的 WARNING 可被节流）。
        if self._window > 0 and self._subs and record.levelno < logging.ERROR:
            for sub in self._subs:
                if sub in msg:
                    now = time.monotonic()
                    with self._lock:
                        last = self._seen.get(msg)
                        if last is not None and (now - last) < self._window:
                            return False
                        self._seen[msg] = now
                        if len(self._seen) > self._max_keys:
                            self._gc_locked(now)
                    break
        return True

    def _gc_locked(self, now: float) -> None:
        """调用方须持锁。先按窗口清过期项，仍超限则整表清空避免无界增长。"""
        cutoff = now - self._window
        for key in [k for k, t in self._seen.items() if t < cutoff]:
            self._seen.pop(key, None)
        if len(self._seen) > self._max_keys:
            self._seen.clear()


def build_filter_from_config(config: Optional[dict]) -> Optional[LogNoiseFilter]:
    """按 config['log_noise'] 构建过滤器；enabled=false 返回 None。"""
    cfg = (config or {}).get('log_noise')
    if cfg is None:
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    if not cfg.get('enabled', True):
        return None

    subs = cfg.get('throttle_substrings')
    if subs is None:
        subs = list(DEFAULT_THROTTLE_SUBSTRINGS)
    elif isinstance(subs, str):
        subs = [subs]

    window = cfg.get('throttle_window_sec', DEFAULT_WINDOW_SEC)

    downgrade_cfg = cfg.get('downgrade')
    if downgrade_cfg is None:
        downgrade_rules: List[Tuple[str, int]] = list(DEFAULT_DOWNGRADE)
    else:
        downgrade_rules = []
        for item in downgrade_cfg:
            if isinstance(item, dict):
                sub = item.get('substring') or item.get('match')
                lvl = _coerce_level(item.get('to_level', item.get('to')))
                if sub:
                    downgrade_rules.append((sub, lvl))

    return LogNoiseFilter(subs, window, downgrade_rules)


def install_log_noise_filter(logger: logging.Logger, config: Optional[dict]) -> bool:
    """在 logger 上安装降噪过滤器（幂等）。失败返回 False 并记录原因。"""
    global _install_error
    _install_error = None
    try:
        # 幂等：先摘除本模块此前装的实例。
        for flt in list(getattr(logger, 'filters', [])):
            if getattr(flt, _FILTER_MARK, False):
                logger.removeFilter(flt)
        nf = build_filter_from_config(config)
        if nf is None:
            return True
        logger.addFilter(nf)
        return True
    except Exception as e:  # noqa: BLE001
        _install_error = repr(e)
        return False
