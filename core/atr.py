"""
ATR Calculator — Average True Range on 15-minute candles.
Used to calculate TP levels for each trade.
"""

from __future__ import annotations
from decimal import Decimal
from typing import List, Optional
from exchange.models import Candle
import logging

logger = logging.getLogger(__name__)


class ATRCalculator:
    """
    Calculates ATR(14) on 15-minute candles.
    Maintains a rolling buffer per symbol.
    """

    def __init__(self, period: int = 14):
        self.period = period
        # symbol -> list of recent 15M candles (for rolling ATR)
        self._candle_buffer: dict[str, List[Candle]] = {}
        # symbol -> latest ATR value
        self._atr_values: dict[str, Decimal] = {}

    def initialize(self, symbol: str, candles: List[Candle]):
        """
        Initialize ATR buffer with historical 15M candles.
        Need at least period+1 candles to calculate ATR.
        Candles must be sorted oldest-first.
        """
        self._candle_buffer[symbol] = candles[-(self.period + 10):]  # Keep some extra
        self._recalculate(symbol)

    def update(self, symbol: str, candle: Candle):
        """Process a new confirmed 15M candle."""
        if symbol not in self._candle_buffer:
            self._candle_buffer[symbol] = []

        self._candle_buffer[symbol].append(candle)

        # Keep buffer manageable
        max_buffer = self.period + 20
        if len(self._candle_buffer[symbol]) > max_buffer:
            self._candle_buffer[symbol] = self._candle_buffer[symbol][-max_buffer:]

        self._recalculate(symbol)

    def get_atr(self, symbol: str) -> Optional[Decimal]:
        """Get current ATR value for a symbol."""
        return self._atr_values.get(symbol)

    def calculate_tp_levels(
        self,
        symbol: str,
        entry_price: Decimal,
        side: str,
        num_levels: int = 10,
    ) -> List[Decimal]:
        """
        Calculate TP levels based on ATR.

        LONG:  TP_n = entry + (n × ATR)
        SHORT: TP_n = entry - (n × ATR)
        """
        atr = self.get_atr(symbol)
        if atr is None or atr == 0:
            logger.warning(f"[ATR] {symbol}: No ATR available, cannot calculate TPs")
            return []

        levels = []
        for n in range(1, num_levels + 1):
            if side == "Buy":  # LONG
                tp = entry_price + (Decimal(str(n)) * atr)
            else:  # SHORT
                tp = entry_price - (Decimal(str(n)) * atr)
            levels.append(tp)

        logger.info(
            f"[ATR] {symbol}: ATR={atr:.6f}, Entry={entry_price}, "
            f"TP1={levels[0]:.6f}, TP10={levels[-1]:.6f}"
        )
        return levels

    def _recalculate(self, symbol: str):
        """Recalculate ATR using SMA method."""
        candles = self._candle_buffer.get(symbol, [])
        if len(candles) < self.period + 1:
            return

        # Calculate True Range for each candle (need previous close)
        true_ranges: List[Decimal] = []
        for i in range(1, len(candles)):
            curr = candles[i]
            prev_close = candles[i - 1].close

            tr = max(
                curr.high - curr.low,
                abs(curr.high - prev_close),
                abs(curr.low - prev_close),
            )
            true_ranges.append(tr)

        # SMA of last `period` TRs
        if len(true_ranges) < self.period:
            return

        recent_trs = true_ranges[-self.period:]
        atr = sum(recent_trs) / Decimal(str(self.period))
        self._atr_values[symbol] = atr

    def remove_symbol(self, symbol: str):
        """Remove a symbol from tracking."""
        self._candle_buffer.pop(symbol, None)
        self._atr_values.pop(symbol, None)
