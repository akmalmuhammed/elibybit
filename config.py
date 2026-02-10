"""
HA Flip Bot — Configuration
All tunable parameters in one place.
"""

import os
from decimal import Decimal
from dataclasses import dataclass, field
from typing import List


@dataclass
class StrategyConfig:
    ha_timeframe: str = "240"           # 4-hour candles (minutes)
    atr_timeframe: str = "15"           # 15-minute candles for ATR
    atr_period: int = 14                # ATR lookback periods
    tp_levels: int = 10                 # Number of TP levels
    initial_sl_pct: Decimal = Decimal("0.025")  # 2.5% initial SL


@dataclass
class SlotConfig:
    num_slots: int = 8
    initial_balance: Decimal = Decimal("10.0")
    min_balance: Decimal = Decimal("5.0")       # Freeze below this
    leverage: int = 8


@dataclass
class ExecutionConfig:
    fill_timeout_sec: int = 15          # Per attempt
    max_fill_retries: int = 3           # Total attempts (Tier 1-3)
    cooldown_minutes: int = 30          # Per-asset cooldown
    post_only_retries: int = 2          # Tier 1 & 2 are PostOnly
    # Tier 3 uses regular limit (may pay taker fee)
    dry_run: bool = True                # Paper mode — log signals, no real orders


@dataclass
class RiskConfig:
    kill_switch_threshold: Decimal = Decimal("30.0")
    kill_switch_check_interval: int = 60  # Seconds


@dataclass
class CoinConfig:
    num_coins: int = 20
    excluded_stablecoins: List[str] = field(default_factory=lambda: [
        "USDC", "USDT", "DAI", "TUSD", "BUSD", "FDUSD",
        "USDCUSDT", "DAIUSDT", "TUSDUSDT", "BUSDUSDT", "FDUSDUSDT",
    ])
    coin_refresh_interval_hours: int = 4
    ha_history_candles: int = 200        # Candles to fetch on startup


@dataclass
class ExchangeConfig:
    api_key: str = ""
    api_secret: str = ""
    testnet: bool = False               # PRODUCTION
    base_url_mainnet: str = "https://api.bybit.com"
    base_url_testnet: str = "https://api-testnet.bybit.com"
    ws_public_mainnet: str = "wss://stream.bybit.com/v5/public/linear"
    ws_private_mainnet: str = "wss://stream.bybit.com/v5/private"
    ws_public_testnet: str = "wss://stream-testnet.bybit.com/v5/public/linear"
    ws_private_testnet: str = "wss://stream-testnet.bybit.com/v5/private"

    @property
    def base_url(self) -> str:
        return self.base_url_testnet if self.testnet else self.base_url_mainnet

    @property
    def ws_public_url(self) -> str:
        return self.ws_public_testnet if self.testnet else self.ws_public_mainnet

    @property
    def ws_private_url(self) -> str:
        return self.ws_private_testnet if self.testnet else self.ws_private_mainnet


@dataclass
class NotificationConfig:
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    enabled: bool = True


@dataclass
class StorageConfig:
    db_path: str = "./data/bot.db"


@dataclass
class BotConfig:
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    slots: SlotConfig = field(default_factory=SlotConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    coins: CoinConfig = field(default_factory=CoinConfig)
    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "BotConfig":
        """Load config with environment variable overrides."""
        config = cls()
        config.exchange.api_key = os.getenv("BYBIT_API_KEY", "")
        config.exchange.api_secret = os.getenv("BYBIT_API_SECRET", "")
        config.exchange.testnet = os.getenv("BYBIT_TESTNET", "false").lower() == "true"
        config.notifications.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        config.notifications.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        config.storage.db_path = os.getenv("DB_PATH", "./data/bot.db")
        config.log_level = os.getenv("LOG_LEVEL", "INFO")
        config.execution.dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
        return config
