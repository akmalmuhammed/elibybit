"""
Coin Selector — Dynamically selects top N coins by 24H volume.
Excludes stablecoins and low-liquidity pairs.
"""

from __future__ import annotations
from decimal import Decimal
from typing import List, Dict, Optional, TYPE_CHECKING
from exchange.models import CoinInfo
import logging

if TYPE_CHECKING:
    from exchange.bybit_rest import BybitRestClient

logger = logging.getLogger(__name__)


class CoinSelector:
    """Selects and maintains the top N coins by volume."""

    def __init__(
        self,
        num_coins: int = 20,
        excluded_stablecoins: Optional[List[str]] = None,
    ):
        self.num_coins = num_coins
        self.excluded = set(excluded_stablecoins or [])
        self._coins: Dict[str, CoinInfo] = {}

    @property
    def symbols(self) -> List[str]:
        return list(self._coins.keys())

    @property
    def coins(self) -> Dict[str, CoinInfo]:
        return self._coins

    def get_coin(self, symbol: str) -> Optional[CoinInfo]:
        return self._coins.get(symbol)

    async def refresh(self, client: "BybitRestClient") -> tuple[List[str], List[str]]:
        """
        Refresh the coin list from Bybit.
        Returns (added_symbols, removed_symbols) for the caller to update subscriptions.
        """
        old_symbols = set(self._coins.keys())

        # Fetch all USDT perpetual tickers
        tickers = await client.get_tickers()
        instruments = await client.get_instruments_info()

        # Build instrument lookup
        inst_map = {}
        for inst in instruments:
            symbol = inst["symbol"]
            inst_map[symbol] = inst

        # Filter and sort
        candidates = []
        for ticker in tickers:
            symbol = ticker["symbol"]

            # Must end with USDT
            if not symbol.endswith("USDT"):
                continue

            # Exclude stablecoins
            base = symbol.replace("USDT", "")
            if base in self.excluded or symbol in self.excluded:
                continue

            # Must have instrument info
            if symbol not in inst_map:
                continue

            volume_24h = Decimal(ticker.get("turnover24h", "0"))
            candidates.append((symbol, base, volume_24h, inst_map[symbol]))

        # Sort by 24H volume descending
        candidates.sort(key=lambda x: x[2], reverse=True)

        # Take top N
        new_coins: Dict[str, CoinInfo] = {}
        for symbol, base, volume, inst in candidates[:self.num_coins]:
            lot_filter = inst.get("lotSizeFilter", {})
            price_filter = inst.get("priceFilter", {})

            coin = CoinInfo(
                symbol=symbol,
                base_coin=base,
                volume_24h=volume,
                min_qty=Decimal(lot_filter.get("minOrderQty", "0.001")),
                qty_step=Decimal(lot_filter.get("qtyStep", "0.001")),
                tick_size=Decimal(price_filter.get("tickSize", "0.01")),
            )

            # Preserve existing state if coin was already tracked
            if symbol in self._coins:
                old = self._coins[symbol]
                coin.last_ha = old.last_ha
                coin.prev_ha = old.prev_ha
                coin.cooldown_until = old.cooldown_until
                coin.in_active_trade = old.in_active_trade

            new_coins[symbol] = coin

        self._coins = new_coins
        new_symbols = set(new_coins.keys())

        added = list(new_symbols - old_symbols)
        removed = list(old_symbols - new_symbols)

        if added or removed:
            logger.info(
                f"[COINS] Refreshed: {len(new_coins)} coins. "
                f"Added: {added}, Removed: {removed}"
            )

        logger.info(
            f"[COINS] Top {self.num_coins}: "
            f"{[c.symbol for c in sorted(new_coins.values(), key=lambda x: x.volume_24h, reverse=True)[:5]]}..."
        )

        return added, removed

    def set_in_trade(self, symbol: str, in_trade: bool):
        """Mark a coin as having an active trade."""
        if symbol in self._coins:
            self._coins[symbol].in_active_trade = in_trade

    def is_in_trade(self, symbol: str) -> bool:
        """Check if a coin has an active trade."""
        coin = self._coins.get(symbol)
        return coin.in_active_trade if coin else False
