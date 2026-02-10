"""
Heiken Ashi Calculator — Proper calculation on completed candles only.
No lookahead, no incomplete candle reads.
"""

from __future__ import annotations
from decimal import Decimal
from typing import Optional, List, Tuple
from exchange.models import Candle, HACandle, Signal, Side
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class HeikenAshiEngine:
    """
    Calculates Heiken Ashi candles from standard OHLCV candles.
    Maintains state per symbol for incremental updates.
    """

    def __init__(self):
        # symbol -> list of HA candles (most recent last)
        self._ha_series: dict[str, List[HACandle]] = {}
        # symbol -> previous HA candle (for incremental calc)
        self._prev_ha: dict[str, HACandle] = {}

    def build_from_history(self, symbol: str, candles: List[Candle]) -> List[HACandle]:
        """
        Build full HA series from historical candles.
        Called on startup with 50 candles per symbol.
        Candles must be sorted oldest-first.
        """
        if not candles:
            return []

        ha_series: List[HACandle] = []
        prev_ha: Optional[HACandle] = None

        for candle in candles:
            ha = self._calc_single(candle, prev_ha)
            ha_series.append(ha)
            prev_ha = ha

        self._ha_series[symbol] = ha_series
        self._prev_ha[symbol] = ha_series[-1] if ha_series else None

        logger.info(
            f"[HA] {symbol}: Built {len(ha_series)} HA candles. "
            f"Latest: {'BULL' if ha_series[-1].is_bullish else 'BEAR'}"
        )
        return ha_series

    def update(self, symbol: str, candle: Candle) -> Tuple[HACandle, Optional[Signal]]:
        """
        Process a new confirmed 4H candle.
        Returns the new HA candle and a Signal if a flip occurred.
        """
        prev_ha = self._prev_ha.get(symbol)
        new_ha = self._calc_single(candle, prev_ha)

        # Append to series (keep last 50)
        if symbol not in self._ha_series:
            self._ha_series[symbol] = []
        self._ha_series[symbol].append(new_ha)
        if len(self._ha_series[symbol]) > 50:
            self._ha_series[symbol] = self._ha_series[symbol][-50:]

        # Check for flip
        signal = None
        if prev_ha is not None:
            signal = self._detect_flip(symbol, prev_ha, new_ha)

        self._prev_ha[symbol] = new_ha
        return new_ha, signal

    def get_latest(self, symbol: str) -> Optional[HACandle]:
        """Get the most recent HA candle for a symbol."""
        return self._prev_ha.get(symbol)

    def get_previous(self, symbol: str) -> Optional[HACandle]:
        """Get the second-to-last HA candle for a symbol."""
        series = self._ha_series.get(symbol, [])
        if len(series) >= 2:
            return series[-2]
        return None

    def _calc_single(self, candle: Candle, prev_ha: Optional[HACandle]) -> HACandle:
        """
        Calculate a single HA candle from a standard candle.

        HA_Close = (O + H + L + C) / 4
        HA_Open  = (prev_HA_Open + prev_HA_Close) / 2   [first: (O + C) / 2]
        HA_High  = max(H, HA_Open, HA_Close)
        HA_Low   = min(L, HA_Open, HA_Close)
        """
        four = Decimal("4")
        two = Decimal("2")

        ha_close = (candle.open + candle.high + candle.low + candle.close) / four

        if prev_ha is None:
            ha_open = (candle.open + candle.close) / two
        else:
            ha_open = (prev_ha.ha_open + prev_ha.ha_close) / two

        ha_high = max(candle.high, ha_open, ha_close)
        ha_low = min(candle.low, ha_open, ha_close)

        return HACandle(
            timestamp=candle.timestamp,
            ha_open=ha_open,
            ha_close=ha_close,
            ha_high=ha_high,
            ha_low=ha_low,
        )

    def _detect_flip(self, symbol: str, prev_ha: HACandle, curr_ha: HACandle) -> Optional[Signal]:
        """
        Detect HA flip between two consecutive completed candles.

        Bullish flip: prev was bearish, current is bullish → LONG
        Bearish flip: prev was bullish, current is bearish → SHORT
        """
        if prev_ha.is_bearish and curr_ha.is_bullish:
            logger.info(f"[HA] {symbol}: BULLISH FLIP detected (LONG)")
            return Signal(
                symbol=symbol,
                side=Side.LONG,
                timestamp=datetime.utcnow(),
                ha_candle=curr_ha,
            )
        elif prev_ha.is_bullish and curr_ha.is_bearish:
            logger.info(f"[HA] {symbol}: BEARISH FLIP detected (SHORT)")
            return Signal(
                symbol=symbol,
                side=Side.SHORT,
                timestamp=datetime.utcnow(),
                ha_candle=curr_ha,
            )
        return None

    def remove_symbol(self, symbol: str):
        """Remove a symbol (e.g., when it drops out of top 20)."""
        self._ha_series.pop(symbol, None)
        self._prev_ha.pop(symbol, None)
