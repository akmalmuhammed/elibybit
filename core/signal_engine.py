"""
Signal Engine — Processes 4H candle data, detects HA flips,
and routes signals to the trading system.
"""

from __future__ import annotations
import asyncio
from decimal import Decimal
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, List, TYPE_CHECKING
from exchange.models import Candle, Signal, Trade, TradeStatus, Side, CoinInfo
import logging

if TYPE_CHECKING:
    from core.heiken_ashi import HeikenAshiEngine
    from core.atr import ATRCalculator
    from core.coin_selector import CoinSelector
    from trading.slot_manager import SlotManager
    from trading.order_executor import OrderExecutor
    from trading.risk_manager import RiskManager
    from exchange.bybit_rest import BybitRestClient
    from storage.database import Database
    from notifications.telegram import TelegramNotifier
    from config import BotConfig

logger = logging.getLogger(__name__)


class SignalEngine:
    """
    Core signal processing engine.
    Receives WebSocket data, processes it through HA engine,
    and orchestrates trade execution.
    """

    def __init__(
        self,
        config: "BotConfig",
        ha_engine: "HeikenAshiEngine",
        atr_calc: "ATRCalculator",
        coin_selector: "CoinSelector",
        slot_manager: "SlotManager",
        order_executor: "OrderExecutor",
        risk_manager: "RiskManager",
        client: "BybitRestClient",
        db: "Database",
        notifier: "TelegramNotifier",
    ):
        self.config = config
        self.ha = ha_engine
        self.atr = atr_calc
        self.coins = coin_selector
        self.slots = slot_manager
        self.executor = order_executor
        self.risk = risk_manager
        self.client = client
        self.db = db
        self.notifier = notifier

        # Per-asset cooldown tracking: symbol -> cooldown_until
        self._cooldowns: Dict[str, datetime] = {}

        # Option A flip tracking: symbol -> candle_start timestamp
        # Only allow ONE flip signal per 4H window per symbol
        self._flip_acted_this_window: Dict[str, int] = {}

        # Live 4H candle cache: symbol -> latest live Candle from kline.240
        # Updated every ~1-2s by WebSocket, but only READ on 5M candle close
        self._live_4h_candle: Dict[str, Candle] = {}

        # Lock to prevent concurrent signal processing
        self._signal_lock = asyncio.Lock()

        # Health tracking — updated on every WS message
        self.last_data_time: datetime = datetime.utcnow()

    # ==================== WebSocket Handlers ====================

    async def on_kline_240(self, topic: str, data: Dict[str, Any]):
        """
        Handle 4H kline WebSocket message.

        - confirm: false → Cache the live 4H candle (do NOT check for flip here)
        - confirm: true  → Store in HA chain, reset flip tracking for new window
        """
        self.last_data_time = datetime.utcnow()
        kline_data = data.get("data", [])
        if not kline_data:
            return

        for kline in kline_data:
            symbol = kline.get("symbol", "")
            if not symbol:
                parts = topic.split(".")
                if len(parts) >= 3:
                    symbol = parts[2]

            confirmed = kline.get("confirm", False)
            candle_start = int(kline.get("start", 0))

            candle = Candle(
                timestamp=candle_start,
                open=Decimal(str(kline.get("open", 0))),
                high=Decimal(str(kline.get("high", 0))),
                low=Decimal(str(kline.get("low", 0))),
                close=Decimal(str(kline.get("close", 0))),
                volume=Decimal(str(kline.get("volume", 0))),
                confirmed=confirmed,
            )

            if confirmed:
                # ═══ 4H CANDLE CLOSED ═══
                # Store in HA chain (becomes the new "previous" for next window)
                logger.info(f"[SIGNAL] {symbol}: 4H candle CONFIRMED. C={candle.close}")
                self.ha.update(symbol, candle)

                # Reset flip tracking — new 4H window starts
                self._flip_acted_this_window.pop(symbol, None)
                self._live_4h_candle.pop(symbol, None)
            else:
                # ═══ LIVE 4H UPDATE ═══
                # Just cache it. The 5M handler will read it.
                self._live_4h_candle[symbol] = candle

    async def on_kline_5(self, topic: str, data: Dict[str, Any]):
        """
        Handle 5M kline WebSocket message.
        This is the TRIGGER for HA flip checks — matches TV indicator behavior.

        On every confirmed 5M candle close:
          1. Read the cached live 4H candle for this symbol
          2. Calculate HA (read-only, no storage)
          3. Check for flip against last confirmed HA
          4. Option A: only act on first flip per 4H window
        """
        kline_data = data.get("data", [])
        if not kline_data:
            return

        for kline in kline_data:
            # Only trigger on confirmed 5M candle close
            if not kline.get("confirm", False):
                continue

            symbol = kline.get("symbol", "")
            if not symbol:
                parts = topic.split(".")
                if len(parts) >= 3:
                    symbol = parts[2]

            # Read the cached live 4H candle
            live_4h = self._live_4h_candle.get(symbol)
            if live_4h is None:
                continue

            # Calculate HA without modifying stored series
            live_ha, signal = self.ha.calc_live(symbol, live_4h)

            if signal is None:
                continue

            # Option A: Only act on FIRST flip per 4H window
            candle_start = live_4h.timestamp
            if self._flip_acted_this_window.get(symbol) == candle_start:
                continue

            # New flip detected at this 5M candle close!
            self._flip_acted_this_window[symbol] = candle_start
            logger.info(
                f"[SIGNAL] {symbol}: Flip detected on 5M close! "
                f"{'LONG' if signal.side == Side.LONG else 'SHORT'}"
            )
            await self._process_signal(signal)

    async def on_kline_15(self, topic: str, data: Dict[str, Any]):
        """Handle 15M kline WebSocket message — update ATR."""
        kline_data = data.get("data", [])
        if not kline_data:
            return

        for kline in kline_data:
            if not kline.get("confirm", False):
                continue

            symbol = kline.get("symbol", "")
            if not symbol:
                parts = topic.split(".")
                if len(parts) >= 3:
                    symbol = parts[2]

            candle = Candle(
                timestamp=int(kline.get("start", 0)),
                open=Decimal(str(kline.get("open", 0))),
                high=Decimal(str(kline.get("high", 0))),
                low=Decimal(str(kline.get("low", 0))),
                close=Decimal(str(kline.get("close", 0))),
                volume=Decimal(str(kline.get("volume", 0))),
                confirmed=True,
            )

            self.atr.update(symbol, candle)

    async def on_ticker(self, topic: str, data: Dict[str, Any]):
        """Handle ticker update — pass to risk manager for TP monitoring."""
        tick_data = data.get("data", {})
        if not tick_data:
            return

        symbol = tick_data.get("symbol", "")
        mark_price_str = tick_data.get("markPrice") or tick_data.get("lastPrice")
        if not symbol or not mark_price_str:
            return

        mark_price = Decimal(str(mark_price_str))
        await self.risk.check_price(symbol, mark_price)

    async def on_position_update(self, topic: str, data: Dict[str, Any]):
        """Handle position WebSocket updates (position closed by SL, etc.)."""
        positions = data.get("data", [])

        for pos in positions:
            symbol = pos.get("symbol", "")
            size = Decimal(pos.get("size", "0"))

            # Position closed
            if size == 0:
                trade = self.risk.get_active_trade_by_symbol(symbol)
                if trade and trade.status == TradeStatus.OPEN:
                    pnl = Decimal(pos.get("cumRealisedPnl", "0"))
                    # Determine exit reason
                    from exchange.models import ExitReason
                    exit_reason = ExitReason.TRAILING_SL if trade.highest_tp_reached >= 2 else ExitReason.SL_HIT

                    await self._handle_trade_closed(trade, exit_reason, pnl)

    async def on_execution(self, topic: str, data: Dict[str, Any]):
        """Handle execution (fill) notifications."""
        executions = data.get("data", [])

        for exec_data in executions:
            order_id = exec_data.get("orderId", "")
            exec_type = exec_data.get("execType", "")
            exec_fee = Decimal(exec_data.get("execFee", "0"))

            # Look up trade by order ID
            trade = self.db.get_trade_by_order_id(order_id)
            if trade:
                trade.fees += abs(exec_fee)
                self.db.update_trade(trade)

    # ==================== Signal Processing ====================

    async def _process_signal(self, signal: Signal):
        """Process a flip signal and attempt to execute a trade."""
        async with self._signal_lock:
            symbol = signal.symbol
            side = signal.side

            direction = "LONG" if side == Side.LONG else "SHORT"
            logger.info(f"[SIGNAL] Processing {direction} signal for {symbol}")

            # Check 1: Is this asset in cooldown?
            if self._is_in_cooldown(symbol):
                logger.info(f"[SIGNAL] {symbol}: In cooldown. Ignoring signal.")
                return

            # Check 2: Is this asset already in an active trade?
            if self.coins.is_in_trade(symbol):
                logger.info(f"[SIGNAL] {symbol}: Already in active trade. Ignoring signal.")
                return

            # Check 3: Is there an available slot?
            slot = self.slots.get_available_slot()
            if slot is None:
                logger.info(f"[SIGNAL] {symbol}: No available slots. Ignoring signal.")
                return

            # Get coin info
            coin = self.coins.get_coin(symbol)
            if coin is None:
                logger.warning(f"[SIGNAL] {symbol}: Not in tracked coins. Ignoring.")
                return

            # Calculate position size
            position_size = self.slots.calculate_position_size(slot)

            # ═══ DRY RUN MODE ═══
            if self.config.execution.dry_run:
                logger.info(
                    f"[DRY RUN] 🔔 WOULD EXECUTE: {direction} {symbol} on Slot #{slot.id}. "
                    f"Size: ${position_size:.2f} (${slot.balance:.2f} × {self.config.slots.leverage}x)"
                )
                atr = self.atr.get_atr(symbol)
                if atr:
                    logger.info(f"[DRY RUN] ATR(14) = {atr:.6f} → TP spacing = {atr}")
                await self.notifier.send(
                    f"📋 <b>DRY RUN SIGNAL</b>\n\n"
                    f"{'🟢 LONG' if side == Side.LONG else '🔴 SHORT'} <code>{symbol}</code>\n"
                    f"Slot #{slot.id} (${slot.balance:.2f})\n"
                    f"Size: ${position_size:.2f}\n"
                    f"ATR: {atr or 'N/A'}"
                )
                return

            # ═══ LIVE TRADING ═══
            logger.info(
                f"[SIGNAL] {symbol}: Executing {direction} on Slot #{slot.id}. "
                f"Size: ${position_size:.2f} (${slot.balance:.2f} × {self.config.slots.leverage}x)"
            )

            # Create trade record
            trade = Trade(
                slot_id=slot.id,
                symbol=symbol,
                side=side,
                status=TradeStatus.PENDING,
            )
            trade_id = self.db.create_trade(trade)
            trade.id = trade_id

            # Assign slot
            self.slots.assign_slot(slot, trade)
            self.coins.set_in_trade(symbol, True)

            # Set leverage for this symbol
            try:
                await self.client.set_leverage(symbol, self.config.slots.leverage)
            except Exception as e:
                # May fail if already set — that's OK
                logger.debug(f"[SIGNAL] {symbol}: Set leverage result: {e}")

            # Execute entry
            filled = await self.executor.execute_entry(trade, coin, position_size)

            if not filled:
                # Fill failed — release slot
                logger.warning(f"[SIGNAL] {symbol}: Fill failed. Releasing slot #{slot.id}")
                self.slots.release_slot(slot)
                self.coins.set_in_trade(symbol, False)
                trade.status = TradeStatus.CANCELLED
                from exchange.models import ExitReason
                trade.exit_reason = ExitReason.FILL_FAILED
                self.db.update_trade(trade)
                return

            # Fill successful — set up risk management
            self.slots.mark_in_trade(slot)
            self.db.update_trade(trade)

            sl_set = await self.risk.setup_trade_risk(trade, coin)
            if not sl_set:
                logger.error(
                    f"[SIGNAL] {symbol}: CRITICAL — Failed to set SL! "
                    f"Closing position immediately."
                )
                await self.executor.close_position_market(trade)
                self.slots.release_slot(slot)
                self.coins.set_in_trade(symbol, False)
                return

            # Send notification
            await self.notifier.send_trade_entry(
                symbol=symbol,
                side=side.value,
                entry_price=str(trade.entry_price),
                qty=str(trade.qty),
                sl_price=str(trade.current_sl_price),
                slot_id=slot.id,
                slot_balance=f"{slot.balance:.2f}",
            )

            logger.info(
                f"[SIGNAL] {symbol}: Trade #{trade.id} fully set up. "
                f"Entry={trade.entry_price}, SL={trade.current_sl_price}"
            )

    async def _handle_trade_closed(self, trade: Trade, exit_reason, pnl: Decimal):
        """Handle a trade being closed."""
        from exchange.models import ExitReason
        symbol = trade.symbol

        # Calculate actual P&L from position
        self.risk.handle_trade_closed(trade, exit_reason, pnl, trade.fees)

        # Update slot
        slot = self.slots.get_slot(trade.slot_id)
        if slot:
            cooldown_minutes = self.config.execution.cooldown_minutes
            self.slots.complete_trade(slot, trade, cooldown_minutes)

            # Start cooldown timer for this asset
            self._set_cooldown(symbol, cooldown_minutes)

            # Schedule slot release from cooldown
            asyncio.create_task(
                self._cooldown_timer(slot.id, cooldown_minutes)
            )

            # Send notification
            await self.notifier.send_trade_exit(
                symbol=symbol,
                side=trade.side.value,
                pnl=f"{(trade.pnl or Decimal('0')):+.4f}",
                exit_reason=exit_reason.value,
                slot_id=slot.id,
                new_balance=f"{slot.balance:.2f}",
                highest_tp=trade.highest_tp_reached,
            )

        self.coins.set_in_trade(symbol, False)

    # ==================== Cooldown Management ====================

    def _is_in_cooldown(self, symbol: str) -> bool:
        until = self._cooldowns.get(symbol)
        if until and datetime.utcnow() < until:
            return True
        return False

    def _set_cooldown(self, symbol: str, minutes: int):
        self._cooldowns[symbol] = datetime.utcnow() + timedelta(minutes=minutes)

    async def _cooldown_timer(self, slot_id: int, minutes: int):
        """Wait for cooldown period then release the slot."""
        await asyncio.sleep(minutes * 60)
        slot = self.slots.get_slot(slot_id)
        if slot:
            from exchange.models import SlotState
            if slot.state == SlotState.COOLDOWN:
                self.slots.release_from_cooldown(slot)
