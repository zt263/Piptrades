from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from .config import PipConfig
from .model import PipSignalModel


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class PipStore:
    def __init__(self, path: str | None = None):
        self.path = path or os.getenv("PIP_DB_PATH", "data/pip.db")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)

    async def initialize(self):
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;

                CREATE TABLE IF NOT EXISTS pip_settings (
                    id INTEGER PRIMARY KEY CHECK (id=1),
                    json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pip_model (
                    id INTEGER PRIMARY KEY CHECK (id=1),
                    json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pip_opportunities (
                    ticker TEXT NOT NULL,
                    side TEXT NOT NULL,
                    title TEXT,
                    observed_at TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    target_price REAL NOT NULL,
                    stop_price REAL NOT NULL,
                    spread REAL NOT NULL,
                    probability REAL NOT NULL,
                    expected_value REAL NOT NULL,
                    score REAL NOT NULL,
                    quantity INTEGER NOT NULL,
                    features_json TEXT NOT NULL,
                    PRIMARY KEY (ticker, side)
                );

                CREATE TABLE IF NOT EXISTS pip_positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    side TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    intended_entry REAL NOT NULL,
                    entry_price REAL,
                    entry_fee REAL NOT NULL DEFAULT 0,
                    target_price REAL NOT NULL,
                    stop_price REAL NOT NULL,
                    opened_at TEXT,
                    created_at TEXT NOT NULL,
                    max_hold_minutes INTEGER NOT NULL,
                    entry_order_id TEXT,
                    exit_order_id TEXT,
                    last_bid REAL,
                    rationale TEXT
                );

                CREATE TABLE IF NOT EXISTS pip_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    side TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL NOT NULL,
                    entry_fee REAL NOT NULL,
                    exit_fee REAL NOT NULL,
                    pnl REAL NOT NULL,
                    opened_at TEXT NOT NULL,
                    closed_at TEXT NOT NULL,
                    exit_reason TEXT NOT NULL,
                    rationale TEXT
                );

                CREATE TABLE IF NOT EXISTS pip_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    level TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    message TEXT NOT NULL,
                    payload_json TEXT
                );

                CREATE TABLE IF NOT EXISTS pip_equity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recorded_at TEXT NOT NULL,
                    equity REAL NOT NULL,
                    cash REAL NOT NULL,
                    exposure REAL NOT NULL,
                    realized_pnl REAL NOT NULL,
                    unrealized_pnl REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pip_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    side TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    target_price REAL NOT NULL,
                    stop_price REAL NOT NULL,
                    probability REAL NOT NULL,
                    features_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    outcome INTEGER,
                    resolved_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_pip_positions_status ON pip_positions(status);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_pip_one_active_position
                  ON pip_positions(ticker, side)
                  WHERE status IN ('pending_entry','open','pending_exit');
                CREATE INDEX IF NOT EXISTS idx_pip_trades_closed ON pip_trades(closed_at);
                CREATE INDEX IF NOT EXISTS idx_pip_signals_pending ON pip_signals(status, ticker, side);
                """
            )
            await db.commit()

        if await self.get_raw_setting() is None:
            await self.save_config(PipConfig().normalize())
        if await self.get_raw_model() is None:
            await self.save_model(PipSignalModel())

    async def get_raw_setting(self) -> str | None:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT json FROM pip_settings WHERE id=1")
            row = await cur.fetchone()
            return row[0] if row else None

    async def load_config(self) -> PipConfig:
        return PipConfig.from_json(await self.get_raw_setting())

    async def save_config(self, config: PipConfig):
        raw = json.dumps(config.normalize().__dict__)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO pip_settings(id,json,updated_at) VALUES(1,?,?) "
                "ON CONFLICT(id) DO UPDATE SET json=excluded.json, updated_at=excluded.updated_at",
                (raw, utcnow()),
            )
            await db.commit()

    async def get_raw_model(self) -> str | None:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT json FROM pip_model WHERE id=1")
            row = await cur.fetchone()
            return row[0] if row else None

    async def load_model(self) -> PipSignalModel:
        return PipSignalModel.from_json(await self.get_raw_model())

    async def save_model(self, model: PipSignalModel):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO pip_model(id,json,updated_at) VALUES(1,?,?) "
                "ON CONFLICT(id) DO UPDATE SET json=excluded.json, updated_at=excluded.updated_at",
                (model.to_json(), utcnow()),
            )
            await db.commit()

    async def event(self, kind: str, message: str, level: str = "info", payload: dict | None = None):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO pip_events(created_at,level,kind,message,payload_json) VALUES(?,?,?,?,?)",
                (utcnow(), level, kind, message, json.dumps(payload or {})),
            )
            await db.commit()

    async def recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM pip_events ORDER BY id DESC LIMIT ?", (limit,)
            )
            return [dict(r) for r in await cur.fetchall()]

    async def replace_opportunities(self, rows: list[dict[str, Any]]):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM pip_opportunities")
            for r in rows:
                await db.execute(
                    """
                    INSERT INTO pip_opportunities(
                      ticker,side,title,observed_at,entry_price,target_price,stop_price,
                      spread,probability,expected_value,score,quantity,features_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        r["ticker"], r["side"], r.get("title"), r["observed_at"],
                        r["entry_price"], r["target_price"], r["stop_price"], r["spread"],
                        r["probability"], r["expected_value"], r["score"], r["quantity"],
                        json.dumps(r["features"]),
                    ),
                )
            await db.commit()

    async def opportunities(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM pip_opportunities ORDER BY score DESC")
            out = []
            for row in await cur.fetchall():
                d = dict(row)
                d["features"] = json.loads(d.pop("features_json"))
                for key in (
                    "strategy", "reviewable_exploration", "minutes_to_close",
                    "break_even_probability", "max_hold_minutes",
                    "net_win_if_target", "net_loss_if_stop",
                ):
                    if key in d["features"]:
                        d[key] = d["features"][key]
                out.append(d)
            return out

    async def add_signal(self, opportunity: dict[str, Any], expires_at: str):
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT 1 FROM pip_signals WHERE ticker=? AND side=? AND status='pending' LIMIT 1",
                (opportunity["ticker"], opportunity["side"]),
            )
            if await cur.fetchone():
                return
            await db.execute(
                """
                INSERT INTO pip_signals(
                  ticker,side,observed_at,expires_at,entry_price,target_price,stop_price,
                  probability,features_json,status
                ) VALUES(?,?,?,?,?,?,?,?,?,'pending')
                """,
                (
                    opportunity["ticker"], opportunity["side"], opportunity["observed_at"], expires_at,
                    opportunity["entry_price"], opportunity["target_price"], opportunity["stop_price"],
                    opportunity["probability"], json.dumps(opportunity["features"]),
                ),
            )
            await db.commit()

    async def pending_signals(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM pip_signals WHERE status='pending'")
            rows = []
            for r in await cur.fetchall():
                d = dict(r)
                d["features"] = json.loads(d.pop("features_json"))
                rows.append(d)
            return rows

    async def resolve_signal(self, signal_id: int, outcome: int):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE pip_signals SET status='resolved', outcome=?, resolved_at=? WHERE id=?",
                (int(outcome), utcnow(), signal_id),
            )
            await db.commit()

    async def create_position(self, row: dict[str, Any]) -> int | None:
        try:
            async with aiosqlite.connect(self.path) as db:
                cur = await db.execute(
                    """
                    INSERT INTO pip_positions(
                      ticker,side,mode,status,quantity,intended_entry,entry_price,entry_fee,
                      target_price,stop_price,opened_at,created_at,max_hold_minutes,entry_order_id,
                      exit_order_id,last_bid,rationale
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        row["ticker"], row["side"], row["mode"], row["status"], row["quantity"],
                        row["intended_entry"], row.get("entry_price"), row.get("entry_fee", 0),
                        row["target_price"], row["stop_price"], row.get("opened_at"), utcnow(),
                        row["max_hold_minutes"], row.get("entry_order_id"), row.get("exit_order_id"),
                        row.get("last_bid"), row.get("rationale"),
                    ),
                )
                await db.commit()
                return cur.lastrowid
        except aiosqlite.IntegrityError:
            return None

    async def update_position(self, position_id: int, **fields):
        allowed = {
            "status", "entry_price", "entry_fee", "opened_at", "entry_order_id",
            "exit_order_id", "last_bid", "quantity", "rationale",
        }
        patch = {k: v for k, v in fields.items() if k in allowed}
        if not patch:
            return
        sql = ", ".join(f"{k}=?" for k in patch)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                f"UPDATE pip_positions SET {sql} WHERE id=?",
                (*patch.values(), position_id),
            )
            await db.commit()

    async def positions(self, statuses: tuple[str, ...] = ("pending_entry", "open", "pending_exit")) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in statuses)
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                f"SELECT * FROM pip_positions WHERE status IN ({placeholders}) ORDER BY id DESC",
                statuses,
            )
            return [dict(r) for r in await cur.fetchall()]

    async def close_position(
        self,
        position: dict[str, Any],
        *,
        exit_price: float,
        exit_fee: float,
        exit_reason: str,
    ) -> float:
        entry = float(position["entry_price"])
        qty = int(position["quantity"])
        pnl = ((float(exit_price) - entry) * qty) - float(position.get("entry_fee") or 0) - float(exit_fee)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE pip_positions SET status='closed', last_bid=? WHERE id=?", (exit_price, position["id"]))
            await db.execute(
                """
                INSERT INTO pip_trades(
                  ticker,side,mode,quantity,entry_price,exit_price,entry_fee,exit_fee,pnl,
                  opened_at,closed_at,exit_reason,rationale
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    position["ticker"], position["side"], position["mode"], qty, entry, exit_price,
                    float(position.get("entry_fee") or 0), float(exit_fee), pnl,
                    position["opened_at"] or position["created_at"], utcnow(), exit_reason,
                    position.get("rationale"),
                ),
            )
            await db.commit()
        return pnl

    async def record_partial_exit(
        self,
        position: dict[str, Any],
        *,
        filled_quantity: int,
        exit_price: float,
        exit_fee: float,
        exit_reason: str,
    ) -> float:
        total_qty = int(position["quantity"])
        filled = max(0, min(int(filled_quantity), total_qty))
        if filled <= 0:
            return 0.0
        if filled >= total_qty:
            return await self.close_position(
                position,
                exit_price=exit_price,
                exit_fee=exit_fee,
                exit_reason=exit_reason,
            )

        entry = float(position["entry_price"])
        total_entry_fee = float(position.get("entry_fee") or 0)
        entry_fee_share = total_entry_fee * (filled / total_qty)
        remaining_entry_fee = max(0.0, total_entry_fee - entry_fee_share)
        pnl = ((float(exit_price) - entry) * filled) - entry_fee_share - float(exit_fee)
        remaining_qty = total_qty - filled

        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE pip_positions SET quantity=?, entry_fee=?, status='open', exit_order_id=NULL, last_bid=? WHERE id=?",
                (remaining_qty, remaining_entry_fee, exit_price, position["id"]),
            )
            await db.execute(
                """
                INSERT INTO pip_trades(
                  ticker,side,mode,quantity,entry_price,exit_price,entry_fee,exit_fee,pnl,
                  opened_at,closed_at,exit_reason,rationale
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    position["ticker"], position["side"], position["mode"], filled, entry, exit_price,
                    entry_fee_share, float(exit_fee), pnl,
                    position["opened_at"] or position["created_at"], utcnow(),
                    exit_reason + "_partial", position.get("rationale"),
                ),
            )
            await db.commit()
        return pnl

    async def trades(self, limit: int = 100) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM pip_trades ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(r) for r in await cur.fetchall()]

    async def realized_pnl(self) -> float:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT COALESCE(SUM(pnl),0) FROM pip_trades")
            return float((await cur.fetchone())[0])

    async def daily_realized_pnl(self) -> float:
        day = datetime.now(timezone.utc).date().isoformat()
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT COALESCE(SUM(pnl),0) FROM pip_trades WHERE substr(closed_at,1,10)=?", (day,)
            )
            return float((await cur.fetchone())[0])

    async def consecutive_losses(self) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT pnl FROM pip_trades ORDER BY id DESC LIMIT 50")
            count = 0
            for (pnl,) in await cur.fetchall():
                if pnl < 0:
                    count += 1
                else:
                    break
            return count

    async def record_equity(
        self,
        equity: float,
        cash: float,
        exposure: float,
        realized_pnl: float,
        unrealized_pnl: float,
    ):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO pip_equity(recorded_at,equity,cash,exposure,realized_pnl,unrealized_pnl) VALUES(?,?,?,?,?,?)",
                (utcnow(), equity, cash, exposure, realized_pnl, unrealized_pnl),
            )
            await db.commit()

    async def peak_equity(self, default: float) -> float:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT MAX(equity) FROM pip_equity")
            row = await cur.fetchone()
            return float(row[0]) if row and row[0] is not None else float(default)
