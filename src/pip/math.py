from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN
from typing import Iterable

CENT = Decimal("0.01")
ONE = Decimal("1")


def D(value) -> Decimal:
    return Decimal(str(value))


def qcent(value: Decimal) -> Decimal:
    return value.quantize(CENT)


def ceil_cent(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_CEILING)


def kalshi_fee(contracts: int, price: Decimal, rate: Decimal = Decimal("0.07")) -> Decimal:
    if contracts <= 0:
        return Decimal("0")
    p = max(Decimal("0"), min(ONE, price))
    raw = rate * D(contracts) * p * (ONE - p)
    return ceil_cent(raw)


def conservative_maker_fee(contracts: int, price: Decimal) -> Decimal:
    return kalshi_fee(contracts, price, Decimal("0.0175"))


def max_contract_count(equity: Decimal, price: Decimal, position_pct: float, order_pct: float) -> int:
    if equity <= 0 or price <= 0:
        return 0
    cap = min(equity * D(position_pct), equity * D(order_pct))
    return int((cap / price).to_integral_value(rounding=ROUND_DOWN))


def notional(count: int, price: Decimal) -> Decimal:
    return D(count) * price


def side_to_v2_entry(side: str, contract_price: Decimal) -> tuple[str, Decimal]:
    """Map a long YES/NO entry to Kalshi V2 book side + YES-side quoted price."""
    s = side.lower()
    if s == "yes":
        return "bid", contract_price
    if s == "no":
        return "ask", ONE - contract_price
    raise ValueError("side must be yes or no")


def side_to_v2_exit(side: str, contract_price: Decimal) -> tuple[str, Decimal]:
    """Map closing a long YES/NO position to Kalshi V2 book side + YES-side price."""
    s = side.lower()
    if s == "yes":
        return "ask", contract_price
    if s == "no":
        return "bid", ONE - contract_price
    raise ValueError("side must be yes or no")


def sigmoid(x: float) -> float:
    x = max(-30.0, min(30.0, x))
    return 1.0 / (1.0 + math.exp(-x))


@dataclass(frozen=True)
class TradeEconomics:
    count: int
    entry_price: Decimal
    target_price: Decimal
    stop_price: Decimal
    entry_fee: Decimal
    target_exit_fee: Decimal
    stop_exit_fee: Decimal
    net_win: Decimal
    net_loss: Decimal
    break_even_probability: float


def trade_economics(
    count: int,
    entry_price: Decimal,
    target_price: Decimal,
    stop_price: Decimal,
    entry_is_maker: bool,
    target_exit_is_maker: bool = True,
) -> TradeEconomics:
    maker = conservative_maker_fee
    taker = kalshi_fee
    entry_fee = maker(count, entry_price) if entry_is_maker else taker(count, entry_price)
    target_fee = maker(count, target_price) if target_exit_is_maker else taker(count, target_price)
    stop_fee = taker(count, stop_price)
    gross_win = D(count) * (target_price - entry_price)
    gross_loss = D(count) * (stop_price - entry_price)
    net_win = gross_win - entry_fee - target_fee
    net_loss = gross_loss - entry_fee - stop_fee
    loss_mag = max(Decimal("0"), -net_loss)
    denom = max(Decimal("0.000001"), net_win + loss_mag)
    break_even = float(loss_mag / denom) if net_win > 0 else 1.0
    return TradeEconomics(
        count=count,
        entry_price=entry_price,
        target_price=target_price,
        stop_price=stop_price,
        entry_fee=entry_fee,
        target_exit_fee=target_fee,
        stop_exit_fee=stop_fee,
        net_win=net_win,
        net_loss=net_loss,
        break_even_probability=break_even,
    )


def expected_value(probability_target: float, economics: TradeEconomics) -> Decimal:
    p = D(max(0.0, min(1.0, probability_target)))
    return (p * economics.net_win) + ((ONE - p) * economics.net_loss)


def book_imbalance(bid_sizes: Iterable[float], opposing_sizes: Iterable[float]) -> float:
    b = max(0.0, sum(float(x) for x in bid_sizes))
    o = max(0.0, sum(float(x) for x in opposing_sizes))
    total = b + o
    if total <= 0:
        return 0.0
    return (b - o) / total
