"""
Telegram Notifier — Sends trade alerts, status updates, and kill switch warnings.
"""

from __future__ import annotations
import aiohttp
from typing import Optional
import logging

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Sends messages via Telegram Bot API."""

    def __init__(self, bot_token: str, chat_id: str, enabled: bool = True):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = enabled and bool(bot_token) and bool(chat_id)
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def send(self, message: str, parse_mode: str = "HTML"):
        """Send a message to the configured chat."""
        if not self.enabled:
            logger.debug(f"[TG] (disabled) Would send: {message[:100]}...")
            return

        try:
            session = await self._get_session()
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }

            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(f"[TG] Send failed ({resp.status}): {body[:200]}")
                else:
                    logger.debug(f"[TG] Sent: {message[:80]}...")

        except Exception as e:
            logger.warning(f"[TG] Error sending message: {e}")

    async def send_trade_entry(
        self,
        symbol: str,
        side: str,
        entry_price: str,
        qty: str,
        sl_price: str,
        slot_id: int,
        slot_balance: str,
    ):
        """Send trade entry notification."""
        emoji = "🟢" if side == "Buy" else "🔴"
        direction = "LONG" if side == "Buy" else "SHORT"
        msg = (
            f"{emoji} <b>NEW TRADE — {direction}</b>\n\n"
            f"Symbol: <code>{symbol}</code>\n"
            f"Entry: <code>{entry_price}</code>\n"
            f"Qty: <code>{qty}</code>\n"
            f"SL: <code>{sl_price}</code> (-2.5%)\n\n"
            f"Slot: #{slot_id} (${slot_balance})"
        )
        await self.send(msg)

    async def send_trade_exit(
        self,
        symbol: str,
        side: str,
        pnl: str,
        exit_reason: str,
        slot_id: int,
        new_balance: str,
        highest_tp: int,
    ):
        """Send trade exit notification."""
        pnl_val = float(pnl)
        emoji = "✅" if pnl_val >= 0 else "❌"
        direction = "LONG" if side == "Buy" else "SHORT"
        msg = (
            f"{emoji} <b>TRADE CLOSED — {direction}</b>\n\n"
            f"Symbol: <code>{symbol}</code>\n"
            f"PnL: <code>${pnl}</code>\n"
            f"Reason: {exit_reason}\n"
            f"Highest TP: {highest_tp}/10\n\n"
            f"Slot #{slot_id} → ${new_balance}"
        )
        await self.send(msg)

    async def send_sl_trailed(self, symbol: str, new_sl: str, tp_level: int):
        """Send SL trail notification."""
        msg = (
            f"📈 <b>SL TRAILED</b>\n"
            f"Symbol: <code>{symbol}</code>\n"
            f"New SL: <code>{new_sl}</code> (TP{tp_level} hit)"
        )
        await self.send(msg)

    async def send_daily_summary(self, summary: str):
        """Send daily P&L summary."""
        msg = f"📊 <b>DAILY SUMMARY</b>\n\n{summary}"
        await self.send(msg)

    async def send_bot_status(self, status: str):
        """Send bot lifecycle status."""
        await self.send(f"🤖 <b>BOT</b>: {status}")
