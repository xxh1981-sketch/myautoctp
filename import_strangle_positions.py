"""Import strangle leg claims from a 2-column CSV: instrument, volume."""

import argparse
import csv
import io
import os
from typing import Dict, Set

from atomic_io import atomic_write_text
from env_utils import is_config_abs_path
from merged_config import load_merged_config

_CSV_HEADERS = frozenset({
    'instrument', 'volume',
    '期权代码', '持仓手数',
})

def _project_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _default_csv() -> str:
    return os.path.join(_project_dir(), 'data', 'strangle_positions.csv')


def _looks_like_header(cols: list) -> bool:
    if len(cols) < 2:
        return False
    left = str(cols[0]).strip().lower()
    right = str(cols[1]).strip().lower()
    if left in _CSV_HEADERS or right in _CSV_HEADERS:
        return True
    try:
        int(right)
        return False
    except ValueError:
        return True

def positions_csv_path(config: dict = None) -> str:
    config = config or {}
    dual = config.get('dual_strategy') or {}
    path = dual.get(
        'strangle_positions_csv',
        os.path.join(_project_dir(), 'data', 'strangle_positions.csv'),
    )
    if not is_config_abs_path(path):
        path = os.path.join(_project_dir(), path)
    return path


def load_positions_csv(path: str) -> Dict[str, int]:
    """空表或仅有表头时返回 {}，表示无持仓。"""
    claims: Dict[str, int] = {}
    with open(path, 'r', encoding='utf-8-sig') as f:
        reader = csv.reader(f)
        for line_no, row in enumerate(reader, start=1):
            if not row or all(not str(c).strip() for c in row):
                continue
            if len(row) < 2:
                raise ValueError(f"{path} line {line_no}: expected 2 columns (instrument, volume)")
            if line_no == 1 and _looks_like_header(row):
                continue
            inst = str(row[0]).strip()
            vol = int(str(row[1]).strip())
            if not inst:
                raise ValueError(f"{path} line {line_no}: instrument is empty")
            if vol <= 0:
                raise ValueError(f"{path} line {line_no}: volume must be a positive integer")
            claims[inst] = claims.get(inst, 0) + vol
    return claims


def save_positions_csv(path: str, claims: Dict[str, int]) -> None:
    rows = sorted(
        ((inst, int(vol)) for inst, vol in (claims or {}).items() if int(vol) > 0),
        key=lambda x: x[0],
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['instrument', 'volume'])
    for inst, vol in rows:
        writer.writerow([inst, vol])
    atomic_write_text(path, buf.getvalue())


def _fill_volume_delta(direction: str, offset: str, traded: int) -> int:
    """开仓买入 +手数，平仓卖出 -手数；其余不变。"""
    from pairtrade.constants import DIRECTION_BUY, DIRECTION_SELL, OFFSET_CLOSE, OFFSET_OPEN

    if traded <= 0:
        return 0
    if direction == DIRECTION_BUY and offset == OFFSET_OPEN:
        return int(traded)
    if direction == DIRECTION_SELL and offset == OFFSET_CLOSE:
        return -int(traded)
    return 0


def read_claim_volume(config: dict, instrument: str) -> int:
    """当前 on-disk 宽跨认领手数（>=0）；文件不存在视为 0。

    读取失败时抛出（与 :func:`apply_fill_to_csv` 一致，绝不静默当 0），供成交
    入账记录 pre_volume，以及自愈器比对 on-disk CSV。
    """
    inst = str(instrument or '').strip()
    if not inst:
        return 0
    path = positions_csv_path(config)
    if not os.path.isfile(path):
        return 0
    claims = load_positions_csv(path)
    return int(claims.get(inst, 0))


def apply_fill_to_csv(
    config: dict,
    instrument: str,
    direction: str,
    offset: str,
    traded: int,
    logger=None,
) -> Dict[str, int]:
    """宽跨成交后增量更新 strangle_positions.csv，返回更新后的认领表。"""
    delta = _fill_volume_delta(direction, offset, traded)
    if delta == 0 or not instrument:
        return load_positions_csv(positions_csv_path(config)) if os.path.isfile(
            positions_csv_path(config)) else {}
    path = positions_csv_path(config)
    claims: Dict[str, int] = {}
    if os.path.isfile(path):
        # 读已有认领失败时绝不能用空表续写——那会把其它合约的认领整表抹掉。
        # 抛出让上层流水停在 pending（触发 journal_halt 止血），原 CSV 原样保留。
        try:
            claims = load_positions_csv(path)
        except Exception as e:
            if logger:
                logger.error(
                    f"[宽跨持仓] 读取 CSV 失败，拒绝重建以保护既有认领: {e} ({path})"
                )
            raise
    inst = str(instrument).strip()
    new_vol = int(claims.get(inst, 0)) + delta
    if new_vol <= 0:
        claims.pop(inst, None)
    else:
        claims[inst] = new_vol
    save_positions_csv(path, claims)
    if logger:
        logger.info(
            f"[宽跨持仓] CSV 更新 {inst}: {'+' if delta > 0 else ''}{delta} "
            f"→ {max(new_vol, 0)} ({path})"
        )
    return claims


