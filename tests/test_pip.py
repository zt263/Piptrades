import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

from src.pip.config import PipConfig
from src.pip.engine import PipEngine, SideQuote, quotes_for_market, refresh_quote_from_orderbook
from src.pip.kalshi import PipKalshiClient
from src.pip.math import (
    expected_value,
    kalshi_fee,
    max_contract_count,
    side_to_v2_entry,
    side_to_v2_exit,
    trade_economics,
)
from src.pip.model import PipSignalModel


def test_hard_10_percent_position_and_order_cap_at_100():
    cfg = PipConfig(risk_level=100, max_position_pct=0.10, max_order_pct=0.10).normalize()
    count = max_contract_count(Decimal("100"), Decimal("0.96"), cfg.effective_position_pct, cfg.effective_order_pct)
    assert count == 10
    assert Decimal(count) * Decimal("0.96") <= Decimal("10")


def test_risk_slider_only_reduces_size():
    hi = PipConfig(risk_level=100).normalize()
    mid = PipConfig(risk_level=50).normalize()
    assert hi.effective_position_pct == 0.10
    assert mid.effective_position_pct == 0.05
    assert PipConfig(risk_level=100, max_position_pct=0.25).normalize().effective_position_pct == 0.10


def test_trade_activity_does_not_change_sizing():
    low = PipConfig(trade_activity=0, risk_level=100).normalize()
    high = PipConfig(trade_activity=100, risk_level=100).normalize()
    assert low.effective_position_pct == high.effective_position_pct == 0.10
    assert high.min_signal_probability < low.min_signal_probability
    assert high.min_expected_value_dollars < low.min_expected_value_dollars


def test_yes_no_v2_order_direction_mapping():
    assert side_to_v2_entry("yes", Decimal("0.96")) == ("bid", Decimal("0.96"))
    assert side_to_v2_entry("no", Decimal("0.96")) == ("ask", Decimal("0.04"))
    assert side_to_v2_exit("yes", Decimal("0.98")) == ("ask", Decimal("0.98"))
    assert side_to_v2_exit("no", Decimal("0.98")) == ("bid", Decimal("0.02"))


def test_fee_rounds_up_to_cent():
    fee = kalshi_fee(10, Decimal("0.96"))
    assert fee == Decimal("0.03")


def test_96_to_98_scalp_requires_real_hit_probability():
    econ = trade_economics(
        10, Decimal("0.96"), Decimal("0.98"), Decimal("0.93"), entry_is_maker=False
    )
    assert econ.net_win > 0
    assert econ.net_loss < 0
    assert econ.break_even_probability > 0.60
    assert expected_value(econ.break_even_probability - 0.01, econ) < 0
    assert expected_value(econ.break_even_probability + 0.01, econ) > 0


def test_online_model_moves_toward_observed_outcome():
    model = PipSignalModel()
    features = {"imbalance": 0.8, "momentum": 0.5, "spread_quality": 1, "liquidity": 0.7, "volume": 0.6, "price_extremity": 0.7}
    before = model.predict(features)
    model.update(features, 1)
    after = model.predict(features)
    assert after > before
    assert model.observations == 1


def test_live_autotrading_stays_locked_without_server_gate(monkeypatch):
    monkeypatch.delenv("PIP_LIVE_EXECUTION_ENABLED", raising=False)
    cfg = PipConfig(mode="live").normalize()
    assert cfg.can_submit_real_money() is False


def test_activity_dial_widens_universe_without_changing_size():
    low = PipConfig(trade_activity=0, risk_level=100).normalize()
    high = PipConfig(trade_activity=100, risk_level=100).normalize()

    assert low.effective_position_pct == high.effective_position_pct == 0.10
    assert low.effective_order_pct == high.effective_order_pct == 0.10

    assert high.effective_min_contract_price < low.effective_min_contract_price
    assert high.effective_max_spread_cents > low.effective_max_spread_cents
    assert high.effective_min_volume_24h < low.effective_min_volume_24h
    assert high.effective_shortlist_size > low.effective_shortlist_size
    assert high.min_signal_probability < low.min_signal_probability
    assert high.min_expected_value_dollars > 0
    assert high.max_new_trades_per_scan > low.max_new_trades_per_scan


def test_no_quote_sizes_are_derived_from_yes_liquidity():
    market = {
        "ticker": "TEST",
        "title": "Test market",
        "yes_bid_dollars": "0.0400",
        "yes_ask_dollars": "0.0500",
        "yes_bid_size_fp": "12.00",
        "yes_ask_size_fp": "34.00",
        "no_bid_dollars": "0.9500",
        "no_ask_dollars": "0.9600",
        "volume_24h_fp": "100.00",
        "close_time": "2099-01-01T00:00:00Z",
    }
    yes, no = quotes_for_market(market)
    assert yes.bid_size == 12.0
    assert yes.ask_size == 34.0
    assert no.bid_size == 34.0
    assert no.ask_size == 12.0


