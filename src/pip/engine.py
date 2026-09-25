from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from .config import PipConfig
from .kalshi import PipKalshiClient, PipKalshiError, fp
from .math import (
    CENT,
    D,
    book_imbalance,
    conservative_maker_fee,
    expected_value,
    kalshi_fee,
    max_contract_count,
    side_to_v2_entry,
    side_to_v2_exit,
    trade_economics,
)
from .model import PipSignalModel
from .store import PipStore, utcnow


@dataclass(frozen=True)
class SideQuote:
    ticker: str
    title: str
    side: str
    bid: Decimal
    ask: Decimal
    bid_size: float
    ask_size: float
    volume_24h: float
    close_time: str | None

    @property
    def spread(self) -> Decimal:
        return max(Decimal("0"), self.ask - self.bid)


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def minutes_until(value: str | None) -> float | None:
    dt = parse_time(value)
    if not dt:
        return None
    return (dt - datetime.now(timezone.utc)).total_seconds() / 60.0


def price_field(market: dict[str, Any], name: str) -> Decimal:
    dollars = market.get(f"{name}_dollars")
    if dollars not in (None, ""):
        return fp(dollars)
    raw = market.get(name)
    if raw in (None, ""):
        return Decimal("0")
    d = D(raw)
    return d / D(100) if d > 1 else d


def size_field(market: dict[str, Any], name: str) -> float:
    raw = market.get(f"{name}_fp", market.get(name, 0))
    try:
        return float(raw or 0)
    except (TypeError, ValueError):
        return 0.0


def quotes_for_market(market: dict[str, Any]) -> list[SideQuote]:
    ticker = str(market.get("ticker") or "")
    if not ticker:
        return []
    title = str(market.get("title") or market.get("subtitle") or ticker)
    vol = size_field(market, "volume_24h") or size_field(market, "volume")
    close_time = market.get("close_time")
    yes = SideQuote(
        ticker=ticker,
        title=title,
        side="yes",
        bid=price_field(market, "yes_bid"),
        ask=price_field(market, "yes_ask"),
        bid_size=size_field(market, "yes_bid_size"),
        ask_size=size_field(market, "yes_ask_size"),
        volume_24h=vol,
        close_time=close_time,
    )
    # Kalshi market payloads expose YES-side sizes. NO bid liquidity is the
    # complementary YES ask liquidity; NO ask liquidity is complementary YES bid.
    no = SideQuote(
        ticker=ticker,
        title=title,
        side="no",
        bid=price_field(market, "no_bid"),
        ask=price_field(market, "no_ask"),
        bid_size=size_field(market, "yes_ask_size"),
        ask_size=size_field(market, "yes_bid_size"),
        volume_24h=vol,
        close_time=close_time,
    )
    return [yes, no]


def _book_levels(orderbook_response: dict[str, Any]) -> tuple[list[tuple[Decimal, float]], list[tuple[Decimal, float]]]:
    ob = orderbook_response.get("orderbook") or orderbook_response.get("orderbook_fp") or orderbook_response
    yes_raw = ob.get("yes_dollars") or ob.get("yes") or []
    no_raw = ob.get("no_dollars") or ob.get("no") or []

    def clean(levels):
        out = []
        for level in levels:
            if not isinstance(level, (list, tuple)) or len(level) < 2:
                continue
            try:
                p = D(level[0])
                if p > 1:
                    p /= D(100)
                out.append((p, float(level[1])))
            except Exception:
                continue
        return out

    return clean(yes_raw), clean(no_raw)


def refresh_quote_from_orderbook(quote: SideQuote, orderbook_response: dict[str, Any]) -> SideQuote:
    """Replace top-of-book prices/sizes with the authenticated live orderbook."""
    yes_levels, no_levels = _book_levels(orderbook_response)
    yes_levels.sort(key=lambda x: x[0], reverse=True)
    no_levels.sort(key=lambda x: x[0], reverse=True)
    yes_bid = yes_levels[0] if yes_levels else None
    no_bid = no_levels[0] if no_levels else None

    if quote.side == "yes":
        bid = yes_bid[0] if yes_bid else quote.bid
        ask = (Decimal("1") - no_bid[0]) if no_bid else quote.ask
        bid_size = yes_bid[1] if yes_bid else quote.bid_size
        ask_size = no_bid[1] if no_bid else quote.ask_size
    else:
        bid = no_bid[0] if no_bid else quote.bid
        ask = (Decimal("1") - yes_bid[0]) if yes_bid else quote.ask
        bid_size = no_bid[1] if no_bid else quote.bid_size
        ask_size = yes_bid[1] if yes_bid else quote.ask_size

    return SideQuote(
        ticker=quote.ticker,
        title=quote.title,
        side=quote.side,
        bid=bid,
        ask=ask,
        bid_size=bid_size,
        ask_size=ask_size,
        volume_24h=quote.volume_24h,
        close_time=quote.close_time,
    )


def side_depths(side: str, quote: SideQuote, orderbook_response: dict[str, Any]) -> tuple[list[float], list[float]]:
    """Near-touch support versus opposing depth, expressed in contract-side prices."""
    yes_levels, no_levels = _book_levels(orderbook_response)
    width = Decimal("0.03")
    if side == "yes":
        support = [q for p, q in yes_levels if p >= quote.bid - width]
        opposing = [q for p, q in no_levels if (Decimal("1") - p) <= quote.ask + width]
    else:
        support = [q for p, q in no_levels if p >= quote.bid - width]
        opposing = [q for p, q in yes_levels if (Decimal("1") - p) <= quote.ask + width]
    return support, opposing


