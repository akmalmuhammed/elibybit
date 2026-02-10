"""
Risk Manager — Handles SL placement, TP monitoring, and trailing SL ladder.

SL Logic:
  - Initial SL = entry ± 2.5%
  - TP1 hit → SL stays at initial
  - TP2 hit → SL moves to TP1
  - TP(n) hit → SL moves to TP(n-1)

INVARIANT: SL can only move in the profitable direction, NEVER regress.
"""

from __future__ import annotations
import asyncio
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from datetime import datetime
from typing import Optional, Dict, List, TYPE_CHECKING
from exchange.models import Trade, TradeStatus, ExitReason, Side, TPLevel, CoinInfo
from storage.database import Database
import logging

if TYPE_CHECKING:
    from exchange.bybit_rest import BybitRestClient
    from config import StrategyConfig
    from core.atr import ATRCalculator

logger = logging.getLogger(__name__)


class RiskManager:
    """
    Manages stop losses and take profit levels for all active trades.
    Monitors price in real-time and adjusts SL as TP levels are hit.
    """

    def __init__(
        self,
        client: "BybitRestClient",
        config: "StrategyConfig",
        atr_calc: "ATRCalculator",
        db: Database,
    ):
        self.client = client
        self.config = config
        self.atr_calc = atr_calc
        self.db = db

        # trade_id -> Trade (in-memory cache for fast price checks)
        self._active_trades: Dict[int, Trade] = {}

    def load_active_trades(self):
        """Load all open trades from DB into memory on startup."""
        trades = self.db.get_open_trades()
        for trade in trades:
            if trade.id and trade.status == TradeStatus.OPEN:
                self._active_trades[trade.id] = trade
        logger.info(f"[RISK] Loaded {len(self._active_trades)} active trades")

    async def setup_trade_risk(
        self,
        trade: Trade,
        coin: CoinInfo,
    ) -> bool:
        """
        Set up initial SL and calculate TP levels for a new trade.
        Called immediately after order fill.
        Returns True if SL was successfully placed.
        """
        if trade.entry_price is None:
            logger.error(f"[RISK] Trade #{trade.id}: No entry price")
            return False

        entry = trade.entry_price
        sl_pct = self.config.initial_sl_pct

        # Calculate initial SL
        if trade.side == Side.LONG:
            sl_price = entry * (Decimal("1") - sl_pct)
        else:
            sl_price = entry * (Decimal("1") + sl_pct)

        # Round SL to tick size
        sl_price = self._round_sl_price(sl_price, coin.tick_size, trade.side)

        # Calculate TP levels from ATR on 15M
        atr = self.atr_calc.get_atr(trade.symbol)
        if atr is None or atr <= 0:
            logger.warning(f"[RISK] {trade.symbol}: No ATR available, using entry * 1% as fallback")
            atr = entry * Decimal("0.01")

        tp_levels = []
        for n in range(1, self.config.tp_levels + 1):
            if trade.side == Side.LONG:
                tp_price = entry + (Decimal(str(n)) * atr)
            else:
                tp_price = entry - (Decimal(str(n)) * atr)

            # Round TP to tick size
            tp_price = self._round_tp_price(tp_price, coin.tick_size, trade.side)

            tp_levels.append(TPLevel(
                level=n,
                price=tp_price,
                hit=False,
            ))

        # Set SL on the exchange using set-trading-stop
        result = await self.client.set_trading_stop(
            symbol=trade.symbol,
            stop_loss=str(sl_price),
        )

        if result.get("retCode") != 0:
            logger.error(
                f"[RISK] {trade.symbol}: Failed to set SL: {result.get('retMsg')}"
            )
            return False

        # Update trade
        trade.initial_sl_price = sl_price
        trade.current_sl_price = sl_price
        trade.tp_levels = tp_levels
        trade.atr_value = atr
        trade.highest_tp_reached = 0
        self.db.update_trade(trade)

        # Add to active monitoring
        self._active_trades[trade.id] = trade

        logger.info(
            f"[RISK] {trade.symbol}: SL set @ {sl_price} (-{sl_pct*100}%). "
            f"ATR={atr:.6f}, TP1={tp_levels[0].price}, TP10={tp_levels[-1].price}"
        )
        return True

    async def check_price(self, symbol: str, mark_price: Decimal):
        """
        Called on every ticker update.
        Checks if any TP level was hit for active trades on this symbol.
        If so, trails the SL accordingly.
        """
        for trade_id, trade in list(self._active_trades.items()):
            if trade.symbol != symbol:
                continue
            if trade.status != TradeStatus.OPEN:
                continue
            if not trade.tp_levels:
                continue

            await self._check_tp_levels(trade, mark_price)

    async def _check_tp_levels(self, trade: Trade, price: Decimal):
        """Check and update TP levels for a trade."""
        new_highest = trade.highest_tp_reached

        for tp in trade.tp_levels:
            if tp.hit:
                continue

            hit = False
            if trade.side == Side.LONG and price >= tp.price:
                hit = True
            elif trade.side == Side.SHORT and price <= tp.price:
                hit = True

            if hit:
                tp.hit = True
                tp.hit_time = datetime.utcnow()
                new_highest = max(new_highest, tp.level)
                logger.info(
                    f"[RISK] {trade.symbol}: TP{tp.level} HIT @ {price} "
                    f"(target was {tp.price})"
                )

        # Trail SL if we reached a new TP level
        if new_highest > trade.highest_tp_reached:
            old_highest = trade.highest_tp_reached
            trade.highest_tp_reached = new_highest

            # Trailing SL logic:
            # TP1 hit → SL stays at initial
            # TP2 hit → SL moves to TP1
            # TP(n) hit → SL moves to TP(n-1)
            if new_highest >= 2:
                target_sl_level = new_highest - 1
                new_sl = self._get_tp_price(trade, target_sl_level)

                if new_sl is not None:
                    await self._update_sl(trade, new_sl)

            self.db.update_trade(trade)

    async def _update_sl(self, trade: Trade, new_sl_price: Decimal):
        """
        Update SL on the exchange.
        INVARIANT: SL can only move in the profitable direction.
        """
        current_sl = trade.current_sl_price

        # Safety check: never regress SL
        if current_sl is not None:
            if trade.side == Side.LONG and new_sl_price <= current_sl:
                logger.warning(
                    f"[RISK] {trade.symbol}: SL regression blocked! "
                    f"Current={current_sl}, Attempted={new_sl_price}"
                )
                return
            elif trade.side == Side.SHORT and new_sl_price >= current_sl:
                logger.warning(
                    f"[RISK] {trade.symbol}: SL regression blocked! "
                    f"Current={current_sl}, Attempted={new_sl_price}"
                )
                return

        # Update on exchange
        result = await self.client.set_trading_stop(
            symbol=trade.symbol,
            stop_loss=str(new_sl_price),
        )

        if result.get("retCode") == 0:
            trade.current_sl_price = new_sl_price
            self.db.update_trade(trade)
            logger.info(
                f"[RISK] {trade.symbol}: SL TRAILED to {new_sl_price} "
                f"(TP{trade.highest_tp_reached} reached)"
            )
        else:
            logger.error(
                f"[RISK] {trade.symbol}: Failed to update SL: {result.get('retMsg')}"
            )

    def handle_trade_closed(self, trade: Trade, exit_reason: ExitReason, pnl: Decimal, fees: Decimal):
        """Handle a trade being closed (SL hit, etc.)."""
        trade.status = TradeStatus.CLOSED
        trade.exit_time = datetime.utcnow()
        trade.exit_reason = exit_reason
        trade.pnl = pnl
        trade.fees = fees
        self.db.update_trade(trade)

        # Remove from active monitoring
        self._active_trades.pop(trade.id, None)

        logger.info(
            f"[RISK] {trade.symbol}: Trade closed. Reason={exit_reason.value}, "
            f"PnL=${pnl:+.4f}, Fees=${fees:.4f}"
        )

    def get_active_trade(self, trade_id: int) -> Optional[Trade]:
        return self._active_trades.get(trade_id)

    def get_active_trade_by_symbol(self, symbol: str) -> Optional[Trade]:
        for trade in self._active_trades.values():
            if trade.symbol == symbol and trade.status == TradeStatus.OPEN:
                return trade
        return None

    def get_all_active_trades(self) -> List[Trade]:
        return list(self._active_trades.values())

    def remove_trade(self, trade_id: int):
        self._active_trades.pop(trade_id, None)

    def _get_tp_price(self, trade: Trade, level: int) -> Optional[Decimal]:
        """Get TP price by level number."""
        for tp in trade.tp_levels:
            if tp.level == level:
                return tp.price
        return None

    def _round_sl_price(self, price: Decimal, tick_size: Decimal, side: Side) -> Decimal:
        """Round SL price conservatively (toward position, not away)."""
        if tick_size <= 0:
            return price
        # For longs: SL below entry → round UP (less aggressive SL)
        # For shorts: SL above entry → round DOWN (less aggressive SL)
        if side == Side.LONG:
            return (price / tick_size).to_integral_value(rounding=ROUND_UP) * tick_size
        else:
            return (price / tick_size).to_integral_value(rounding=ROUND_DOWN) * tick_size

    def _round_tp_price(self, price: Decimal, tick_size: Decimal, side: Side) -> Decimal:
        """Round TP price conservatively."""
        if tick_size <= 0:
            return price
        # For longs: TP above entry → round DOWN (easier to hit)
        # For shorts: TP below entry → round UP (easier to hit)
        if side == Side.LONG:
            return (price / tick_size).to_integral_value(rounding=ROUND_DOWN) * tick_size
        else:
            return (price / tick_size).to_integral_value(rounding=ROUND_UP) * tick_size