def collect_tracked_instruments(ledger, csv_claims: Dict[str, int], conn=None, config=None) -> Set[str]:
    """CSV / 账本 / 在途宽跨单涉及的合约集合。"""
    insts: Set[str] = set(csv_claims or {})
    for p in ledger.list_positions():
        if p.get('status') not in ('open', 'closing'):
            continue
        for key in ('call_instrument', 'put_instrument'):
            leg = p.get(key)
            if leg:
                insts.add(str(leg).strip())
    for item in ledger.list_unmatched_legs():
        leg = item.get('leg') or {}
        for key in ('inst', 'filled_instrument', 'call_inst', 'put_inst'):
            val = item.get(key) if key != 'inst' else leg.get('inst')
            if val:
                insts.add(str(val).strip())
    if conn is not None and config is not None:
        from auto_strategy_order_ref import is_strangle_order_ref

        pending = getattr(conn, 'pending_orders', None) or {}
        for ref_key, order in pending.items():
            try:
                ref = int(getattr(order, 'order_ref', None) or ref_key)
            except (TypeError, ValueError):
                ref = 0
            if not is_strangle_order_ref(ref, config):
                continue
            inst = ''
            if hasattr(order, 'instrument_id'):
                inst = (order.instrument_id or '').strip()
            elif isinstance(order, dict):
                inst = (order.get('instrument') or order.get('InstrumentID') or '').strip()
            if inst:
                insts.add(inst)
    return {i for i in insts if i}


def _ctp_strangle_long_volumes(conn, trade_symbols: Set[str]) -> Dict[str, int]:
    from auto_connection import extract_symbol_prefix

    out: Dict[str, int] = {}
    try:
        positions = conn.query_positions_sync(timeout=10) or []
    except Exception:
        return out
    for pos in positions:
        direction = pos.get('direction') or pos.get('PosiDirection', '')
        if direction not in ('2', 2, 'LONG'):
            continue
        inst = (pos.get('instrument') or pos.get('InstrumentID') or '').strip()
        if not inst:
            continue
        sym = extract_symbol_prefix(inst)
        if sym not in trade_symbols:
            continue
        vol = int(pos.get('volume') or pos.get('Position') or pos.get('position') or 0)
        if vol > 0:
            out[inst] = out.get(inst, 0) + vol
    return out


def sync_csv_from_ctp(
    conn,
    ledger,
    config: dict,
    trade_symbols: Set[str],
    logger=None,
) -> bool:
    """
    Deprecated: use ``strangle_fill_sync.sync_csv_from_strangle_trades`` (strict
    OrderRef filter). This shim forwards to the new path for backwards compat
    and emits ``DeprecationWarning`` so external callers surface the migration.
    """
    import warnings

    warnings.warn(
        'sync_csv_from_ctp is deprecated; '
        'use strangle_fill_sync.sync_csv_from_strangle_trades instead.',
        DeprecationWarning,
        stacklevel=2,
    )
    del trade_symbols
    try:
        from strangle_fill_sync import sync_csv_from_strangle_trades
        n = sync_csv_from_strangle_trades(conn, ledger, config, logger)
        return n > 0
    except Exception as e:
        if logger:
            logger.warning(f"[宽跨持仓] sync_csv_from_ctp 转发失败: {e}")
        return False


def sync_strangle_leg_claims(
    ledger,
    config: dict = None,
    csv_path: str = None,
    logger=None,
) -> int:
    """从 CSV 同步 leg_claims；空表、仅有表头或文件不存在均视为无持仓。"""
    csv_path = csv_path or positions_csv_path(config)
    if not os.path.isfile(csv_path):
        claims = {}
        if logger:
            logger.info(f"[宽跨持仓] 未找到 {csv_path}，视为无持仓")
    else:
        claims = load_positions_csv(csv_path)
        if logger:
            if claims:
                logger.info(f"[宽跨持仓] 已从 {csv_path} 同步 {len(claims)} 个合约")
            else:
                logger.info(f"[宽跨持仓] {csv_path} 为空，无持仓")

    ledger.set_leg_claims(claims)
    return len(claims)


def import_csv_to_ledger(
    csv_path: str,
    ledger_path: str,
    preserve_runtime: bool = True,
) -> int:
    from straggle_ledger import StrangleLedger

    if os.path.isfile(csv_path):
        claims = load_positions_csv(csv_path)
    else:
        claims = {}

    ledger = StrangleLedger(ledger_path)
    ledger.set_leg_claims(claims)

    if not preserve_runtime:
        with ledger._lock:
            ledger._data['positions'] = []
            ledger._data['unmatched_legs'] = []
            ledger._data['cooldowns'] = []
            ledger._data['daily_groups'] = {}
            ledger._data['daily_buy_amount'] = {}
            ledger._data['open_halted'] = False
            ledger._data['open_halt_reason'] = ''
            ledger._save()
    return len(claims)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description='Import strangle_positions.csv (instrument, volume; empty file = no positions)',    )
    parser.add_argument(
        '-c', '--csv',
        default=_default_csv(),
        help='positions CSV (default: data/strangle_positions.csv)',    )
    parser.add_argument(
        '--reset-runtime',
        action='store_true',
        help='also clear positions/unmatched runtime fields',
    )
    args = parser.parse_args(argv)

    config = load_merged_config()
    ledger_path = config['strangle']['ledger_path']
    n = import_csv_to_ledger(
        args.csv, ledger_path, preserve_runtime=not args.reset_runtime,
    )
    if n == 0:
        print(f"no positions -> {ledger_path}")
    else:
        print(f"imported {n} contracts -> {ledger_path}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
