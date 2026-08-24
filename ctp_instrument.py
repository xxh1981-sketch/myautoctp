"""CTP 合约代码：账本匹配键 vs 交易所报单键。

CSV / ledger 认领刻意 ``upper()``（大小写合并），但 ``ReqOrderInsert`` 按交易所
原文校验。大商所玉米是 ``c2701-C-2340``，账本写成 ``C2701-C-2340`` 会得到
错误码 16「找不到合约」。

两层约定：

* **匹配层**（CSV、认领、对账）：大小写不敏感，继续用大写键。
* **报单/撤单/订阅层**：必须用码表或交易所规则还原的原文。

优先级：行情键 → 期权行情键 → 码表/options_by_strike 结构匹配 → 品种前缀大小写启发式。
不改月份宽度，除非码表里能精确对上同一期权（避免把测试用的 ``SA2608C2400``
误切成郑商所三位数）。
"""

from __future__ import annotations

import re
from typing import Optional

_INST_FIELD_KEYS = frozenset({
    'call_instrument',
    'put_instrument',
    'call_inst',
    'put_inst',
    'filled_instrument',
    'instrument',
    'inst',
})

_DASH_OPTION_RE = re.compile(
    r'^([A-Za-z]+)(\d{3,4})(-MS)?-([CPcp])-(\d+(?:\.\d+)?)$',
)
_COMPACT_OPTION_RE = re.compile(
    r'^([A-Za-z]+)(\d{3,4})([CPcp])(\d+(?:\.\d+)?)$',
)
_PREFIX_RE = re.compile(r'^([A-Za-z]+)')

_LOWERCASE_EXCHANGES = frozenset({'DCE', 'GFEX', 'SHFE', 'INE'})
_UPPERCASE_EXCHANGES = frozenset({'CZCE', 'CFFEX'})


def _exchange_of(instrument: str) -> str:
    try:
        from pairtrade.exchange import get_exchange_type
        return get_exchange_type(instrument) or ''
    except Exception:
        return ''


def apply_exchange_instrument_case(instrument: str) -> str:
    """Only rewrite the product prefix to the exchange's CTP case.

    Leaves ``-C-`` / compact ``C`` / strike / month digits unchanged.
    """
    raw = (instrument or '').strip()
    if not raw:
        return raw
    m = _PREFIX_RE.match(raw)
    if not m:
        return raw
    prefix = m.group(1)
    rest = raw[len(prefix):]
    exchange = _exchange_of(raw)
    if exchange in _LOWERCASE_EXCHANGES:
        new_prefix = prefix.lower()
    elif exchange in _UPPERCASE_EXCHANGES:
        new_prefix = prefix.upper()
    else:
        return raw
    if new_prefix == prefix:
        return raw
    return new_prefix + rest


def parse_option_instrument(instrument: str) -> Optional[dict]:
    """Parse a listed option id into prefix/month/CP/strike; None for futures."""
    raw = (instrument or '').strip()
    if not raw:
        return None
    m = _DASH_OPTION_RE.match(raw)
    if m:
        prefix, month, ms, cp, strike = m.groups()
        return {
            'prefix': prefix,
            'month': month,
            'ms': bool(ms),
            'cp': cp.upper(),
            'strike': float(strike),
            'raw': raw,
        }
    m = _COMPACT_OPTION_RE.match(raw)
    if m:
        prefix, month, cp, strike = m.groups()
        return {
            'prefix': prefix,
            'month': month,
            'ms': False,
            'cp': cp.upper(),
            'strike': float(strike),
            'raw': raw,
        }
    return None


def _months_equivalent(conn, symbol: str, left: str, right: str) -> bool:
    if left == right:
        return True
    normalize = getattr(conn, '_normalize_month', None)
    if not callable(normalize):
        return False
    try:
        return normalize(symbol, left) == normalize(symbol, right)
    except Exception:
        return False


def _options_equivalent(conn, left: str, right: str) -> bool:
    if (left or '').upper() == (right or '').upper():
        return bool(left) and bool(right)
    a = parse_option_instrument(left)
    b = parse_option_instrument(right)
    if not a or not b:
        return False
    if a['cp'] != b['cp'] or a['ms'] != b['ms']:
        return False
    if abs(a['strike'] - b['strike']) > 1e-9:
        return False
    if a['prefix'].lower() != b['prefix'].lower():
        return False
    return _months_equivalent(conn, a['prefix'].lower(), a['month'], b['month'])


