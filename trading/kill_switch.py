"""
Kill Switch — Global drawdown monitor.
If total balance drops below threshold, closes everything and pauses.
"""

from __future__ import annotations
import asyncio
from decimal import Decimal
from typing import TYPE_CHECKING
import logging

if TYPE_CHECKING:
    from trading.slot_manager import SlotManager
    from trading.risk_manager import RiskManager
    from trading.order_executor import OrderExecutor
    from exchange.bybit_rest import BybitRestClient
    from storage.database import Database
    from notifications.telegram import TelegramNotifier
    from config import RiskConfig

logger = logging.getLogger(__name__)


class KillSwitch:
    """
    Monitors total portfolio value and triggers emergency shutdown
    if drawdown threshold is breached.
    """

    def __init__(
        self,
        config: "RiskConfig",
        slot_manager: "SlotManager",
        risk_manager: "RiskManager",
        order_executor: "OrderExecutor",
        client: "BybitRestClient",
        db: "Database",
        notifier: "TelegramNotifier",
    ):
        self.config = config
        self.slot_manager = slot_manager
        self.risk_manager = risk_manager
        self.order_executor = order_executor
        self.client = client
        self.db = db
        self.notifier = notifier
        self._triggered = False
        self._running = False

    @property
    def is_triggered(self) -> bool:
        return self._triggered

    async def start(self):
        """Start the kill switch monitoring loop."""
        self._running = True
        logger.info(
            f"[KILLSWITCH] Active. Threshold: ${self.config.kill_switch_threshold}. "
            f"Check interval: {self.config.kill_switch_check_interval}s"
        )

        while self._running:
            try:
                await self._check()
            except Exception as e:
                logger.error(f"[KILLSWITCH] Check error: {e}", exc_info=True)

            await asyncio.sleep(self.config.kill_switch_check_interval)

    async def stop(self):
        self._running = False

    async def _check(self):
        """Check total balance against threshold."""
        if self._triggered:
            return

        # Get unrealized P&L from exchange
        unrealized_pnl = Decimal("0")
        try:
            positions = await self.client.get_positions()
            for pos in positions:
                size = Decimal(pos.get("size", "0"))
                if size > 0:
                    unrealized_pnl += Decimal(pos.get("unrealisedPnl", "0"))
        except Exception as e:
            logger.warning(f"[KILLSWITCH] Failed to get positions: {e}")

        total = self.slot_manager.get_total_balance_with_positions(unrealized_pnl)

        if total < self.config.kill_switch_threshold:
            logger.critical(
                f"[KILLSWITCH] ⚠️ TRIGGERED! Total: ${total:.2f} "
                f"< threshold ${self.config.kill_switch_threshold}"
            )
            await self._execute_shutdown(total)

    async def _execute_shutdown(self, total_balance: Decimal):
        """Emergency shutdown — close everything."""
        self._triggered = True
        self.db.set_state("kill_switch_triggered", "true")

        # 1. Cancel all open orders
        try:
            orders = await self.client.get_open_orders()
            for order in orders:
                try:
                    await self.client.cancel_order(
                        order["symbol"], order["orderId"]
                    )
                except Exception as e:
                    logger.error(f"[KILLSWITCH] Cancel order error: {e}")
            logger.info(f"[KILLSWITCH] Cancelled {len(orders)} open orders")
        except Exception as e:
            logger.error(f"[KILLSWITCH] Error cancelling orders: {e}")

        # 2. Close all positions at market
        try:
            positions = await self.client.get_positions()
            for pos in positions:
                size = Decimal(pos.get("size", "0"))
                if size > 0:
                    try:
                        await self.client.close_position_market(
                            symbol=pos["symbol"],
                            side=pos["side"],
                            qty=str(size),
                        )
                        logger.info(f"[KILLSWITCH] Closed {pos['symbol']} position")
                    except Exception as e:
                        logger.error(f"[KILLSWITCH] Close position error: {e}")
        except Exception as e:
            logger.error(f"[KILLSWITCH] Error closing positions: {e}")

        # 3. Mark all active trades as closed
        for trade in self.risk_manager.get_all_active_trades():
            from exchange.models import ExitReason
            self.risk_manager.handle_trade_closed(
                trade,
                exit_reason=ExitReason.KILL_SWITCH,
                pnl=Decimal("0"),  # Will be reconciled
                fees=Decimal("0"),
            )

        # 4. Send notification
        msg = (
            f"🚨 KILL SWITCH TRIGGERED 🚨\n\n"
            f"Total balance: ${total_balance:.2f}\n"
            f"Threshold: ${self.config.kill_switch_threshold}\n\n"
            f"All positions closed. All orders cancelled.\n"
            f"Bot is PAUSED. Manual restart required."
        )
        await self.notifier.send(msg)

        logger.critical("[KILLSWITCH] Shutdown complete. Bot paused.")
