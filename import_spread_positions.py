"""Import spread leg claims from CSV (instrument, signed volume)."""

import argparse
import csv
import io
import os
from typing import Dict

from atomic_io import atomic_write_text
from env_utils import is_config_abs_path
from merged_config import load_merged_config

_CSV_HEADERS = frozenset({
    'instrument', 'volume',
    '合约', '持仓', '期权代码', '持仓手数',
})


def _project_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _default_csv() -> str:
    return os.path.join(_project_dir(), 'data', 'spread_positions.csv')


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


def spread_positions_csv_path(config: dict = None) -> str:
    config = config or {}
    dual = config.get('dual_strategy') or {}
    path = dual.get(
        'spread_positions_csv',
        os.path.join(_project_dir(), 'data', 'spread_positions.csv'),
    )
    if not is_config_abs_path(path):
        path = os.path.join(_project_dir(), path)
    return path


def load_spread_positions_csv(path: str) -> Dict[str, int]:
    """Empty file or header-only -> {}."""
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
            if vol == 0:
                raise ValueError(f"{path} line {line_no}: volume must be non-zero")
            claims[inst] = claims.get(inst, 0) + vol
    return claims


def save_spread_positions_csv(path: str, claims: Dict[str, int]) -> None:
    rows = sorted(
        ((inst, int(vol)) for inst, vol in (claims or {}).items() if int(vol) != 0),
        key=lambda x: x[0],
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['instrument', 'volume'])
    for inst, vol in rows:
        writer.writerow([inst, vol])
    atomic_write_text(path, buf.getvalue())


def spread_fill_delta(direction: str, offset: str, traded: int) -> int:
    """Signed net-position delta for spread leg claims."""
    from pairtrade.constants import DIRECTION_BUY, DIRECTION_SELL, OFFSET_CLOSE, OFFSET_OPEN

    if traded <= 0:
        return 0
    if direction == DIRECTION_BUY and offset == OFFSET_OPEN:
        return int(traded)
    if direction == DIRECTION_SELL and offset == OFFSET_OPEN:
        return -int(traded)
    if direction == DIRECTION_SELL and offset == OFFSET_CLOSE:
        return -int(traded)
    if direction == DIRECTION_BUY and offset == OFFSET_CLOSE:
        return int(traded)
    return 0


def read_spread_claim_volume(config: dict, instrument: str) -> int:
    """当前 on-disk 价差认领（signed）；文件不存在视为 0。

    读取失败时抛出（与 :func:`apply_fill_to_spread_csv` 一致，绝不静默当 0），
    供成交入账记录 pre_volume，以及自愈器比对 on-disk CSV。
    """
    inst = str(instrument or '').strip()
    if not inst:
        return 0
    path = spread_positions_csv_path(config)
    if not os.path.isfile(path):
        return 0
    claims = load_spread_positions_csv(path)
    return int(claims.get(inst, 0))


def apply_fill_to_spread_csv(
    config: dict,
    instrument: str,
    direction: str,
    offset: str,
    traded: int,
    logger=None,
) -> Dict[str, int]:
    delta = spread_fill_delta(direction, offset, traded)
    if delta == 0 or not instrument:
        path = spread_positions_csv_path(config)
        return load_spread_positions_csv(path) if os.path.isfile(path) else {}

    path = spread_positions_csv_path(config)
    claims: Dict[str, int] = {}
    if os.path.isfile(path):
        # 读已有认领失败时绝不能用空表续写——那会把其它合约的认领整表抹掉。
        # 抛出让上层流水停在 pending（触发 journal_halt 止血），原 CSV 原样保留。
        try:
            claims = load_spread_positions_csv(path)
        except Exception as e:
            if logger:
                logger.error(
                    f"[价差持仓] 读取 CSV 失败，拒绝重建以保护既有认领: {e} ({path})"
                )
            raise
    inst = str(instrument).strip()
    new_vol = int(claims.get(inst, 0)) + delta
    if new_vol == 0:
        claims.pop(inst, None)
    else:
        claims[inst] = new_vol
    save_spread_positions_csv(path, claims)
    if logger:
        logger.info(
            f"[价差持仓] CSV 更新 {inst}: {'+' if delta > 0 else ''}{delta} "
            f"→ {new_vol} ({path})"
        )
    return claims


def sync_spread_leg_claims(
    store,
    config: dict = None,
    csv_path: str = None,
    logger=None,
) -> int:
    csv_path = csv_path or spread_positions_csv_path(config)
    if not os.path.isfile(csv_path):
        claims = {}
        if logger:
            logger.info(f"[价差持仓] 未找到 {csv_path}，视为无持仓")
    else:
        claims = load_spread_positions_csv(csv_path)
        if logger:
            if claims:
                logger.info(f"[价差持仓] 已从 {csv_path} 同步 {len(claims)} 个合约")
            else:
                logger.info(f"[价差持仓] {csv_path} 为空，无持仓")
    store.set_leg_claims(claims)
    return len(claims)


def _load_config_with_tradeinfo():
    from merged_tradeinfo import load_dual_tradeinfo

    config = load_merged_config()
    spread_info, strangle_info, _combined = load_dual_tradeinfo(config)
    config['spread_tradeinfo'] = spread_info
    config['strangle_tradeinfo'] = strangle_info
    return config


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description='Import spread_positions.csv (instrument, signed volume)',
    )
    parser.add_argument(
        '-c', '--csv',
        default=_default_csv(),
        help='positions CSV (default: data/spread_positions.csv)',
    )
    parser.add_argument(
        '--audit',
        action='store_true',
        help='audit spread_positions.csv vs tradeinfo (no write)',
    )
    parser.add_argument(
        '--repair-journal',
        action='store_true',
        help='remove spread journal lines outside spread tradeinfo',
    )
    args = parser.parse_args(argv)

    config = _load_config_with_tradeinfo()

    if args.audit:
        from spread_claims_guard import audit_spread_claims, format_spread_claims_audit

        path = args.csv
        claims = load_spread_positions_csv(path) if os.path.isfile(path) else {}
        issues = audit_spread_claims(
            claims, config.get('spread_tradeinfo') or [], conn=None, ctp_signed=None,
        )
        text = format_spread_claims_audit(issues)
        if text:
            print(text)
        elif claims:
            print(f'【价差认领审计】{path} 共 {len(claims)} 条，未发现 tradeinfo 层面问题')
        else:
            print(f'【价差认领审计】{path} 为空')
        return 1 if issues else 0

    if args.repair_journal:
        from spread_claims_guard import repair_spread_trade_journals

        removed, kept = repair_spread_trade_journals(config, conn=None, logger=None)
        print(f'[价差 journal] 已清理：移除 {removed} 条，保留 {kept} 条')
        return 0

    from spread_ledger import SpreadLegStore

    if os.path.isfile(args.csv):
        claims = load_spread_positions_csv(args.csv)
    else:
        claims = {}

    store = SpreadLegStore()
    store.set_leg_claims(claims)
    out_path = spread_positions_csv_path(config)
    save_spread_positions_csv(out_path, store.list_leg_claims())
    if not claims:
        print(f"no positions -> {out_path}")
    else:
        print(f"imported {len(claims)} contracts -> {out_path}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