def _as_mapping(obj):
    """Real dict (incl. ThreadSafeDict); skip MagicMock conn.quotes children."""
    return obj if isinstance(obj, dict) else None


def _lookup_quotes(conn, instrument: str) -> Optional[str]:
    quotes = _as_mapping(getattr(conn, 'quotes', None))
    option_quotes = _as_mapping(getattr(conn, 'option_quotes', None))
    if quotes is None and option_quotes is None:
        return None
    try:
        from auto_connection_utils import (
            resolve_option_quotes_key,
            resolve_quotes_key,
        )
    except Exception:
        return None
    if quotes is not None:
        key = resolve_quotes_key(conn, instrument)
        if key:
            return key
    if option_quotes is not None:
        return resolve_option_quotes_key(conn, instrument)
    return None


def _lookup_option_info(conn, instrument: str) -> Optional[str]:
    try:
        from auto_connection_utils import (
            extract_symbol_prefix,
            lookup_contract_info,
        )
    except Exception:
        return None
    option_info = _as_mapping(getattr(conn, 'option_info', None))
    if option_info is None:
        return None
    sym = extract_symbol_prefix(instrument)
    index = option_info.get(sym) if sym else None
    if not isinstance(index, dict):
        return None
    cid, _ = lookup_contract_info(index, instrument)
    if cid:
        return cid
    for cid in index:
        if _options_equivalent(conn, instrument, cid):
            return cid
    return None


def _lookup_options_by_strike(conn, instrument: str) -> Optional[str]:
    parsed = parse_option_instrument(instrument)
    if not parsed or parsed['ms']:
        return None
    obs = _as_mapping(getattr(conn, 'options_by_strike', None))
    if obs is None:
        return None
    sym = parsed['prefix'].lower()
    month = parsed['month']
    normalize = getattr(conn, '_normalize_month', None)
    if callable(normalize):
        try:
            month = normalize(sym, month)
        except Exception:
            month = parsed['month']
    row = ((obs.get(sym) or {}).get(month) or {}).get(parsed['strike']) or {}
    side = 'call' if parsed['cp'] == 'C' else 'put'
    found = row.get(side)
    if isinstance(found, str) and found.strip():
        return found.strip()
    return None


def canonical_ctp_instrument(conn, instrument: str) -> str:
    """Return the CTP wire id for ``instrument`` (options or futures)."""
    raw = (instrument or '').strip()
    if not raw:
        return raw

    try:
        key = _lookup_quotes(conn, raw)
        if key:
            return key
    except Exception:
        pass
    try:
        key = _lookup_option_info(conn, raw)
        if key:
            return key
    except Exception:
        pass
    try:
        key = _lookup_options_by_strike(conn, raw)
        if key:
            return key
    except Exception:
        pass
    return apply_exchange_instrument_case(raw)


def rewrite_ledger_instrument_ids(ledger, conn) -> int:
    """Rewrite option ids on strangle JSON positions / unmatched legs.

    Leaves ``leg_claims`` uppercase matching keys unchanged. Returns how many
    fields were rewritten.
    """
    if ledger is None or conn is None:
        return 0
    data = getattr(ledger, '_data', None)
    if not isinstance(data, dict):
        return 0

    def _walk(obj) -> int:
        n = 0
        if isinstance(obj, dict):
            for k, v in list(obj.items()):
                if k in _INST_FIELD_KEYS and isinstance(v, str) and v.strip():
                    new = canonical_ctp_instrument(conn, v)
                    if new != v:
                        obj[k] = new
                        n += 1
                else:
                    n += _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                n += _walk(item)
        return n

    lock = getattr(ledger, '_lock', None)
    acquired = False
    if lock is not None:
        lock.acquire()
        acquired = True
    try:
        n = _walk(data.get('positions'))
        n += _walk(data.get('unmatched_legs'))
        if n and hasattr(ledger, '_save'):
            ledger._save()
        return n
    finally:
        if acquired:
            lock.release()
