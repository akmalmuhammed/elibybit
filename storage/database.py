"""
SQLite Storage Layer.
Handles persistence for slots, trades, HA candles, and bot state.
All monetary values stored as TEXT to preserve Decimal precision.
"""

from __future__ import annotations
import sqlite3
import json
from decimal import Decimal
from datetime import datetime
from typing import List, Optional, Dict, Any
from exchange.models import Slot, SlotState, Trade, TradeStatus, ExitReason, Side, TPLevel
import logging

logger = logging.getLogger(__name__)


class Database:
    """SQLite database manager with typed accessors."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    def connect(self):
        """Initialize database connection and create tables."""
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._create_tables()
        logger.info(f"[DB] Connected to {self.db_path}")

    def close(self):
        if self._conn:
            self._conn.close()

    @property
    def conn(self) -> sqlite3.Connection:
        assert self._conn is not None, "Database not connected"
        return self._conn

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS slots (
                id INTEGER PRIMARY KEY,
                balance TEXT NOT NULL DEFAULT '10.0',
                state TEXT NOT NULL DEFAULT 'AVAILABLE',
                current_symbol TEXT,
                current_trade_id INTEGER,
                total_trades INTEGER NOT NULL DEFAULT 0,
                total_pnl TEXT NOT NULL DEFAULT '0',
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slot_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price TEXT,
                qty TEXT,
                order_id TEXT,
                sl_order_id TEXT,
                current_sl_price TEXT,
                initial_sl_price TEXT,
                tp_levels TEXT,
                highest_tp_reached INTEGER DEFAULT 0,
                atr_value TEXT,
                status TEXT NOT NULL DEFAULT 'PENDING',
                pnl TEXT,
                fees TEXT DEFAULT '0',
                entry_time TEXT,
                exit_time TEXT,
                exit_reason TEXT,
                cooldown_until TEXT,
                fill_attempts INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS ha_candles (
                symbol TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                ha_open TEXT,
                ha_close TEXT,
                ha_high TEXT,
                ha_low TEXT,
                is_bullish INTEGER,
                PRIMARY KEY (symbol, timestamp)
            );

            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_trades_slot ON trades(slot_id);
            CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
            CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
        """)
        self.conn.commit()

    # ==================== Slot Operations ====================

    def initialize_slots(self, num_slots: int, initial_balance: Decimal):
        """Create slot records if they don't exist."""
        for i in range(1, num_slots + 1):
            existing = self.conn.execute("SELECT id FROM slots WHERE id = ?", (i,)).fetchone()
            if not existing:
                self.conn.execute(
                    "INSERT INTO slots (id, balance, state, updated_at) VALUES (?, ?, ?, ?)",
                    (i, str(initial_balance), SlotState.AVAILABLE.value, datetime.utcnow().isoformat()),
                )
        self.conn.commit()
        logger.info(f"[DB] Initialized {num_slots} slots @ ${initial_balance} each")

    def get_slot(self, slot_id: int) -> Optional[Slot]:
        row = self.conn.execute("SELECT * FROM slots WHERE id = ?", (slot_id,)).fetchone()
        return self._row_to_slot(row) if row else None

    def get_all_slots(self) -> List[Slot]:
        rows = self.conn.execute("SELECT * FROM slots ORDER BY id").fetchall()
        return [self._row_to_slot(r) for r in rows]

    def get_available_slot(self) -> Optional[Slot]:
        """Get first available slot."""
        row = self.conn.execute(
            "SELECT * FROM slots WHERE state = ? ORDER BY id LIMIT 1",
            (SlotState.AVAILABLE.value,),
        ).fetchone()
        return self._row_to_slot(row) if row else None

    def update_slot(self, slot: Slot):
        self.conn.execute(
            """UPDATE slots SET balance=?, state=?, current_symbol=?,
               current_trade_id=?, total_trades=?, total_pnl=?, updated_at=?
               WHERE id=?""",
            (
                str(slot.balance), slot.state.value, slot.current_symbol,
                slot.current_trade_id, slot.total_trades, str(slot.total_pnl),
                datetime.utcnow().isoformat(), slot.id,
            ),
        )
        self.conn.commit()

    def get_total_balance(self) -> Decimal:
        """Sum of all slot balances."""
        row = self.conn.execute("SELECT SUM(CAST(balance AS REAL)) as total FROM slots").fetchone()
        return Decimal(str(row["total"])) if row and row["total"] else Decimal("0")

    # ==================== Trade Operations ====================

    def create_trade(self, trade: Trade) -> int:
        """Insert a new trade and return its ID."""
        tp_json = json.dumps([
            {"level": tp.level, "price": str(tp.price), "hit": tp.hit}
            for tp in trade.tp_levels
        ]) if trade.tp_levels else "[]"

        cursor = self.conn.execute(
            """INSERT INTO trades (slot_id, symbol, side, entry_price, qty, order_id,
               sl_order_id, current_sl_price, initial_sl_price, tp_levels,
               highest_tp_reached, atr_value, status, pnl, fees, entry_time,
               exit_time, exit_reason, cooldown_until, fill_attempts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                trade.slot_id, trade.symbol, trade.side.value,
                str(trade.entry_price) if trade.entry_price else None,
                str(trade.qty) if trade.qty else None,
                trade.order_id, trade.sl_order_id,
                str(trade.current_sl_price) if trade.current_sl_price else None,
                str(trade.initial_sl_price) if trade.initial_sl_price else None,
                tp_json, trade.highest_tp_reached,
                str(trade.atr_value) if trade.atr_value else None,
                trade.status.value,
                str(trade.pnl) if trade.pnl else None,
                str(trade.fees),
                trade.entry_time.isoformat() if trade.entry_time else None,
                trade.exit_time.isoformat() if trade.exit_time else None,
                trade.exit_reason.value if trade.exit_reason else None,
                trade.cooldown_until.isoformat() if trade.cooldown_until else None,
                trade.fill_attempts,
            ),
        )
        self.conn.commit()
        trade.id = cursor.lastrowid
        return trade.id

    def update_trade(self, trade: Trade):
        """Update an existing trade."""
        tp_json = json.dumps([
            {"level": tp.level, "price": str(tp.price), "hit": tp.hit,
             "hit_time": tp.hit_time.isoformat() if tp.hit_time else None}
            for tp in trade.tp_levels
        ]) if trade.tp_levels else "[]"

        self.conn.execute(
            """UPDATE trades SET entry_price=?, qty=?, order_id=?, sl_order_id=?,
               current_sl_price=?, initial_sl_price=?, tp_levels=?,
               highest_tp_reached=?, atr_value=?, status=?, pnl=?, fees=?,
               entry_time=?, exit_time=?, exit_reason=?, cooldown_until=?,
               fill_attempts=?
               WHERE id=?""",
            (
                str(trade.entry_price) if trade.entry_price else None,
                str(trade.qty) if trade.qty else None,
                trade.order_id, trade.sl_order_id,
                str(trade.current_sl_price) if trade.current_sl_price else None,
                str(trade.initial_sl_price) if trade.initial_sl_price else None,
                tp_json, trade.highest_tp_reached,
                str(trade.atr_value) if trade.atr_value else None,
                trade.status.value,
                str(trade.pnl) if trade.pnl else None,
                str(trade.fees),
                trade.entry_time.isoformat() if trade.entry_time else None,
                trade.exit_time.isoformat() if trade.exit_time else None,
                trade.exit_reason.value if trade.exit_reason else None,
                trade.cooldown_until.isoformat() if trade.cooldown_until else None,
                trade.fill_attempts,
                trade.id,
            ),
        )
        self.conn.commit()

    def get_trade(self, trade_id: int) -> Optional[Trade]:
        row = self.conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
        return self._row_to_trade(row) if row else None

    def get_open_trades(self) -> List[Trade]:
        rows = self.conn.execute(
            "SELECT * FROM trades WHERE status IN (?, ?, ?)",
            (TradeStatus.PENDING.value, TradeStatus.FILLING.value, TradeStatus.OPEN.value),
        ).fetchall()
        return [self._row_to_trade(r) for r in rows]

    def get_trade_by_symbol(self, symbol: str) -> Optional[Trade]:
        """Get active trade for a symbol."""
        row = self.conn.execute(
            "SELECT * FROM trades WHERE symbol = ? AND status IN (?, ?, ?) LIMIT 1",
            (symbol, TradeStatus.PENDING.value, TradeStatus.FILLING.value, TradeStatus.OPEN.value),
        ).fetchone()
        return self._row_to_trade(row) if row else None

    def get_trade_by_order_id(self, order_id: str) -> Optional[Trade]:
        """Find trade by its entry order ID."""
        row = self.conn.execute(
            "SELECT * FROM trades WHERE order_id = ? LIMIT 1",
            (order_id,),
        ).fetchone()
        return self._row_to_trade(row) if row else None

    # ==================== Bot State ====================

    def set_state(self, key: str, value: str):
        self.conn.execute(
            "INSERT OR REPLACE INTO bot_state (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, datetime.utcnow().isoformat()),
        )
        self.conn.commit()

    def get_state(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    # ==================== Row Converters ====================

    def _row_to_slot(self, row) -> Slot:
        return Slot(
            id=row["id"],
            balance=Decimal(row["balance"]),
            state=SlotState(row["state"]),
            current_symbol=row["current_symbol"],
            current_trade_id=row["current_trade_id"],
            total_trades=row["total_trades"],
            total_pnl=Decimal(row["total_pnl"]),
            updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else datetime.utcnow(),
        )

    def _row_to_trade(self, row) -> Trade:
        tp_data = json.loads(row["tp_levels"]) if row["tp_levels"] else []
        tp_levels = [
            TPLevel(
                level=tp["level"],
                price=Decimal(tp["price"]),
                hit=tp.get("hit", False),
                hit_time=datetime.fromisoformat(tp["hit_time"]) if tp.get("hit_time") else None,
            )
            for tp in tp_data
        ]

        return Trade(
            id=row["id"],
            slot_id=row["slot_id"],
            symbol=row["symbol"],
            side=Side(row["side"]),
            entry_price=Decimal(row["entry_price"]) if row["entry_price"] else None,
            qty=Decimal(row["qty"]) if row["qty"] else None,
            order_id=row["order_id"],
            sl_order_id=row["sl_order_id"],
            current_sl_price=Decimal(row["current_sl_price"]) if row["current_sl_price"] else None,
            initial_sl_price=Decimal(row["initial_sl_price"]) if row["initial_sl_price"] else None,
            tp_levels=tp_levels,
            highest_tp_reached=row["highest_tp_reached"],
            atr_value=Decimal(row["atr_value"]) if row["atr_value"] else None,
            status=TradeStatus(row["status"]),
            pnl=Decimal(row["pnl"]) if row["pnl"] else None,
            fees=Decimal(row["fees"]) if row["fees"] else Decimal("0"),
            entry_time=datetime.fromisoformat(row["entry_time"]) if row["entry_time"] else None,
            exit_time=datetime.fromisoformat(row["exit_time"]) if row["exit_time"] else None,
            exit_reason=ExitReason(row["exit_reason"]) if row["exit_reason"] else None,
            cooldown_until=datetime.fromisoformat(row["cooldown_until"]) if row["cooldown_until"] else None,
            fill_attempts=row["fill_attempts"],
        )
