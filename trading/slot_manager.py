"""
Slot Manager — Manages the 8 independent trading slots.
Handles slot lifecycle, assignment, compounding, and freezing.
"""

from __future__ import annotations
from decimal import Decimal
from datetime import datetime, timedelta
from typing import Optional, List, TYPE_CHECKING
from exchange.models import Slot, SlotState, Trade, TradeStatus, Signal, ExitReason
from storage.database import Database
import logging

if TYPE_CHECKING:
    from config import SlotConfig

logger = logging.getLogger(__name__)


class SlotManager:
    """
    Manages 8 independent trading slots.
    Each slot has its own balance and compounds independently.
    """

    def __init__(self, config: "SlotConfig", db: Database):
        self.config = config
        self.db = db
        self._slots: dict[int, Slot] = {}

    def initialize(self):
        """Load or create slots on startup."""
        self.db.initialize_slots(self.config.num_slots, self.config.initial_balance)
        self._reload_slots()

        # Log slot statuses
        for slot in self._slots.values():
            logger.info(
                f"[SLOT {slot.id}] Balance: ${slot.balance:.2f}, "
                f"State: {slot.state.value}, "
                f"Trades: {slot.total_trades}, PnL: ${slot.total_pnl:.2f}"
            )

    def _reload_slots(self):
        """Reload all slots from DB."""
        for slot in self.db.get_all_slots():
            self._slots[slot.id] = slot

    def get_slot(self, slot_id: int) -> Optional[Slot]:
        return self._slots.get(slot_id)

    def get_all_slots(self) -> List[Slot]:
        return list(self._slots.values())

    def get_available_slot(self) -> Optional[Slot]:
        """Find first available slot for a new trade."""
        for slot in sorted(self._slots.values(), key=lambda s: s.id):
            if slot.state == SlotState.AVAILABLE:
                return slot
        return None

    def count_available(self) -> int:
        return sum(1 for s in self._slots.values() if s.state == SlotState.AVAILABLE)

    def count_in_trade(self) -> int:
        return sum(1 for s in self._slots.values() if s.state == SlotState.IN_TRADE)

    def assign_slot(self, slot: Slot, trade: Trade) -> bool:
        """
        Assign a slot to a trade.
        Returns False if slot is not available.
        """
        if slot.state != SlotState.AVAILABLE:
            logger.warning(f"[SLOT {slot.id}] Cannot assign — state is {slot.state.value}")
            return False

        slot.state = SlotState.ASSIGNED
        slot.current_symbol = trade.symbol
        slot.current_trade_id = trade.id
        self.db.update_slot(slot)
        self._slots[slot.id] = slot

        logger.info(f"[SLOT {slot.id}] Assigned to {trade.symbol} (Trade #{trade.id})")
        return True

    def mark_in_trade(self, slot: Slot):
        """Mark slot as actively in a trade (order filled)."""
        slot.state = SlotState.IN_TRADE
        self.db.update_slot(slot)
        self._slots[slot.id] = slot

    def complete_trade(self, slot: Slot, trade: Trade, cooldown_minutes: int = 30):
        """
        Complete a trade — update slot balance with P&L, start cooldown.
        """
        # Calculate new balance
        pnl = trade.pnl or Decimal("0")
        fees = trade.fees or Decimal("0")
        net_pnl = pnl - fees

        old_balance = slot.balance
        new_balance = old_balance + net_pnl

        slot.balance = new_balance
        slot.total_trades += 1
        slot.total_pnl += net_pnl
        slot.current_symbol = None
        slot.current_trade_id = None

        # Check if slot should be frozen
        if new_balance < self.config.min_balance:
            slot.state = SlotState.FROZEN
            logger.warning(
                f"[SLOT {slot.id}] FROZEN — Balance ${new_balance:.2f} "
                f"< min ${self.config.min_balance:.2f}"
            )
        else:
            # Start cooldown
            slot.state = SlotState.COOLDOWN
            logger.info(
                f"[SLOT {slot.id}] Trade complete. "
                f"PnL: ${net_pnl:+.2f} (${old_balance:.2f} → ${new_balance:.2f}). "
                f"Cooldown: {cooldown_minutes}min"
            )

        self.db.update_slot(slot)
        self._slots[slot.id] = slot

    def release_from_cooldown(self, slot: Slot):
        """Release a slot from cooldown to available."""
        if slot.state == SlotState.COOLDOWN:
            slot.state = SlotState.AVAILABLE
            self.db.update_slot(slot)
            self._slots[slot.id] = slot
            logger.info(f"[SLOT {slot.id}] Released from cooldown. Balance: ${slot.balance:.2f}")

    def release_slot(self, slot: Slot):
        """
        Immediately release a slot (e.g., fill failed).
        No balance change, no cooldown.
        """
        slot.state = SlotState.AVAILABLE
        slot.current_symbol = None
        slot.current_trade_id = None
        self.db.update_slot(slot)
        self._slots[slot.id] = slot
        logger.info(f"[SLOT {slot.id}] Released (no trade executed)")

    def get_total_balance(self) -> Decimal:
        """Sum of all slot balances (not including unrealized P&L)."""
        return sum(s.balance for s in self._slots.values())

    def get_total_balance_with_positions(self, unrealized_pnl: Decimal = Decimal("0")) -> Decimal:
        """Total balance including unrealized P&L from open positions."""
        return self.get_total_balance() + unrealized_pnl

    def calculate_position_size(self, slot: Slot) -> Decimal:
        """
        Calculate position size for a slot.
        Position size = slot_balance × leverage
        """
        return slot.balance * Decimal(str(self.config.leverage))

    def get_status_summary(self) -> str:
        """Get a formatted status summary of all slots."""
        lines = ["═══ SLOT STATUS ═══"]
        total = Decimal("0")
        for s in sorted(self._slots.values(), key=lambda x: x.id):
            emoji = {
                SlotState.AVAILABLE: "🟢",
                SlotState.ASSIGNED: "🟡",
                SlotState.IN_TRADE: "🔵",
                SlotState.COOLDOWN: "⏳",
                SlotState.FROZEN: "🔴",
            }.get(s.state, "⚪")
            sym = f" ({s.current_symbol})" if s.current_symbol else ""
            lines.append(
                f"{emoji} Slot {s.id}: ${s.balance:.2f} [{s.state.value}]{sym}"
            )
            total += s.balance
        lines.append(f"═══ TOTAL: ${total:.2f} ═══")
        return "\n".join(lines)
