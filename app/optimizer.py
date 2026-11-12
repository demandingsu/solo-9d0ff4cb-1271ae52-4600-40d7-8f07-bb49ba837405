"""整包采购组合优化。

在每种包装的可购包数（库存）约束内，求满足需求片数且总价最低的整包组合；
总价相同时，优先选择超量（实到片数 - 需求片数）更少的组合。

算法：二进制拆分把有界背包转为 0/1 背包，对"恰好达到 j 片"做最小费用 DP，
再在所有 j >= 需求 的状态中按 (总价, 总片数) 字典序取最优。

上界说明：若某可行组合总片数 T >= 需求 + 最大包装片数，则必可去掉一包仍满足
需求，且总价不增、超量更小，因此最优解总片数一定 < 需求 + 最大包装片数，
DP 状态只需覆盖到 cap = 需求 + 最大包装片数。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# DP 状态数上限（需求 + 最大包装片数），防止异常输入导致内存耗尽
MAX_DP_STATES = 20_000_000


@dataclass
class Offer:
    """一种可购包装。"""

    pack_size: int    # 每包片数
    price_fen: int    # 含税包价，单位：分（避免浮点误差）
    stock_packs: int  # 可购包数（库存）


@dataclass
class Plan:
    """最优采购方案。"""

    packs: list[int]      # 与传入 offers 一一对应的采购包数
    total_pieces: int     # 实到总片数
    total_price_fen: int  # 总价，单位：分


def optimize(offers: list[Offer], demand: int) -> Optional[Plan]:
    """返回满足需求且总价最低的整包组合；库存不足时返回 None。"""
    if demand <= 0:
        return Plan(packs=[0] * len(offers), total_pieces=0, total_price_fen=0)

    # 只保留真正可买的包装（有库存、每包片数为正）
    indexed = [(i, o) for i, o in enumerate(offers) if o.pack_size > 0 and o.stock_packs > 0]
    if not indexed:
        return None

    total_available = sum(o.pack_size * o.stock_packs for _, o in indexed)
    if total_available < demand:
        return None  # 库存不足

    max_size = max(o.pack_size for _, o in indexed)
    cap = demand + max_size  # 最优解总片数严格小于 cap
    if cap > MAX_DP_STATES:
        raise ValueError(f"需求片数过大（{demand}），超出求解范围")

    # 二进制拆分：stock 包 -> 1, 2, 4, ... 余数 个 0/1 物品
    chunks: list[tuple[int, int, int]] = []  # (indexed 下标, 片数, 费用/分)
    for pos, (_, o) in enumerate(indexed):
        remaining = o.stock_packs
        k = 1
        while remaining > 0:
            take = min(k, remaining)
            chunks.append((pos, take * o.pack_size, take * o.price_fen))
            remaining -= take
            k <<= 1

    # dp[j] = 恰好 j 片的最低费用（分）；None 表示不可达
    dp: list[Optional[int]] = [None] * (cap + 1)
    dp[0] = 0
    parent: list[Optional[tuple[int, int]]] = [None] * (cap + 1)  # j -> (前驱 j, chunk 下标)

    for ci, (_, pieces, cost) in enumerate(chunks):
        if pieces > cap:
            continue  # 单块就超过上界，最优解不会用到
        for j in range(cap - pieces, -1, -1):  # 倒序保证每块只用一次
            prev = dp[j]
            if prev is None:
                continue
            nj = j + pieces
            nc = prev + cost
            if dp[nj] is None or nc < dp[nj]:
                dp[nj] = nc
                parent[nj] = (j, ci)

    # 在所有满足需求的状态中，按 (总价, 总片数) 取最优：同价时超量更少者优先
    best_j: Optional[int] = None
    best_key: Optional[tuple[int, int]] = None
    for j in range(demand, cap + 1):
        cost = dp[j]
        if cost is None:
            continue
        key = (cost, j)
        if best_key is None or key < best_key:
            best_key = key
            best_j = j

    if best_j is None or best_key is None:
        return None

    # 回溯还原每种包装的采购包数
    packs_usable = [0] * len(indexed)
    j = best_j
    while j:
        prev_j, ci = parent[j]  # type: ignore[misc]
        pos, pieces, _ = chunks[ci]
        packs_usable[pos] += pieces // indexed[pos][1].pack_size
        j = prev_j

    packs = [0] * len(offers)
    for pos, (orig_i, _) in enumerate(indexed):
        packs[orig_i] = packs_usable[pos]

    return Plan(packs=packs, total_pieces=best_j, total_price_fen=best_key[0])
