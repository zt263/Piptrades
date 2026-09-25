from __future__ import annotations

import asyncio
import os
from pathlib import Path

from aiohttp import web

from src.pip.engine import PipEngine
from src.pip.kalshi import PipKalshiClient
from src.pip.store import PipStore

ROOT = Path(__file__).resolve().parent
DASHBOARD = ROOT / "static" / "pip.html"


class PipWebApp:
    def __init__(self):
        self.store = PipStore()
        self.kalshi = PipKalshiClient()
        self.engine = PipEngine(self.store, self.kalshi)
        self.dashboard_token = os.getenv("PIP_DASHBOARD_TOKEN", "")
        if os.getenv("RAILWAY_ENVIRONMENT") and not self.dashboard_token:
            raise RuntimeError("PIP_DASHBOARD_TOKEN is required on Railway")

    def authorized(self, request: web.Request) -> bool:
        if not self.dashboard_token:
            return True
        supplied = request.headers.get("X-Pip-Token") or request.query.get("token") or ""
        return supplied == self.dashboard_token

    @web.middleware
    async def auth_middleware(self, request: web.Request, handler):
        if request.path.startswith("/api/") and not self.authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    async def index(self, request):
        return web.FileResponse(DASHBOARD)

    async def api_status(self, request):
        return web.json_response(await self.engine.status())

    async def api_settings_get(self, request):
        return web.json_response((await self.store.load_config()).to_public_dict())

    async def api_settings_post(self, request):
        patch = await request.json()
        config = await self.store.load_config()
        before_mode = config.mode
        config.update_from_dict(patch)
        if config.mode == "live" and not config.live_execution_unlocked:
            config.auto_trade = False
        await self.store.save_config(config)
        await self.store.event(
            "settings",
            "Pip settings updated",
            payload={"changed": list(patch.keys()), "mode_before": before_mode, "mode_after": config.mode},
        )
        return web.json_response(config.to_public_dict())

    async def api_opportunities(self, request):
        return web.json_response(await self.store.opportunities())

    async def api_positions(self, request):
        return web.json_response(await self.store.positions())

    async def api_trades(self, request):
        return web.json_response(await self.store.trades(100))

    async def api_events(self, request):
        return web.json_response(await self.store.recent_events(100))

    async def api_scan(self, request):
        asyncio.create_task(self.engine.scan_once())
        return web.json_response({"ok": True, "message": "scan started"})

    async def api_start(self, request):
        config = await self.store.load_config()
        config.agent_enabled = True
        config.auto_trade = not (config.mode == "live" and not config.live_execution_unlocked)
        await self.store.save_config(config)
        await self.store.event("operator", "Pip started by operator")
        return web.json_response(config.to_public_dict())

    async def api_pause(self, request):
        config = await self.store.load_config()
        config.agent_enabled = False
        await self.store.save_config(config)
        await self.engine.cancel_pending_entries()
        await self.store.event("operator", "Pip paused by operator; existing positions still managed", "warning")
        return web.json_response(config.to_public_dict())

    async def api_kill(self, request):
        config = await self.store.load_config()
        config.agent_enabled = False
        config.auto_trade = False
        await self.store.save_config(config)
        await self.engine.cancel_pending_entries()
        await self.store.event("operator", "EMERGENCY KILL: new trading disabled; existing positions still managed", "error")
        return web.json_response(config.to_public_dict())

    async def health(self, request):
        return web.json_response({"ok": True, "service": "piptrades"})

    async def on_startup(self, app):
        await self.store.initialize()
        await self.engine.start_background()

    async def on_cleanup(self, app):
        await self.engine.stop_background()
        await self.kalshi.close()

    def build(self):
        app = web.Application(middlewares=[self.auth_middleware])
        app.router.add_get("/", self.index)
        app.router.add_get("/health", self.health)
        app.router.add_get("/api/status", self.api_status)
        app.router.add_get("/api/settings", self.api_settings_get)
        app.router.add_post("/api/settings", self.api_settings_post)
        app.router.add_get("/api/opportunities", self.api_opportunities)
        app.router.add_get("/api/positions", self.api_positions)
        app.router.add_get("/api/trades", self.api_trades)
        app.router.add_get("/api/events", self.api_events)
        app.router.add_post("/api/scan", self.api_scan)
        app.router.add_post("/api/agent/start", self.api_start)
        app.router.add_post("/api/agent/pause", self.api_pause)
        app.router.add_post("/api/agent/kill", self.api_kill)
        app.on_startup.append(self.on_startup)
        app.on_cleanup.append(self.on_cleanup)
        return app


def main():
    port = int(os.getenv("PORT", "8080"))
    web.run_app(PipWebApp().build(), host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
