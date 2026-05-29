"""崩溃后 journal pending 自愈器。

背景
----
成交入账走 ``pending → 写 CSV/账本 → applied`` 两阶段（见 ``spread_fill_sync`` /
``strangle_fill_sync`` / ``fill_ledger``）。进程在 ``pending`` 与 ``applied`` 之间
被杀，会留下 unresolved pending：

- ``scan_unresolved_pending`` 检测到后，主循环置 ``_journal_halt_open`` 长期禁新开；
- ``load_applied_keys(include_pending=True)`` 把 pending 当成已处理，阻断 CTP 回放。

历史上只能人工修 journal。本模块在 **不重复入账、不丢账** 的前提下自动收尾。

幂等判定
--------
仅凭 journal 无法区分"崩在写 CSV 前"还是"写 CSV 后、写 applied 前"。因此 pending
行额外记录了 ``pre_volume`` / ``post_volume``（成交前/后的 on-disk 净额）。自愈时读
当前 on-disk CSV ``cur``：

- ``cur == post`` → CSV 已体现本笔，只补 applied 标记（不再改 CSV）；
- ``cur == pre``  → CSV 未体现本笔，先把 CSV 落到 post（原子写）再补 applied；
- 其它             → 模棱两可，**跳过保持 halt 交人工**（绝不猜测）。

为彻底杜绝"同合约多笔 pending 的 pre/post 巧合误判"，**同一 instrument 若有 >1 条
unresolved pending（多次崩溃叠加）一律判歧义跳过**。journal_lock 把每笔成交整体串
行化，单次崩溃每个 journal 至多遗留 1 条 pending，故正常单次崩溃可自愈。

崩溃安全的写序
--------------
``apply`` 分支先 **save CSV（原子）再补 applied 标记**：
- 若在 save 后、补标记前再次崩溃 → 下次 ``cur == post`` → 判 ``already`` → 补标记；
- 若在 save 前崩溃 → 下次 ``cur == pre`` → 重新 apply。
两种中断都幂等收敛，不会重复入账或丢账。

fill_ledger（append-only 分析日志、非持仓真相）
----------------------------------------------
``fill_ledger.csv`` 仅用于滑点/成交分析，不参与持仓/保证金/日限判定，且行内无
dedupe 键不便成员判定。自愈仅补 applied 标记（**不重复 append**，避免脏分析行）；
个别分析行可能缺失，不影响交易。
"""

from __future__ import annotations

from datetime import date


def _heal_net_csv_journal(
    journal_file: str,
    config: dict,
    *,
    load_claims,
    save_claims,
    set_runtime_claims,
    remove_when_post,
    logger,
    tag: str,
) -> dict:
    """Heal one net-accumulator CSV journal (spread or strangle).

    ``load_claims() -> dict`` / ``save_claims(dict)`` operate on the full claim
    table; ``remove_when_post(post:int) -> bool`` decides removal vs set;
    ``set_runtime_claims(dict)`` (optional) syncs the in-memory store/ledger.
    """
    from trade_journal import append_journal, scan_unresolved_pending_rows
    from trade_journal_lock import journal_lock

    result = {'healed': 0, 'ambiguous': 0, 'errors': 0}
    with journal_lock(journal_file):
        rows = scan_unresolved_pending_rows(journal_file, config)
        if not rows:
            return result

        by_inst: dict = {}
        for row in rows:
            inst = str(row.get('instrument') or '').strip()
            by_inst.setdefault(inst, []).append(row)

        try:
            claims = load_claims()
        except Exception as e:
            if logger:
                logger.error(
                    f'[自愈:{tag}] 读取认领表失败，保持 halt 交人工: {e}'
                )
            result['errors'] = len(rows)
            return result

        any_healed = False
        for inst, group in by_inst.items():
            if not inst or len(group) != 1:
                result['ambiguous'] += len(group)
                if logger:
                    logger.warning(
                        f'[自愈:{tag}] {inst or "<空合约>"} 有 {len(group)} 条 '
                        'unresolved pending（无法唯一定位），保持 halt 交人工'
                    )
                continue
            row = group[0]
            if 'pre_volume' not in row or 'post_volume' not in row:
                result['ambiguous'] += 1
                if logger:
                    logger.warning(
                        f'[自愈:{tag}] {inst} pending 缺 pre/post（旧格式），'
                        '无法幂等判定，保持 halt 交人工'
                    )
                continue
            try:
                pre = int(row['pre_volume'])
                post = int(row['post_volume'])
            except (TypeError, ValueError):
                result['ambiguous'] += 1
                continue

            cur = int(claims.get(inst, 0))
            if cur == post:
                verdict = 'already'
            elif cur == pre:
                verdict = 'apply'
            else:
                result['ambiguous'] += 1
                if logger:
                    logger.warning(
                        f'[自愈:{tag}] {inst} 歧义 cur={cur} pre={pre} '
                        f'post={post}，保持 halt 交人工'
                    )
                continue

            if verdict == 'apply':
                # 先持久化 CSV（原子），再补 applied 标记 —— 保证崩溃安全写序。
                if remove_when_post(post):
                    claims.pop(inst, None)
                else:
                    claims[inst] = post
                try:
                    save_claims(claims)
                except Exception as e:
                    if logger:
                        logger.error(
                            f'[自愈:{tag}] {inst} 写认领表失败，跳过: {e}'
                        )
                    result['errors'] += 1
                    continue
                any_healed = True

            append_journal(journal_file, {
                'dedupe_key': row.get('dedupe_key'),
                'trade_id': row.get('trade_id', ''),
                'instrument': inst,
                'pre_volume': pre,
                'post_volume': post,
                'journal_state': 'applied',
                'recovered_by': 'self_heal',
                'recovered_verdict': verdict,
                'applied_on': date.today().isoformat(),
            }, config)
            result['healed'] += 1
            if logger:
                logger.warning(
                    f'[自愈:{tag}] {inst} {verdict}（pre={pre} post={post} '
                    f'cur={cur}），已补 applied 标记'
                )

        if any_healed and set_runtime_claims is not None:
            try:
                set_runtime_claims(claims)
            except Exception as e:
                if logger:
                    logger.debug(f'[自愈:{tag}] 同步内存认领失败: {e}')
    return result