class PipEngine:
    def __init__(self, store: PipStore, kalshi: PipKalshiClient):
        self.store = store
        self.kalshi = kalshi
        self.stop_event = asyncio.Event()
        self.history: dict[tuple[str, str], deque[tuple[float, Decimal]]] = defaultdict(lambda: deque(maxlen=12))
        self.last_scan_at: str | None = None
        self.last_scan_error: str | None = None
        self.last_scan_stats: dict[str, Any] = {}
        self.scanning = False
        self._loop_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._execution_lock = asyncio.Lock()

    async def start_background(self):
        if self._loop_task and not self._loop_task.done():
            return
        self.stop_event.clear()
        self._loop_task = asyncio.create_task(self.run_forever(), name="pip-trading-loop")

    async def stop_background(self):
        self.stop_event.set()
        if self._loop_task:
            try:
                await asyncio.wait_for(self._loop_task, timeout=5)
            except Exception:
                self._loop_task.cancel()

    async def run_forever(self):
        await self.store.event("agent", "Pip trading loop started")
        while not self.stop_event.is_set():
            config = await self.store.load_config()
            # Keep scanning and managing existing positions even while new entries are paused.
            try:
                await self.scan_once()
            except Exception as exc:
                self.last_scan_error = str(exc)
                await self.store.event("scan_error", str(exc), "error")
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=config.scan_interval_seconds)
            except asyncio.TimeoutError:
                pass
        await self.store.event("agent", "Pip trading loop stopped")

    async def equity_snapshot(self, config: PipConfig, quote_map: dict[tuple[str, str], SideQuote] | None = None) -> dict[str, float]:
        positions = await self.store.positions()
        realized = await self.store.realized_pnl()
        if config.mode == "paper":
            exposure = 0.0
            unrealized = 0.0
            reserved = 0.0
            for p in positions:
                qty = int(p["quantity"])
                if p["status"] == "pending_entry":
                    reserved += float(p["intended_entry"]) * qty
                    continue
                entry = float(p["entry_price"] or p["intended_entry"])
                exposure += entry * qty
                bid = p.get("last_bid")
                if quote_map:
                    q = quote_map.get((p["ticker"], p["side"]))
                    if q:
                        bid = float(q.bid)
                if bid is not None:
                    unrealized += (float(bid) - entry) * qty - float(p.get("entry_fee") or 0)
            equity = config.paper_starting_equity + realized + unrealized
            cash = config.paper_starting_equity + realized - exposure - reserved
        else:
            if not self.kalshi.authenticated:
                raise PipKalshiError(f"{config.mode} mode requires Kalshi credentials")
            balance = await self.kalshi.get_balance()
            cash = self._balance_dollars(balance)
            portfolio_value = float(balance.get("portfolio_value") or 0) / 100.0
            exposure = sum(
                float(p.get("entry_price") or p["intended_entry"]) * int(p["quantity"])
                for p in positions if p["status"] != "pending_entry"
            )
            unrealized = 0.0
            for p in positions:
                if p["status"] == "pending_entry":
                    continue
                entry = float(p.get("entry_price") or p["intended_entry"])
                bid = p.get("last_bid")
                if quote_map:
                    q = quote_map.get((p["ticker"], p["side"]))
                    if q:
                        bid = float(q.bid)
                if bid is not None:
                    unrealized += (float(bid) - entry) * int(p["quantity"])
            # Kalshi's portfolio_value includes account positions across exchange indexes.
            equity = cash + portfolio_value
        return {
            "equity": max(0.0, equity),
            "cash": cash,
            "exposure": exposure,
            "realized_pnl": realized,
            "unrealized_pnl": unrealized,
        }

    def _balance_dollars(self, payload: dict[str, Any]) -> float:
        for key in ("balance_dollars", "available_balance_dollars", "cash_balance_dollars"):
            if payload.get(key) not in (None, ""):
                return float(payload[key])
        for key in ("balance", "available_balance", "cash_balance"):
            if payload.get(key) not in (None, ""):
                val = float(payload[key])
                return val / 100.0 if val > 100 else val
        raise PipKalshiError("Could not read account balance from Kalshi response")

    async def scan_once(self) -> list[dict[str, Any]]:
        if self._lock.locked():
            return await self.store.opportunities()
        async with self._lock:
            self.scanning = True
            try:
                config = await self.store.load_config()
                model = await self.store.load_model()
                markets = []
                quote_map: dict[tuple[str, str], SideQuote] = {}
                async for market in self.kalshi.iter_open_markets():
                    markets.append(market)
                    for q in quotes_for_market(market):
                        quote_map[(q.ticker, q.side)] = q

                await self._reconcile_positions(config, quote_map)
                await self._resolve_signals(config, model, quote_map)

                snapshot = await self.equity_snapshot(config, quote_map)
                await self.store.record_equity(**snapshot)
                killed = await self._risk_kill_check(config, snapshot)

                prelim = []
                rejected = defaultdict(int)
                for q in quote_map.values():
                    reason = self._pre_filter_reason(q, config)
                    if reason is None:
                        prelim.append(q)
                    else:
                        rejected[reason] += 1
                prelim.sort(key=lambda q: self._candidate_sort_key(q, config))
                passed_prefilter = len(prelim)
                prelim = prelim[: config.effective_shortlist_size]

                # Authenticate one bulk depth request (max 100 tickers) instead of
                # hammering the exchange with one orderbook request per candidate.
                depth_candidates = prelim[:100]
                depth_tickers = list(dict.fromkeys(q.ticker for q in depth_candidates))
                orderbook_error = None
                try:
                    books = await self.kalshi.get_orderbooks(depth_tickers) if depth_tickers else {}
                except Exception as exc:
                    books = {}
                    orderbook_error = str(exc)
                    await self.store.event("orderbook_error", orderbook_error, "error")

                opportunities = []
                refreshed_quotes: dict[tuple[str, str], SideQuote] = {}
                for q in depth_candidates:
                    book = books.get(q.ticker, {})
                    live_q = refresh_quote_from_orderbook(q, book) if book else q
                    refreshed_quotes[(live_q.ticker, live_q.side)] = live_q
                    # Re-apply gates after the live book refresh; a candidate may have moved.
                    if self._pre_filter_reason(live_q, config) is not None:
                        continue
                    self.history[(live_q.ticker, live_q.side)].append((time.time(), live_q.bid))
                    opp = self._build_opportunity(live_q, book, config, model, snapshot)
                    if opp:
                        opportunities.append(opp)

                # Prefer the authenticated top-of-book for position management/signals.
                quote_map.update(refreshed_quotes)

                opportunities.sort(key=lambda x: x["score"], reverse=True)
                await self.store.replace_opportunities(opportunities)
                probability_pass = [
                    o for o in opportunities
                    if o["probability"] >= config.min_signal_probability
                ]
                positive_ev = [o for o in opportunities if o["expected_value"] > 0]
                ev_pass = [
                    o for o in opportunities
                    if o["expected_value"] >= config.min_expected_value_dollars
                ]
                eligible = [
                    o for o in opportunities
                    if o["probability"] >= config.min_signal_probability
                    and o["expected_value"] > 0
                    and o["expected_value"] >= config.min_expected_value_dollars
                ]
                exploratory = [
                    o for o in opportunities
                    if o.get("strategy") == "late_close"
                    and bool(o.get("reviewable_exploration"))
                ]
                self.last_scan_stats = {
                    "markets": len(markets),
                    "quotes": len(quote_map),
                    "passed_prefilter": passed_prefilter,
                    "shortlisted": len(prelim),
                    "depth_checked": len(depth_tickers),
                    "orderbooks_received": len(books),
                    "orderbook_error": orderbook_error,
                    "ranked": len(opportunities),
                    "eligible": len(eligible),
                    "late_close_reviewable": len(exploratory),
                    "probability_pass": len(probability_pass),
                    "positive_ev": len(positive_ev),
                    "ev_pass": len(ev_pass),
                    "max_probability": max((o["probability"] for o in opportunities), default=0),
                    "max_expected_value": max((o["expected_value"] for o in opportunities), default=0),
                    "top_break_even_probability": min((o["break_even_probability"] for o in opportunities), default=1),
                    "model_observations": model.observations,
                    "rejected": dict(rejected),
                    "activity": config.trade_activity,
                    "effective_min_contract_price": config.effective_min_contract_price,
                    "effective_max_spread_cents": config.effective_max_spread_cents,
                    "effective_min_volume_24h": config.effective_min_volume_24h,
                    "effective_shortlist_size": config.effective_shortlist_size,
                    "min_signal_probability": config.min_signal_probability,
                    "min_expected_value_dollars": config.min_expected_value_dollars,
                    "late_close_window_minutes": config.effective_late_close_window_minutes,
                    "late_close_max_spread_cents": config.effective_late_close_max_spread_cents,
                }

                expires = (datetime.now(timezone.utc) + timedelta(minutes=config.signal_horizon_minutes)).isoformat()
                for opp in opportunities[: min(15, len(opportunities))]:
                    await self.store.add_signal(opp, expires)

                if config.agent_enabled and config.auto_trade and not killed:
                    await self._enter_qualified(config, snapshot, opportunities)

                self.last_scan_at = utcnow()
                self.last_scan_error = None
                await self.store.event(
                    "scan",
                    f"Scanned {len(markets)} markets; {len(opportunities)} ranked; {len(eligible)} eligible",
                    payload=self.last_scan_stats,
                )
                print(json.dumps({"event": "pip_scan", **self.last_scan_stats}), flush=True)
                return opportunities
            finally:
                self.scanning = False

    def _is_late_close_quote(self, q: SideQuote, config: PipConfig) -> bool:
        if not config.late_close_enabled:
            return False
        mins = minutes_until(q.close_time)
        if mins is None or mins < 5 or mins > config.effective_late_close_window_minutes:
            return False
        if not (D(config.late_close_min_price) <= q.ask <= D(config.late_close_max_price)):
            return False
        if q.spread <= 0 or q.spread > D(config.effective_late_close_max_spread_cents) * CENT:
            return False
        if q.volume_24h < config.effective_late_close_min_volume:
            return False
        return True

    def _candidate_sort_key(self, q: SideQuote, config: PipConfig):
        late = self._is_late_close_quote(q, config)
        mins = minutes_until(q.close_time)
        return (
            0 if late else 1,
            float(q.spread),
            mins if (late and mins is not None) else 10**9,
            -q.volume_24h,
            -q.ask_size,
        )

    def _pre_filter_reason(self, q: SideQuote, config: PipConfig) -> str | None:
        if q.ask <= 0 or q.bid <= 0:
            return "no_quote"
        if not (D(config.effective_min_contract_price) <= q.ask <= D(config.max_contract_price)):
            return "price"
        if q.spread <= 0 or q.spread > D(config.effective_max_spread_cents) * CENT:
            return "spread"
        if q.volume_24h < config.effective_min_volume_24h:
            return "volume"
        if q.ask >= Decimal("1"):
            return "settled_price"
        if q.close_time:
            close = parse_time(q.close_time)
            if close and close <= datetime.now(timezone.utc) + timedelta(minutes=5):
                return "closing"
        if q.ticker.startswith("KXMVE"):
            return "excluded_series"
        return None

    def _pre_filter(self, q: SideQuote, config: PipConfig) -> bool:
        return self._pre_filter_reason(q, config) is None

    def _momentum(self, q: SideQuote) -> float:
        hist = self.history[(q.ticker, q.side)]
        if len(hist) < 2:
            return 0.0
        old = hist[0][1]
        move_cents = float((q.bid - old) / CENT)
        return max(-1.0, min(1.0, move_cents / 2.0))

    def _build_opportunity(
        self,
        q: SideQuote,
        orderbook: dict[str, Any],
        config: PipConfig,
        model: PipSignalModel,
        snapshot: dict[str, float],
    ) -> dict[str, Any] | None:
        support, opposing = side_depths(q.side, q, orderbook)
        imbalance = book_imbalance(support, opposing)
        momentum = self._momentum(q)
        spread_cents = float(q.spread / CENT)
        spread_quality = clamp(1.0 - (spread_cents / max(1.0, config.effective_max_spread_cents)))
        near_depth = sum(support) + sum(opposing)
        liquidity = clamp(math.log10(near_depth + 1) / 2.5)
        volume = clamp(math.log10(q.volume_24h + 1) / 4.0)
        price_extremity = clamp(
            (float(q.ask) - config.effective_min_contract_price)
            / max(0.01, config.max_contract_price - config.effective_min_contract_price)
        )
        close_minutes = minutes_until(q.close_time)
        late_close = self._is_late_close_quote(q, config)
        time_proximity = 0.0
        if close_minutes is not None and close_minutes > 0:
            time_proximity = clamp(1.0 - (close_minutes / max(5.0, config.effective_late_close_window_minutes)))

        features = {
            "imbalance": imbalance,
            "momentum": momentum,
            "spread_quality": spread_quality,
            "liquidity": liquidity,
            "volume": volume,
            "price_extremity": price_extremity,
            "strategy": "late_close" if late_close else "scalp",
            "minutes_to_close": close_minutes,
            "time_proximity": time_proximity,
        }
        probability = model.predict(features)

        if late_close:
            # Maker-first late-stage micro scalp: improve entry economics and seek +1c.
            intended = min(q.ask - CENT, q.bid + CENT)
            intended = max(Decimal("0.01"), min(Decimal("0.98"), intended))
            entry_is_maker = True
            target = min(Decimal("0.99"), intended + D(config.late_close_target_cents) * CENT)
            stop = max(Decimal("0.01"), intended - D(config.late_close_stop_cents) * CENT)
            max_hold = max(
                1,
                min(
                    config.late_close_max_hold_minutes,
                    int(max(1.0, (close_minutes or config.late_close_max_hold_minutes) - 2)),
                ),
            )
        else:
            intended, entry_is_maker = self._entry_price(q, config)
            target = min(Decimal("0.99"), intended + D(config.take_profit_cents) * CENT)
            stop = max(Decimal("0.01"), intended - D(config.stop_loss_cents) * CENT)
            max_hold = config.max_hold_minutes

        count = max_contract_count(
            D(snapshot["equity"]), intended, config.effective_position_pct, config.effective_order_pct
        )
        if late_close:
            count = int(count * config.late_close_size_multiplier)
        if count < 1 or target <= intended:
            return None

        support_depth = int(sum(support)) if sum(support) > 0 else 0
        if support_depth > 0:
            count = min(count, support_depth)
        if not entry_is_maker and q.ask_size > 0:
            count = min(count, max(0, int(q.ask_size)))
        if count < 1:
            return None

        econ = trade_economics(
            count, intended, target, stop,
            entry_is_maker=entry_is_maker,
            target_exit_is_maker=False,
        )
        if econ.net_win <= 0:
            return None

        ev = expected_value(probability, econ)
        fill_factor = 0.70 if entry_is_maker else 1.0
        horizon = max(5.0, min(float(config.signal_horizon_minutes), float(max_hold)))
        velocity = max(0.0, float(ev)) * fill_factor / horizon

        reviewable_exploration = (
            late_close
            and econ.break_even_probability <= 0.80
            and close_minutes is not None
            and close_minutes >= 5
        )
        features.update({
            "reviewable_exploration": reviewable_exploration,
            "break_even_probability": econ.break_even_probability,
            "max_hold_minutes": max_hold,
            "net_win_if_target": float(econ.net_win),
            "net_loss_if_stop": float(econ.net_loss),
        })

        late_bonus = (time_proximity * 8.0) + (2.0 if reviewable_exploration else 0.0)
        score = (
            (probability * 100.0)
            + (float(ev) * 35.0)
            + (velocity * 1000.0)
            + (imbalance * 4.0)
            + late_bonus
        )
        return {
            "ticker": q.ticker,
            "title": q.title,
            "side": q.side,
            "observed_at": utcnow(),
            "entry_price": float(intended),
            "target_price": float(target),
            "stop_price": float(stop),
            "spread": float(q.spread),
            "probability": probability,
            "expected_value": float(ev),
            "score": score,
            "quantity": count,
            "features": features,
            "break_even_probability": econ.break_even_probability,
            "entry_is_maker": entry_is_maker,
            "entry_fee_estimate": float(econ.entry_fee),
            "strategy": "late_close" if late_close else "scalp",
            "reviewable_exploration": reviewable_exploration,
            "minutes_to_close": close_minutes,
            "max_hold_minutes": max_hold,
        }

    def _entry_price(self, q: SideQuote, config: PipConfig) -> tuple[Decimal, bool]:
        if config.entry_style == "taker":
            return q.ask, False
        maker = min(q.ask - CENT, q.bid + CENT) if q.spread >= D("0.02") else q.bid
        maker = max(Decimal("0.01"), min(Decimal("0.99"), maker))
        if config.entry_style == "maker":
            return maker, True
        return (maker, True) if q.spread >= D("0.02") else (q.ask, False)

    async def _enter_qualified(self, config: PipConfig, snapshot: dict[str, float], opportunities: list[dict[str, Any]]):
        positions = await self.store.positions()
        open_keys = {(p["ticker"], p["side"]) for p in positions}
        equity = max(0.01, snapshot["equity"])
        exposure = snapshot["exposure"] + sum(
            float(p["intended_entry"]) * int(p["quantity"])
            for p in positions if p["status"] == "pending_entry"
        )
        max_exposure = equity * config.max_total_exposure_pct
        cash_floor = equity * config.min_cash_reserve_pct
        max_new = config.max_new_trades_per_scan
        entered = 0

        for opp in opportunities:
            if entered >= max_new or len(positions) + entered >= config.max_open_positions:
                break
            if (opp["ticker"], opp["side"]) in open_keys:
                continue
            if opp["probability"] < config.min_signal_probability:
                continue
            if opp["expected_value"] < config.min_expected_value_dollars or opp["expected_value"] <= 0:
                continue
            position_notional = opp["entry_price"] * opp["quantity"]
            hard_cap = equity * PipConfig.HARD_MAX_POSITION_PCT
            if position_notional > hard_cap + 1e-9:
                continue
            if exposure + position_notional > max_exposure + 1e-9:
                continue
            if snapshot["cash"] - position_notional < cash_floor - 1e-9:
                continue

            ok = await self._submit_entry(config, opp)
            if ok:
                exposure += position_notional
                entered += 1
                open_keys.add((opp["ticker"], opp["side"]))

    async def _submit_entry(self, config: PipConfig, opp: dict[str, Any], reviewed: bool = False) -> bool:
        maker = bool(opp["entry_is_maker"])
        rationale = json.dumps({
            "probability": opp["probability"],
            "expected_value": opp["expected_value"],
            "strategy": opp.get("strategy", "scalp"),
            "reviewable_exploration": opp.get("reviewable_exploration", False),
            "features": opp["features"],
        })
        if config.mode == "paper":
            if maker:
                status = "pending_entry"
                entry_price = None
                entry_fee = 0.0
                opened_at = None
            else:
                status = "open"
                entry_price = opp["entry_price"]
                entry_fee = float(kalshi_fee(opp["quantity"], D(opp["entry_price"])))
                opened_at = utcnow()
            pid = await self.store.create_position({
                "ticker": opp["ticker"], "side": opp["side"], "mode": "paper",
                "status": status, "quantity": opp["quantity"],
                "intended_entry": opp["entry_price"], "entry_price": entry_price,
                "entry_fee": entry_fee, "target_price": opp["target_price"],
                "stop_price": opp["stop_price"], "opened_at": opened_at,
                "max_hold_minutes": int(opp.get("max_hold_minutes", config.max_hold_minutes)), "last_bid": None,
                "rationale": rationale,
            })
            if pid:
                await self.store.event("entry", f"Pip {'rested' if maker else 'filled'} paper {opp['side'].upper()} {opp['ticker']}", payload=opp)
                return True
            return False

        if config.mode == "live" and not config.can_submit_real_money() and not reviewed:
            await self.store.event("live_locked", "Live order blocked: explicit review required", "warning")
            return False
        expected_env = "production" if config.mode == "live" else "demo"
        if self.kalshi.environment.name != expected_env:
            await self.store.event("env_mismatch", f"{config.mode} mode requires KALSHI_ENV={expected_env}", "error")
            return False
        if not self.kalshi.authenticated:
            return False

        book_side, yes_price = side_to_v2_entry(opp["side"], D(opp["entry_price"]))
        response = await self.kalshi.create_order_v2(
            ticker=opp["ticker"], client_order_id=str(uuid.uuid4()), book_side=book_side,
            count=opp["quantity"], yes_price=yes_price, post_only=maker,
            time_in_force=("fill_or_kill" if reviewed and not maker else "good_till_canceled"),
        )
        order_id = response.get("order_id")
        fill_count = int(float(response.get("fill_count") or 0))
        remaining_count = int(float(response.get("remaining_count") or 0))
        fill_summary = await self._created_order_fill_summary(response, opp["side"])
        actual_qty = int(fill_summary["quantity"])
        actual_contract_price = fill_summary["average_contract_price"]

        if reviewed and not maker and actual_qty <= 0:
            await self.store.event(
                "reviewed_live_no_fill",
                f"Reviewed live entry did not fill {opp['ticker']} {opp['side'].upper()}",
                "warning",
            )
            return False

        if actual_qty > 0 and remaining_count > 0 and order_id:
            try:
                await self.kalshi.cancel_order_v2(order_id, opp["ticker"])
            except Exception as exc:
                await self.store.event("partial_entry_cancel_error", str(exc), "warning")

        if actual_qty > 0 and actual_contract_price is None:
            raise PipKalshiError("Kalshi reported a fill but Pip could not resolve its price")

        status = "open" if actual_qty > 0 else "pending_entry"
        qty = actual_qty if actual_qty > 0 else opp["quantity"]
        pid = await self.store.create_position({
            "ticker": opp["ticker"], "side": opp["side"], "mode": config.mode,
            "status": status, "quantity": qty, "intended_entry": opp["entry_price"],
            "entry_price": float(actual_contract_price) if actual_contract_price is not None else None,
            "entry_fee": float(fill_summary["total_fee"]),
            "target_price": opp["target_price"], "stop_price": opp["stop_price"],
            "opened_at": utcnow() if actual_qty > 0 else None,
            "max_hold_minutes": int(opp.get("max_hold_minutes", config.max_hold_minutes)), "entry_order_id": order_id,
            "rationale": rationale,
        })
        if pid:
            kind = "reviewed_live_entry" if reviewed and config.mode == "live" else "entry_order"
            await self.store.event(kind, f"Submitted {config.mode} entry for {opp['ticker']}", payload={"order_id": order_id, **opp})
            return True
        return False

    async def account_balance(self) -> dict[str, Any]:
        if not self.kalshi.authenticated:
            raise PipKalshiError("Kalshi credentials are not configured")
        payload = await self.kalshi.get_balance()
        cash = self._balance_dollars(payload)
        portfolio_value = float(payload.get("portfolio_value") or 0) / 100.0
        return {
            "environment": self.kalshi.environment.name,
            "cash": cash,
            "portfolio_value": portfolio_value,
            "equity": cash + portfolio_value,
            "authenticated": True,
        }

    async def submit_reviewed_live_entry(self, ticker: str, side: str) -> dict[str, Any]:
        if self._execution_lock.locked():
            raise PipKalshiError("Another reviewed order is being processed")
        async with self._execution_lock:
            return await self._submit_reviewed_live_entry_locked(ticker, side)

    async def _submit_reviewed_live_entry_locked(self, ticker: str, side: str) -> dict[str, Any]:
        config = await self.store.load_config()
        if config.mode != "live":
            raise PipKalshiError("Switch Pip to Live mode before approving a real order")
        if not config.agent_enabled:
            raise PipKalshiError("Pip is paused; start Pip before approving a new live entry")
        if self.kalshi.environment.name != "production":
            raise PipKalshiError("Reviewed live orders require KALSHI_ENV=production")
        if not self.kalshi.authenticated:
            raise PipKalshiError("Kalshi production credentials are not authenticated")

        opportunities = await self.scan_once()
        opp = next(
            (o for o in opportunities if o.get("ticker") == ticker and o.get("side") == side),
            None,
        )
        if not opp:
            raise PipKalshiError("Opportunity is no longer available after a fresh scan")

        snapshot = await self.equity_snapshot(config)
        if await self._risk_kill_check(config, snapshot):
            raise PipKalshiError("Risk controls paused new entries")

        positions = await self.store.positions()
        if len(positions) >= config.max_open_positions:
            raise PipKalshiError("Maximum open positions reached")
        if any(p["ticker"] == ticker and p["side"] == side for p in positions):
            raise PipKalshiError("Pip already has an active position/order in this market and side")
        exploratory_late_close = (
            opp.get("strategy") == "late_close"
            and bool(opp.get("reviewable_exploration"))
        )
        if not exploratory_late_close:
            if opp["probability"] < config.min_signal_probability:
                raise PipKalshiError("Signal fell below the current approval threshold")
            if opp["expected_value"] <= 0 or opp["expected_value"] < config.min_expected_value_dollars:
                raise PipKalshiError("Expected value fell below the current approval threshold")
        else:
            # Exploratory lane is explicitly reviewed by the operator and must remain
            # structurally bounded even while the model is still calibrating.
            if opp.get("minutes_to_close") is None or float(opp["minutes_to_close"]) < 5:
                raise PipKalshiError("Late-close market is too close to closing")
            if float(opp.get("break_even_probability", 1)) > 0.80:
                raise PipKalshiError("Late-close trade economics are too weak")

        equity = max(0.01, snapshot["equity"])
        position_notional = float(opp["entry_price"]) * int(opp["quantity"])
        pending = sum(
            float(p["intended_entry"]) * int(p["quantity"])
            for p in positions if p["status"] == "pending_entry"
        )
        if position_notional > equity * PipConfig.HARD_MAX_POSITION_PCT + 1e-9:
            raise PipKalshiError("Order exceeds the hard 10% position cap")
        if position_notional > equity * PipConfig.HARD_MAX_ORDER_PCT + 1e-9:
            raise PipKalshiError("Order exceeds the hard 10% order cap")
        if snapshot["exposure"] + pending + position_notional > equity * config.max_total_exposure_pct + 1e-9:
            raise PipKalshiError("Order exceeds total exposure limit")
        if snapshot["cash"] - position_notional < equity * config.min_cash_reserve_pct - 1e-9:
            raise PipKalshiError("Order would violate the cash reserve")

        submitted = await self._submit_entry(config, opp, reviewed=True)
        return {"submitted": bool(submitted), "opportunity": opp}

    async def submit_reviewed_live_exit(self, position_id: int) -> dict[str, Any]:
        if self._execution_lock.locked():
            raise PipKalshiError("Another reviewed order is being processed")
        async with self._execution_lock:
            return await self._submit_reviewed_live_exit_locked(position_id)

    async def _submit_reviewed_live_exit_locked(self, position_id: int) -> dict[str, Any]:
        config = await self.store.load_config()
        if config.mode != "live":
            raise PipKalshiError("Switch Pip to Live mode before approving a real exit")
        if self.kalshi.environment.name != "production" or not self.kalshi.authenticated:
            raise PipKalshiError("Kalshi production authentication is required")

        position = next(
            (p for p in await self.store.positions() if int(p["id"]) == int(position_id)),
            None,
        )
        if not position or position["status"] != "open":
            raise PipKalshiError("Open live position not found")

        market = await self.kalshi.get_market(position["ticker"])
        raw_market = market.get("market", market)
        quote = next(
            (q for q in quotes_for_market(raw_market) if q.side == position["side"]),
            None,
        )
        if not quote or quote.bid <= 0:
            raise PipKalshiError("No executable bid is available for this position")

        rationale = position.get("rationale") or ""
        reason = "reviewed_manual"
        if "|exit_ready:" in rationale:
            reason = rationale.rsplit("|exit_ready:", 1)[-1].split("|", 1)[0]
        result = await self._submit_exit(position, quote.bid, reason, reviewed=True)
        return result or {"submitted": False}

    async def _fill_summary(self, order_id: str, contract_side: str) -> dict[str, Any]:
        data = await self.kalshi.get_fills(order_id=order_id)
        fills = data.get("fills", [])
        total_qty = Decimal("0")
        total_cost = Decimal("0")
        total_fee = Decimal("0")
        for fill in fills:
            qty = D(fill.get("count_fp") or 0)
            if qty <= 0:
                continue
            price_key = "yes_price_dollars" if contract_side == "yes" else "no_price_dollars"
            price = D(fill.get(price_key) or 0)
            total_qty += qty
            total_cost += qty * price
            total_fee += D(fill.get("fee_cost") or 0)
        avg_price = (total_cost / total_qty) if total_qty > 0 else None
        return {
            "quantity": int(total_qty),
            "average_contract_price": avg_price,
            "total_fee": total_fee,
            "fills": fills,
        }

    async def _created_order_fill_summary(
        self,
        response: dict[str, Any],
        contract_side: str,
    ) -> dict[str, Any]:
        order_id = response.get("order_id")
        fill_count = int(float(response.get("fill_count") or 0))
        if fill_count <= 0:
            return {
                "quantity": 0,
                "average_contract_price": None,
                "total_fee": Decimal("0"),
                "fills": [],
            }
        if order_id:
            try:
                summary = await self._fill_summary(order_id, contract_side)
                if summary["quantity"] > 0:
                    return summary
            except Exception:
                pass

        # Fallback to the synchronous create response if the fills endpoint lags.
        yes_avg = response.get("average_fill_price")
        contract_avg = (
            self._contract_price_from_yes(contract_side, yes_avg)
            if yes_avg is not None else None
        )
        per_contract_fee = D(response.get("average_fee_paid") or 0)
        return {
            "quantity": fill_count,
            "average_contract_price": contract_avg,
            "total_fee": per_contract_fee * D(fill_count),
            "fills": [],
        }

    def _contract_price_from_yes(self, side: str, yes_price: Any) -> Decimal:
        p = D(yes_price)
        if p > 1:
            p /= D(100)
        return p if side == "yes" else Decimal("1") - p

    async def _reconcile_positions(self, config: PipConfig, quote_map: dict[tuple[str, str], SideQuote]):
        positions = await self.store.positions()
        for p in positions:
            q = quote_map.get((p["ticker"], p["side"]))
            if q:
                await self.store.update_position(p["id"], last_bid=float(q.bid))
            if p["status"] == "pending_entry":
                await self._reconcile_pending_entry(p, q)
            elif p["status"] == "open":
                await self._manage_open_position(p, q)
            elif p["status"] == "pending_exit":
                await self._reconcile_pending_exit(p)

    async def _reconcile_pending_entry(self, p: dict[str, Any], q: SideQuote | None):
        if p["mode"] == "paper":
            if q and q.ask <= D(p["intended_entry"]):
                price = float(D(p["intended_entry"]))
                fee = float(conservative_maker_fee(int(p["quantity"]), D(price)))
                await self.store.update_position(p["id"], status="open", entry_price=price, entry_fee=fee, opened_at=utcnow())
                await self.store.event("fill", f"Paper maker entry filled {p['ticker']} {p['side'].upper()}")
            return
        if not p.get("entry_order_id") or not self.kalshi.authenticated:
            return
        try:
            response = await self.kalshi.get_order(p["entry_order_id"])
        except Exception:
            return
        order = response.get("order", response)
        fill_count = int(float(order.get("fill_count_fp") or 0))
        remaining = int(float(order.get("remaining_count_fp") or 0))
        if fill_count <= 0:
            return
        if remaining > 0:
            try:
                await self.kalshi.cancel_order_v2(p["entry_order_id"], p["ticker"])
            except Exception:
                pass
        summary = await self._fill_summary(p["entry_order_id"], p["side"])
        if summary["quantity"] <= 0 or summary["average_contract_price"] is None:
            return
        await self.store.update_position(
            p["id"], status="open", quantity=summary["quantity"],
            entry_price=float(summary["average_contract_price"]),
            entry_fee=float(summary["total_fee"]), opened_at=utcnow(),
        )
        await self.store.event("fill", f"{p['mode']} entry filled {p['ticker']} {p['side'].upper()}")

    async def _manage_open_position(self, p: dict[str, Any], q: SideQuote | None):
        if not q or not p.get("entry_price"):
            return
        bid = q.bid
        target = D(p["target_price"])
        stop = D(p["stop_price"])
        opened = parse_time(p["opened_at"])
        held_minutes = ((datetime.now(timezone.utc) - opened).total_seconds() / 60.0) if opened else 0
        reason = None
        if bid >= target:
            reason = "target"
        elif bid <= stop:
            reason = "stop"
        elif held_minutes >= int(p["max_hold_minutes"]):
            reason = "time"
        elif q.close_time:
            close = parse_time(q.close_time)
            if close and close <= datetime.now(timezone.utc) + timedelta(minutes=5):
                reason = "market_closing"
        if reason:
            if p["mode"] == "live":
                config = await self.store.load_config()
                if not config.can_submit_real_money():
                    marker = f"|exit_ready:{reason}"
                    rationale = p.get("rationale") or ""
                    if marker not in rationale:
                        await self.store.update_position(p["id"], rationale=rationale + marker)
                        await self.store.event(
                            "exit_review_required",
                            f"Live exit requires approval: {p['ticker']} ({reason})",
                            "warning",
                            payload={"position_id": p["id"], "reason": reason, "bid": float(bid)},
                        )
                    return
            await self._submit_exit(p, bid, reason)

    async def _submit_exit(self, p: dict[str, Any], bid: Decimal, reason: str, reviewed: bool = False):
        qty = int(p["quantity"])
        if p["mode"] == "paper":
            # Conservative simulator: target and risk exits both cross the executable bid.
            exit_price = bid
            fee = kalshi_fee(qty, exit_price)
            pnl = await self.store.close_position(p, exit_price=float(exit_price), exit_fee=float(fee), exit_reason=reason)
            await self.store.event("exit", f"Paper exit {p['ticker']} {reason}: {pnl:+.2f}", payload={"pnl": pnl})
            return
        config = await self.store.load_config()
        if p["mode"] == "live" and not config.can_submit_real_money() and not reviewed:
            await self.store.event("live_locked", "Live exit blocked: explicit review required", "warning")
            return {"submitted": False}
        exit_contract_price = bid
        book_side, yes_price = side_to_v2_exit(p["side"], exit_contract_price)
        try:
            response = await self.kalshi.create_order_v2(
                ticker=p["ticker"], client_order_id=str(uuid.uuid4()), book_side=book_side,
                count=qty, yes_price=yes_price, post_only=False,
                time_in_force="fill_or_kill", reduce_only=True,
            )
        except Exception as exc:
            await self.store.event("exit_error", f"Exit submission failed {p['ticker']}: {exc}", "error")
            return {"submitted": False, "error": str(exc)}

        fill_summary = await self._created_order_fill_summary(response, p["side"])
        filled = int(fill_summary["quantity"])
        order_id = response.get("order_id")
        if filled <= 0:
            await self.store.event(
                "exit_no_fill",
                f"Exit did not fill {p['ticker']} ({reason})",
                "warning",
                payload={"position_id": p["id"], "reason": reason},
            )
            return {"submitted": True, "filled": 0, "order_id": order_id}

        exit_price = fill_summary["average_contract_price"]
        if exit_price is None:
            raise PipKalshiError("Kalshi reported an exit fill but Pip could not resolve its price")
        pnl = await self.store.record_partial_exit(
            p,
            filled_quantity=filled,
            exit_price=float(exit_price),
            exit_fee=float(fill_summary["total_fee"]),
            exit_reason=reason,
        )
        await self.store.event(
            "reviewed_live_exit" if reviewed and p["mode"] == "live" else "exit",
            f"{p['mode']} exit filled {p['ticker']}: {pnl:+.2f}",
            payload={"pnl": pnl, "filled": filled, "order_id": order_id},
        )
        return {"submitted": True, "filled": filled, "order_id": order_id, "pnl": pnl}

    async def _reconcile_pending_exit(self, p: dict[str, Any]):
        if not p.get("exit_order_id") or not self.kalshi.authenticated:
            return
        try:
            response = await self.kalshi.get_order(p["exit_order_id"])
        except Exception:
            return
        order = response.get("order", response)
        filled = int(float(order.get("fill_count_fp") or 0))
        if filled <= 0:
            return
        summary = await self._fill_summary(p["exit_order_id"], p["side"])
        if summary["quantity"] <= 0 or summary["average_contract_price"] is None:
            return
        reason = "exchange_exit"
        rationale = p.get("rationale") or ""
        if "|exit:" in rationale:
            reason = rationale.rsplit("|exit:", 1)[-1]
        pnl = await self.store.record_partial_exit(
            p,
            filled_quantity=summary["quantity"],
            exit_price=float(summary["average_contract_price"]),
            exit_fee=float(summary["total_fee"]),
            exit_reason=reason,
        )
        await self.store.event("exit", f"{p['mode']} exit filled {p['ticker']}: {pnl:+.2f}", payload={"pnl": pnl})

    async def _resolve_signals(self, config: PipConfig, model: PipSignalModel, quote_map: dict[tuple[str, str], SideQuote]):
        changed = False
        now = datetime.now(timezone.utc)
        for signal in await self.store.pending_signals():
            q = quote_map.get((signal["ticker"], signal["side"]))
            expiry = parse_time(signal["expires_at"])
            outcome = None
            if q and q.bid >= D(signal["target_price"]):
                outcome = 1
            elif q and q.bid <= D(signal["stop_price"]):
                outcome = 0
            elif expiry and now >= expiry:
                outcome = 0
            if outcome is not None:
                model.update(signal["features"], outcome)
                await self.store.resolve_signal(signal["id"], outcome)
                changed = True
        if changed:
            await self.store.save_model(model)

    async def cancel_pending_entries(self):
        """Cancel unfilled entry orders while leaving open positions under management."""
        for p in await self.store.positions():
            if p["status"] != "pending_entry":
                continue
            if p["mode"] != "paper" and p.get("entry_order_id") and self.kalshi.authenticated:
                try:
                    await self.kalshi.cancel_order_v2(p["entry_order_id"], p["ticker"])
                except Exception as exc:
                    await self.store.event("cancel_error", f"Could not cancel {p['ticker']}: {exc}", "warning")
                    continue
            await self.store.update_position(p["id"], status="cancelled")
            await self.store.event("cancel", f"Cancelled pending entry {p['ticker']} {p['side'].upper()}")

    async def _risk_kill_check(self, config: PipConfig, snapshot: dict[str, float]) -> bool:
        equity = max(0.0, snapshot["equity"])
        daily = await self.store.daily_realized_pnl()
        losses = await self.store.consecutive_losses()
        peak = await self.store.peak_equity(config.paper_starting_equity)
        drawdown = (peak - equity) / peak if peak > 0 else 0
        daily_base = max(config.paper_starting_equity, peak)
        reasons = []
        if daily + snapshot["unrealized_pnl"] <= -(daily_base * config.max_daily_loss_pct):
            reasons.append("daily loss limit")
        if drawdown >= config.max_drawdown_pct:
            reasons.append("drawdown limit")
        if losses >= config.max_consecutive_losses:
            reasons.append("consecutive loss limit")
        if reasons:
            config.agent_enabled = False
            config.auto_trade = False
            await self.store.save_config(config)
            await self.store.event("kill_switch", "Pip paused: " + ", ".join(reasons), "error")
            return True
        return False

    async def status(self) -> dict[str, Any]:
        config = await self.store.load_config()
        try:
            snapshot = await self.equity_snapshot(config)
        except Exception as exc:
            snapshot = {
                "equity": config.paper_starting_equity,
                "cash": config.paper_starting_equity,
                "exposure": 0.0,
                "realized_pnl": await self.store.realized_pnl(),
                "unrealized_pnl": 0.0,
            }
            self.last_scan_error = str(exc)
        return {
            **snapshot,
            "daily_realized_pnl": await self.store.daily_realized_pnl(),
            "agent_enabled": config.agent_enabled,
            "auto_trade": config.auto_trade,
            "mode": config.mode,
            "scanning": self.scanning,
            "last_scan_at": self.last_scan_at,
            "last_scan_error": self.last_scan_error,
            "scan_stats": self.last_scan_stats,
            "kalshi_environment": self.kalshi.environment.name,
            "kalshi_authenticated": self.kalshi.authenticated,
            "config": config.to_public_dict(),
            "model": json.loads((await self.store.load_model()).to_json()),
        }
