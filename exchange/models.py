"""
Data models for the HA Flip Bot.
Uses Decimal for all monetary/price calculations — no floating point errors.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Optional, List
from datetime import datetime


class Side(Enum):
    LONG = "Buy"
    SHORT = "Sell"


class SlotState(Enum):
    AVAILABLE = "AVAILABLE"
    ASSIGNED = "ASSIGNED"
    IN_TRADE = "IN_TRADE"
    COOLDOWN = "COOLDOWN"
    FROZEN = "FROZEN"


class TradeStatus(Enum):
    PENDING = "PENDING"
    FILLING = "FILLING"
    OPEN = "OPEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class ExitReason(Enum):
    SL_HIT = "SL_HIT"
    TRAILING_SL = "TRAILING_SL"
    KILL_SWITCH = "KILL_SWITCH"
    MANUAL = "MANUAL"
    FILL_FAILED = "FILL_FAILED"


@dataclass
class Candle:
    """Standard OHLCV candle."""
    timestamp: int          # Unix ms
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    confirmed: bool = True


@dataclass
class HACandle:
    """Heiken Ashi candle."""
    timestamp: int
    ha_open: Decimal
    ha_close: Decimal
    ha_high: Decimal
    ha_low: Decimal

    @property
    def is_bullish(self) -> bool:
        return self.ha_close > self.ha_open

    @property
    def is_bearish(self) -> bool:
        return self.ha_close < self.ha_open


@dataclass
class Signal:
    """Trading signal from HA flip detection."""
    symbol: str
    side: Side
    timestamp: datetime
    ha_candle: HACandle


@dataclass
class TPLevel:
    """Single take profit level."""
    level: int              # 1-10
    price: Decimal
    hit: bool = False
    hit_time: Optional[datetime] = None


@dataclass
class Trade:
    """Represents a single trade with full lifecycle."""
    id: Optional[int] = None
    slot_id: int = 0
    symbol: str = ""
    side: Side = Side.LONG
    entry_price: Optional[Decimal] = None
    qty: Optional[Decimal] = None
    order_id: Optional[str] = None
    sl_order_id: Optional[str] = None
    current_sl_price: Optional[Decimal] = None
    initial_sl_price: Optional[Decimal] = None
    tp_levels: List[TPLevel] = field(default_factory=list)
    highest_tp_reached: int = 0
    atr_value: Optional[Decimal] = None
    status: TradeStatus = TradeStatus.PENDING
    pnl: Optional[Decimal] = None
    fees: Decimal = Decimal("0")
    entry_time: Optional[datetime] = None
    exit_time: Optional[datetime] = None
    exit_reason: Optional[ExitReason] = None
    cooldown_until: Optional[datetime] = None
    fill_attempts: int = 0
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Slot:
    """Trading slot with independent balance."""
    id: int
    balance: Decimal
    state: SlotState = SlotState.AVAILABLE
    current_symbol: Optional[str] = None
    current_trade_id: Optional[int] = None
    total_trades: int = 0
    total_pnl: Decimal = Decimal("0")
    updated_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class CoinInfo:
    """Tracked coin metadata."""
    symbol: str
    base_coin: str          # e.g., "BTC"
    volume_24h: Decimal
    min_qty: Decimal        # Minimum order quantity
    qty_step: Decimal       # Quantity precision step
    tick_size: Decimal      # Price precision step
    last_ha: Optional[HACandle] = None
    prev_ha: Optional[HACandle] = None
    cooldown_until: Optional[datetime] = None
    in_active_trade: bool = False


@dataclass
class OrderBookSnap:
    """Top of book snapshot."""
    symbol: str
    best_bid: Decimal
    best_ask: Decimal
    timestamp: int