def recover_spread_pending(config: dict, store=None, logger=None) -> dict:
    """Heal stuck spread-journal pending rows (signed net claims)."""
    from import_spread_positions import (
        load_spread_positions_csv,
        save_spread_positions_csv,
        spread_positions_csv_path,
    )
    from spread_fill_sync import _journal_path

    import os

    path = spread_positions_csv_path(config)

    def _load():
        return load_spread_positions_csv(path) if os.path.isfile(path) else {}

    return _heal_net_csv_journal(
        _journal_path(config),
        config,
        load_claims=_load,
        save_claims=lambda c: save_spread_positions_csv(path, c),
        set_runtime_claims=(store.set_leg_claims if store is not None else None),
        remove_when_post=lambda post: post == 0,
        logger=logger,
        tag='spread',
    )


def recover_strangle_pending(config: dict, ledger=None, logger=None) -> dict:
    """Heal stuck strangle-journal pending rows (non-negative net claims)."""
    from import_strangle_positions import (
        load_positions_csv,
        positions_csv_path,
        save_positions_csv,
    )
    from strangle_fill_sync import _journal_path

    import os

    path = positions_csv_path(config)

    def _load():
        return load_positions_csv(path) if os.path.isfile(path) else {}

    return _heal_net_csv_journal(
        _journal_path(config),
        config,
        load_claims=_load,
        save_claims=lambda c: save_positions_csv(path, c),
        set_runtime_claims=(ledger.set_leg_claims if ledger is not None else None),
        remove_when_post=lambda post: post <= 0,
        logger=logger,
        tag='strangle',
    )


def recover_fill_ledger_pending(config: dict, logger=None) -> dict:
    """Resolve stuck fill-ledger pending rows by writing an applied marker.

    fill_ledger.csv is analytics-only (not position truth); we do NOT re-append
    the row (that would risk duplicate analytics lines and there is no per-row
    dedupe key in the CSV). A missing analytics row has no trading impact.
    """
    from fill_ledger import fill_ledger_journal_path
    from trade_journal import append_journal, scan_unresolved_pending_rows
    from trade_journal_lock import journal_lock

    journal_file = fill_ledger_journal_path(config)
    healed = 0
    with journal_lock(journal_file):
        rows = scan_unresolved_pending_rows(journal_file, config)
        for row in rows:
            append_journal(journal_file, {
                'dedupe_key': row.get('dedupe_key'),
                'trade_id': row.get('trade_id', ''),
                'instrument': row.get('instrument', ''),
                'journal_state': 'applied',
                'recovered_by': 'self_heal',
                'recovered_note': 'fill_ledger_analytics_only',
                'applied_on': date.today().isoformat(),
            }, config)
            healed += 1
    if healed and logger:
        logger.warning(
            f'[自愈:fill_ledger] 补 {healed} 条 applied 标记（分析日志，不重复 '
            'append；个别分析行可能缺失，不影响持仓/风控/日限）'
        )
    return {'healed': healed, 'ambiguous': 0, 'errors': 0}


def recover_all_pending(
    config: dict,
    store=None,
    ledger=None,
    logger=None,
) -> dict:
    """Run all three healers; return aggregate ``{healed, ambiguous, errors}``."""
    total = {'healed': 0, 'ambiguous': 0, 'errors': 0}
    for fn in (
        lambda: recover_spread_pending(config, store=store, logger=logger),
        lambda: recover_strangle_pending(config, ledger=ledger, logger=logger),
        lambda: recover_fill_ledger_pending(config, logger=logger),
    ):
        try:
            r = fn()
        except Exception as e:
            if logger:
                logger.error(f'[自愈] 子流程异常: {e}', exc_info=True)
            total['errors'] += 1
            continue
        for k in total:
            total[k] += int(r.get(k, 0) or 0)
    return total
