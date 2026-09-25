from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class PipConfig:
    mode: str = "paper"  # paper | demo | live
    agent_enabled: bool = True
    auto_trade: bool = True

    risk_level: int = 100
    trade_activity: int = 65

    # Operator limits. Hard caps below cannot be exceeded by settings or UI.
    max_position_pct: float = 0.10
    max_order_pct: float = 0.10
    max_total_exposure_pct: float = 0.60
    min_cash_reserve_pct: float = 0.20
    max_open_positions: int = 10

    min_contract_price: float = 0.90
    max_contract_price: float = 0.99
    take_profit_cents: int = 2
    stop_loss_cents: int = 3
    max_hold_minutes: int = 60

    min_volume_24h: float = 25.0
    max_spread_cents: int = 2
    shortlist_size: int = 30
    scan_interval_seconds: int = 15
    entry_style: str = "smart"  # smart | maker | taker

    max_daily_loss_pct: float = 0.05
    max_drawdown_pct: float = 0.15
    max_consecutive_losses: int = 5

    paper_starting_equity: float = 100.0
    signal_horizon_minutes: int = 45

    # Hard safety ceilings. They are not editable from the dashboard.
    HARD_MAX_POSITION_PCT: float = 0.10
    HARD_MAX_ORDER_PCT: float = 0.10
    HARD_MAX_TOTAL_EXPOSURE_PCT: float = 0.90
    MIN_SCAN_INTERVAL_SECONDS: int = 5

    def normalize(self) -> "PipConfig":
        self.mode = str(self.mode).lower().strip()
        if self.mode not in {"paper", "demo", "live"}:
            self.mode = "paper"

        self.risk_level = max(0, min(100, int(self.risk_level)))
        self.trade_activity = max(0, min(100, int(self.trade_activity)))

        self.max_position_pct = max(0.001, min(float(self.max_position_pct), self.HARD_MAX_POSITION_PCT))
        self.max_order_pct = max(0.001, min(float(self.max_order_pct), self.HARD_MAX_ORDER_PCT))
        self.max_total_exposure_pct = max(
            self.max_position_pct,
            min(float(self.max_total_exposure_pct), self.HARD_MAX_TOTAL_EXPOSURE_PCT),
        )
        self.min_cash_reserve_pct = max(0.0, min(float(self.min_cash_reserve_pct), 0.95))
        self.max_open_positions = max(1, min(int(self.max_open_positions), 50))

        self.min_contract_price = max(0.01, min(float(self.min_contract_price), 0.99))
        self.max_contract_price = max(self.min_contract_price, min(float(self.max_contract_price), 0.99))
        self.take_profit_cents = max(1, min(int(self.take_profit_cents), 20))
        self.stop_loss_cents = max(1, min(int(self.stop_loss_cents), 25))
        self.max_hold_minutes = max(1, min(int(self.max_hold_minutes), 24 * 60))
        self.signal_horizon_minutes = max(5, min(int(self.signal_horizon_minutes), 24 * 60))

        self.min_volume_24h = max(0.0, float(self.min_volume_24h))
        self.max_spread_cents = max(1, min(int(self.max_spread_cents), 20))
        self.shortlist_size = max(5, min(int(self.shortlist_size), 200))
        self.scan_interval_seconds = max(self.MIN_SCAN_INTERVAL_SECONDS, min(int(self.scan_interval_seconds), 300))
        if self.entry_style not in {"smart", "maker", "taker"}:
            self.entry_style = "smart"

        self.max_daily_loss_pct = max(0.005, min(float(self.max_daily_loss_pct), 0.25))
        self.max_drawdown_pct = max(0.01, min(float(self.max_drawdown_pct), 0.50))
        self.max_consecutive_losses = max(1, min(int(self.max_consecutive_losses), 20))
        self.paper_starting_equity = max(1.0, float(self.paper_starting_equity))
        return self

    @property
    def effective_position_pct(self) -> float:
        # Risk can only reduce the explicit cap; it can never raise it.
        return min(self.max_position_pct, self.HARD_MAX_POSITION_PCT) * (self.risk_level / 100.0)

    @property
    def effective_order_pct(self) -> float:
        return min(self.max_order_pct, self.HARD_MAX_ORDER_PCT) * (self.risk_level / 100.0)

    @property
    def min_signal_probability(self) -> float:
        # Activity changes selectivity, not size. 0 => 86%, 100 => 68%.
        return 0.86 - (0.18 * (self.trade_activity / 100.0))

    @property
    def min_expected_value_dollars(self) -> float:
        # Higher activity tolerates smaller positive edges but never negative EV.
        return 0.08 - (0.07 * (self.trade_activity / 100.0))

    @property
    def live_execution_unlocked(self) -> bool:
        return os.getenv("PIP_LIVE_EXECUTION_ENABLED", "false").lower() == "true"

    def can_submit_real_money(self) -> bool:
        return self.mode == "live" and self.live_execution_unlocked

    def to_public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.update(
            effective_position_pct=self.effective_position_pct,
            effective_order_pct=self.effective_order_pct,
            min_signal_probability=self.min_signal_probability,
            min_expected_value_dollars=self.min_expected_value_dollars,
            live_execution_unlocked=self.live_execution_unlocked,
        )
        return data

    def update_from_dict(self, patch: dict[str, Any]) -> "PipConfig":
        editable = {
            "mode", "agent_enabled", "auto_trade", "risk_level", "trade_activity",
            "max_position_pct", "max_order_pct", "max_total_exposure_pct",
            "min_cash_reserve_pct", "max_open_positions", "min_contract_price",
            "max_contract_price", "take_profit_cents", "stop_loss_cents",
            "max_hold_minutes", "min_volume_24h", "max_spread_cents",
            "shortlist_size", "scan_interval_seconds", "entry_style",
            "max_daily_loss_pct", "max_drawdown_pct", "max_consecutive_losses",
            "signal_horizon_minutes",
        }
        for key, value in patch.items():
            if key in editable:
                setattr(self, key, value)
        return self.normalize()

    @classmethod
    def from_json(cls, raw: str | None) -> "PipConfig":
        if not raw:
            return cls().normalize()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return cls().normalize()
        fields = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in payload.items() if k in fields}).normalize()
