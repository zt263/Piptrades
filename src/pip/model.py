from __future__ import annotations

import json
from dataclasses import dataclass, field

from .math import sigmoid

FEATURES = [
    "imbalance",
    "momentum",
    "spread_quality",
    "liquidity",
    "volume",
    "price_extremity",
]


@dataclass
class PipSignalModel:
    # Conservative bootstrap weights. They are updated only from forward observations.
    bias: float = 0.55
    weights: dict[str, float] = field(default_factory=lambda: {
        "imbalance": 0.80,
        "momentum": 0.55,
        "spread_quality": 0.45,
        "liquidity": 0.30,
        "volume": 0.25,
        "price_extremity": 0.15,
    })
    learning_rate: float = 0.06
    observations: int = 0

    def predict(self, features: dict[str, float]) -> float:
        z = self.bias
        for name in FEATURES:
            z += self.weights.get(name, 0.0) * float(features.get(name, 0.0))
        return max(0.05, min(0.95, sigmoid(z)))

    def update(self, features: dict[str, float], outcome: int) -> None:
        y = 1.0 if outcome else 0.0
        p = self.predict(features)
        err = y - p
        self.bias += self.learning_rate * err
        for name in FEATURES:
            self.weights[name] = self.weights.get(name, 0.0) + (
                self.learning_rate * err * float(features.get(name, 0.0))
            )
        self.observations += 1

    def to_json(self) -> str:
        return json.dumps({
            "bias": self.bias,
            "weights": self.weights,
            "learning_rate": self.learning_rate,
            "observations": self.observations,
        })

    @classmethod
    def from_json(cls, raw: str | None) -> "PipSignalModel":
        if not raw:
            return cls()
        try:
            data = json.loads(raw)
            return cls(
                bias=float(data.get("bias", 0.55)),
                weights={**cls().weights, **data.get("weights", {})},
                learning_rate=float(data.get("learning_rate", 0.06)),
                observations=int(data.get("observations", 0)),
            )
        except Exception:
            return cls()
