"""
Dashboard — Lightweight web server providing real-time bot status.
Uses aiohttp.web (already a dependency) to serve JSON API + HTML frontend.
Runs on port 8080 alongside the main bot.
"""

from __future__ import annotations
import os
import json
from decimal import Decimal
from datetime import datetime
from typing import TYPE_CHECKING
from aiohttp import web
import logging

if TYPE_CHECKING:
    from main import Bot

logger = logging.getLogger(__name__)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


class DecimalEncoder(json.JSONEncoder):
    """JSON encoder that handles Decimal types."""
    def default(self, o):
        if isinstance(o, Decimal):
            return float(o)
        if isinstance(o, datetime):
            return o.isoformat()
        return super().default(o)


def json_response(data, status=200):
    return web.Response(
        text=json.dumps(data, cls=DecimalEncoder),
        content_type="application/json",
        status=status,
    )


class Dashboard:
    """Web dashboard server."""

    def __init__(self, bot: "Bot", port: int = 8080):
        self.bot = bot
        self.port = port
        self.app = web.Application()
        self._setup_routes()

    def _setup_routes(self):
        self.app.router.add_get("/", self._serve_html)
        self.app.router.add_get("/api/dashboard", self._api_dashboard)
        self.app.router.add_get("/api/trades", self._api_trades)
        self.app.router.add_get("/api/logs", self._api_logs)

    async def start(self):
        """Start the dashboard web server."""
        runner = web.AppRunner(self.app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.port)
        await site.start()
        logger.info(f"[DASHBOARD] Running on http://0.0.0.0:{self.port}")

    # ─── Routes ───

    async def _serve_html(self, request: web.Request) -> web.Response:
        """Serve the dashboard HTML."""
        html_path = os.path.join(STATIC_DIR, "dashboard.html")
        if os.path.exists(html_path):
            with open(html_path, "r") as f:
                return web.Response(text=f.read(), content_type="text/html")
        return web.Response(text="Dashboard HTML not found", status=404)

    async def _api_dashboard(self, request: web.Request) -> web.Response:
        """Main dashboard data endpoint — returns everything in one call."""
        try:
            bot = self.bot
            se = bot.signal_engine
            ha = bot.ha_engine

            # Overview
            now = datetime.utcnow()
            uptime_seconds = (now - se._start_time).total_seconds()
            total_balance = float(bot.db.get_total_balance())
            kill_switch = bot.db.get_state("kill_switch_triggered") == "true"

            overview = {
                "mode": "DRY RUN" if bot.config.execution.dry_run else "LIVE",
                "total_balance": total_balance,
                "initial_balance": float(bot.config.slots.num_slots * bot.config.slots.initial_balance),
                "total_pnl": total_balance - float(bot.config.slots.num_slots * bot.config.slots.initial_balance),
                "uptime_seconds": int(uptime_seconds),
                "kill_switch_active": not kill_switch,
                "kill_switch_triggered": kill_switch,
                "kill_switch_threshold": float(bot.config.risk.kill_switch_threshold),
                "last_data_seconds_ago": int((now - se.last_data_time).total_seconds()),
                "ws_public_connected": bot.ws._public_ws is not None,
                "ws_private_connected": bot.ws._private_ws is not None,
                "leverage": bot.config.slots.leverage,
                "num_coins": len(bot.coin_selector.symbols),
            }

            # Coins — HA status + price + ATR
            coins = []
            for symbol in bot.coin_selector.symbols:
                ha_series = ha._ha_series.get(symbol, [])
                last_ha = ha_series[-1] if ha_series else None

                # Find last flip time by scanning backward
                last_flip_time = None
                if len(ha_series) >= 2:
                    for i in range(len(ha_series) - 1, 0, -1):
                        if ha_series[i].is_bullish != ha_series[i - 1].is_bullish:
                            ts = ha_series[i].timestamp
                            last_flip_time = datetime.utcfromtimestamp(ts / 1000 if ts > 1e12 else ts).isoformat()
                            break

                coins.append({
                    "symbol": symbol,
                    "ha_direction": "BULL" if (last_ha and last_ha.is_bullish) else "BEAR",
                    "price": float(se._prices.get(symbol, 0)),
                    "atr": float(bot.atr_calc.get_atr(symbol) or 0),
                    "last_flip": last_flip_time,
                    "in_cooldown": se._is_in_cooldown(symbol),
                    "in_trade": bot.coin_selector.is_in_trade(symbol) if hasattr(bot.coin_selector, 'is_in_trade') else False,
                })

            # Slots
            slots = []
            for slot in bot.db.get_all_slots():
                slots.append({
                    "id": slot.id,
                    "balance": float(slot.balance),
                    "state": slot.state.value,
                    "symbol": slot.current_symbol,
                    "trade_id": slot.current_trade_id,
                    "total_trades": slot.total_trades,
                    "total_pnl": float(slot.total_pnl),
                })

            # Recent signals
            signals = list(reversed(se._signal_log[-20:]))

            # Active trades
            active_trades = []
            for trade in bot.db.get_open_trades():
                current_price = float(se._prices.get(trade.symbol, 0))
                entry_price = float(trade.entry_price) if trade.entry_price else 0

                # Calculate unrealized P&L
                unrealized_pnl = 0
                if entry_price > 0 and current_price > 0 and trade.qty:
                    qty = float(trade.qty)
                    if trade.side.value == "Buy":
                        unrealized_pnl = (current_price - entry_price) * qty
                    else:
                        unrealized_pnl = (entry_price - current_price) * qty

                active_trades.append({
                    "id": trade.id,
                    "slot_id": trade.slot_id,
                    "symbol": trade.symbol,
                    "side": trade.side.value,
                    "entry_price": entry_price,
                    "current_price": current_price,
                    "qty": float(trade.qty) if trade.qty else 0,
                    "current_sl": float(trade.current_sl_price) if trade.current_sl_price else 0,
                    "highest_tp": trade.highest_tp_reached,
                    "unrealized_pnl": round(unrealized_pnl, 4),
                    "entry_time": trade.entry_time.isoformat() if trade.entry_time else None,
                })

            return json_response({
                "overview": overview,
                "coins": coins,
                "slots": slots,
                "signals": signals,
                "active_trades": active_trades,
                "timestamp": now.isoformat(),
            })

        except Exception as e:
            logger.error(f"[DASHBOARD] API error: {e}", exc_info=True)
            return json_response({"error": str(e)}, status=500)

    async def _api_trades(self, request: web.Request) -> web.Response:
        """Return recent trade history."""
        try:
            rows = self.bot.db.conn.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT 50"
            ).fetchall()

            trades = []
            for r in rows:
                trades.append({
                    "id": r["id"],
                    "slot_id": r["slot_id"],
                    "symbol": r["symbol"],
                    "side": r["side"],
                    "entry_price": r["entry_price"],
                    "qty": r["qty"],
                    "status": r["status"],
                    "pnl": r["pnl"],
                    "exit_reason": r["exit_reason"],
                    "highest_tp": r["highest_tp_reached"],
                    "entry_time": r["entry_time"],
                    "exit_time": r["exit_time"],
                })
            return json_response({"trades": trades})

        except Exception as e:
            logger.error(f"[DASHBOARD] Trades API error: {e}")
            return json_response({"error": str(e)}, status=500)

    async def _api_logs(self, request: web.Request) -> web.Response:
        """Return last N lines from bot.log."""
        try:
            n = int(request.query.get("n", 50))
            log_path = "data/bot.log"
            lines = []
            if os.path.exists(log_path):
                with open(log_path, "r") as f:
                    all_lines = f.readlines()
                    lines = [l.strip() for l in all_lines[-n:]]
            return json_response({"lines": lines, "total": len(lines)})
        except Exception as e:
            return json_response({"error": str(e)}, status=500)
