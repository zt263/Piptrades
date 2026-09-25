from decimal import Decimal

from src.pip.config import PipConfig
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
