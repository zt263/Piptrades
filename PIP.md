# Pip

Pip is the short-term prediction-market trading experiment built on top of this fork.

It is intentionally isolated under `src/pip/` so the upstream Kalshi toolkit remains available as reference code while Pip can use its own execution, risk, state, and dashboard logic.

## What Pip does

Pip scans open Kalshi markets for high-priced YES or NO contracts, currently 90–99 cents by default, and ranks short-term scalp opportunities.

The trading thesis is **not** "96 cents is safe." Pip tries to estimate whether the executable bid is likely to reach a configurable profit target within a short horizon after accounting for spread, liquidity, modeled fees, recent bid momentum, order-book imbalance, and downside.

The first model is deliberately simple and auditable. It starts with fixed bootstrap weights and updates from forward observations recorded after the signal was created.

## Hard risk rules

Defaults for a $100 paper bankroll:

- Maximum single position: **10% of equity**
- Maximum single order: **10% of equity**
- Maximum total exposure: **60% of equity**
- Minimum cash reserve: **20%**
- Target price band: **90–99 cents**
- Profit target: **2 cents**
- Stop: **3 cents**
- Maximum hold: **60 minutes**
- Daily loss pause: **5%**
- Account drawdown pause: **15%**
- Consecutive-loss pause: **5**

The Risk slider can only scale the 10% single-position and single-order limits downward. It cannot raise them.

The Trade Activity slider changes selectivity and how many qualifying trades may be opened. It does not increase the risk cap.

## Modes

### Paper

No authenticated Kalshi account is required. Pip uses public Kalshi market data and maintains a simulated $100 ledger.

Paper execution is deliberately conservative:

- taker entries fill at the displayed ask
- maker entries remain pending until the market reaches the order
- open positions are marked to executable bid
- target/stop/time exits cross the executable bid
- modeled fees are deducted

### Demo

Uses Kalshi's demo environment and demo credentials. Orders are sent to Kalshi demo, not to the production account.

Set:

```
KALSHI_ENV=demo
KALSHI_API_KEY_ID=...
KALSHI_PRIVATE_KEY_PEM=...
```

### Live

Selecting Live in the dashboard is not enough to enable real-money orders.

The server must also have:

```
KALSHI_ENV=production
PIP_LIVE_EXECUTION_ENABLED=true
```

This second gate is intentional.

## Pause and kill behavior

Pause and Emergency Kill stop **new entries** and cancel unfilled entry orders.

Pip continues scanning and managing already-open positions. A kill switch does not blindly liquidate positions into thin books.

## Run locally

```bash
pip install -r requirements-pip.txt
export PIP_DB_PATH=./data/pip.db
python pip_app.py
```

Open `http://localhost:8080`.

## Railway

Pip is designed to run as one always-on Railway service.

1. Create a Railway project from this GitHub repository.
2. Add a persistent volume mounted at `/data`.
3. Set `PIP_DB_PATH=/data/pip.db`.
4. Set a strong `PIP_DASHBOARD_TOKEN`.
5. Leave `PIP_LIVE_EXECUTION_ENABLED=false`.
6. Start in Paper mode.
7. Add Kalshi demo credentials when ready to test exchange execution.

Railway deployment uses the included `Dockerfile` and `railway.toml`.

When running on Railway, the app refuses to start if `PIP_DASHBOARD_TOKEN` is missing.

## Environment variables

See `.env.pip.example`.

Important variables:

| Variable | Purpose |
| --- | --- |
| `PIP_DB_PATH` | SQLite location. Use `/data/pip.db` with a Railway volume. |
| `PIP_DASHBOARD_TOKEN` | Protects operator APIs and dashboard controls. |
| `KALSHI_ENV` | `demo` or `production`. |
| `KALSHI_API_KEY_ID` | Kalshi API key ID. |
| `KALSHI_PRIVATE_KEY_PEM` | Private key stored only as a server secret. |
| `PIP_LIVE_EXECUTION_ENABLED` | Separate server-side gate for live-money order submission. |

## Mobile dashboard

The root page is a mobile-first operator dashboard for:

- bankroll and P&L
- exposure
- ranked opportunities
- open positions
- completed trades
- Risk slider
- Trade Activity slider
- Paper / Demo / Live mode
- Start / Pause / Emergency Kill
- scan status, connection state, and model sample count

On iPhone, open the Railway URL in Safari and use **Add to Home Screen**.

## Current boundary

Pip v1 is ready for paper testing and the next step is Kalshi demo validation.

Before production money is enabled, demo testing should validate:

- authentication
- order submission and cancellation
- partial fills
- entry/exit reconciliation
- fee assumptions
- disconnect/reconnect behavior
- risk pauses
- persistence across redeploys

The codebase inherits the upstream project's MIT license. Pip-specific additions remain under the same repository license.