def test_orderbook_refresh_uses_complementary_bids_for_asks():
    market = {
        "ticker": "TEST",
        "title": "Test market",
        "yes_bid_dollars": "0.9500",
        "yes_ask_dollars": "0.9700",
        "yes_bid_size_fp": "5.00",
        "yes_ask_size_fp": "8.00",
        "no_bid_dollars": "0.0300",
        "no_ask_dollars": "0.0500",
        "volume_24h_fp": "100.00",
        "close_time": "2099-01-01T00:00:00Z",
    }
    yes, no = quotes_for_market(market)
    book = {
        "orderbook_fp": {
            "yes_dollars": [["0.9600", "11.00"]],
            "no_dollars": [["0.0300", "17.00"]],
        }
    }
    live_yes = refresh_quote_from_orderbook(yes, book)
    live_no = refresh_quote_from_orderbook(no, book)
    assert live_yes.bid == Decimal("0.9600")
    assert live_yes.ask == Decimal("0.9700")
    assert live_yes.bid_size == 11.0
    assert live_yes.ask_size == 17.0
    assert live_no.bid == Decimal("0.0300")
    assert live_no.ask == Decimal("0.0400")
    assert live_no.bid_size == 17.0
    assert live_no.ask_size == 11.0


def test_market_discovery_excludes_multivariate_markets():
    async def run():
        client = PipKalshiClient(env="production")
        client.request = AsyncMock(return_value={"markets": [], "cursor": ""})
        await client.get_markets()
        _, path = client.request.call_args.args
        kwargs = client.request.call_args.kwargs
        assert path == "/trade-api/v2/markets"
        assert kwargs["params"]["mve_filter"] == "exclude"
        await client.close()
    asyncio.run(run())


def test_orderbooks_are_authenticated_and_bulk():
    async def run():
        client = PipKalshiClient(env="production")
        client.api_key = "test"
        client.private_key = object()
        client.request = AsyncMock(return_value={
            "orderbooks": [{"ticker": "A", "orderbook_fp": {"yes_dollars": [], "no_dollars": []}}]
        })
        result = await client.get_orderbooks(["A", "B"])
        assert "A" in result
        _, path = client.request.call_args.args
        kwargs = client.request.call_args.kwargs
        assert path == "/trade-api/v2/markets/orderbooks"
        assert kwargs["params"]["tickers"] == ["A", "B"]
        assert kwargs["auth"] is True
        await client.close()
    asyncio.run(run())


def test_get_order_uses_current_v2_portfolio_path():
    async def run():
        client = PipKalshiClient(env="production")
        client.request = AsyncMock(return_value={"order": {}})
        await client.get_order("order-123")
        _, path = client.request.call_args.args
        assert path == "/trade-api/v2/portfolio/orders/order-123"
        assert client.request.call_args.kwargs["auth"] is True
        await client.close()
    asyncio.run(run())


def test_late_close_window_expands_with_activity_without_changing_risk():
    low = PipConfig(trade_activity=0, risk_level=100).normalize()
    high = PipConfig(trade_activity=100, risk_level=100).normalize()
    assert low.effective_position_pct == high.effective_position_pct == 0.10
    assert low.effective_late_close_window_minutes < high.effective_late_close_window_minutes
    assert high.effective_late_close_window_minutes == high.late_close_window_minutes
    assert high.effective_late_close_max_spread_cents == 3


def test_late_close_micro_scalp_is_reviewable_but_not_forced_model_eligible():
    cfg = PipConfig(
        trade_activity=100,
        risk_level=100,
        late_close_enabled=True,
        late_close_min_price=0.90,
        late_close_max_price=0.98,
    ).normalize()
    engine = PipEngine(store=None, kalshi=None)
    close = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    q = SideQuote(
        ticker="LATE",
        title="Late close test",
        side="yes",
        bid=Decimal("0.96"),
        ask=Decimal("0.97"),
        bid_size=100,
        ask_size=100,
        volume_24h=500,
        close_time=close,
    )
    book = {
        "orderbook_fp": {
            "yes_dollars": [["0.9600", "100.00"]],
            "no_dollars": [["0.0300", "100.00"]],
        }
    }
    opp = engine._build_opportunity(
        q,
        book,
        cfg,
        PipSignalModel(),
        {"equity": 100.0, "cash": 100.0, "exposure": 0.0, "realized_pnl": 0.0, "unrealized_pnl": 0.0},
    )
    assert opp is not None
    assert opp["strategy"] == "late_close"
    assert opp["reviewable_exploration"] is True
    assert Decimal(str(opp["target_price"])) - Decimal(str(opp["entry_price"])) == Decimal("0.01")
    assert Decimal(str(opp["entry_price"])) - Decimal(str(opp["stop_price"])) == Decimal("0.01")
    assert opp["quantity"] <= 10
    assert opp["break_even_probability"] <= 0.80


def test_99_cent_contract_is_not_late_close_micro_scalp():
    cfg = PipConfig(trade_activity=100).normalize()
    engine = PipEngine(store=None, kalshi=None)
    close = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
    q = SideQuote(
        ticker="NINETY-NINE",
        title="No pre-settlement upside",
        side="yes",
        bid=Decimal("0.98"),
        ask=Decimal("0.99"),
        bid_size=100,
        ask_size=100,
        volume_24h=500,
        close_time=close,
    )
    assert engine._is_late_close_quote(q, cfg) is False
