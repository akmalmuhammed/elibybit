"""
HA Flip Trading Bot — Main Orchestrator.
Ties all components together: startup, data loading, WS connections, shutdown.
"""

from __future__ import annotations
import asyncio
import os
import sys
import signal
from decimal import Decimal
from datetime import datetime
from typing import List
import logging

# Load .env file before anything else
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv not installed, rely on real env vars

# Create data dir before FileHandler
os.makedirs("data", exist_ok=True)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("data/bot.log"),
    ],
)
logger = logging.getLogger(__name__)

from config import BotConfig
from exchange.models import Candle, SlotState
from exchange.bybit_rest import BybitRestClient
from exchange.bybit_ws import BybitWSManager
from core.heiken_ashi import HeikenAshiEngine
from core.atr import ATRCalculator
from core.coin_selector import CoinSelector
from core.signal_engine import SignalEngine
from trading.slot_manager import SlotManager
from trading.order_executor import OrderExecutor
from trading.risk_manager import RiskManager
from trading.kill_switch import KillSwitch
from storage.database import Database
from notifications.telegram import TelegramNotifier


class Bot:
    """Main bot orchestrator."""

    def __init__(self, config: BotConfig):
        self.config = config
        self._running = False

        # Initialize components
        self.db = Database(config.storage.db_path)
        self.client = BybitRestClient(
            api_key=config.exchange.api_key,
            api_secret=config.exchange.api_secret,
            base_url=config.exchange.base_url,
        )
        self.notifier = TelegramNotifier(
            bot_token=config.notifications.telegram_bot_token,
            chat_id=config.notifications.telegram_chat_id,
            enabled=config.notifications.enabled,
        )

        # Core engines
        self.ha_engine = HeikenAshiEngine()
        self.atr_calc = ATRCalculator(period=config.strategy.atr_period)
        self.coin_selector = CoinSelector(
            num_coins=config.coins.num_coins,
            excluded_stablecoins=config.coins.excluded_stablecoins,
        )

        # Trading components
        self.slot_manager = SlotManager(config.slots, self.db)
        self.order_executor = OrderExecutor(self.client, config.execution)
        self.risk_manager = RiskManager(
            self.client, config.strategy, self.atr_calc, self.db
        )

        # Signal engine
        self.signal_engine = SignalEngine(
            config=config,
            ha_engine=self.ha_engine,
            atr_calc=self.atr_calc,
            coin_selector=self.coin_selector,
            slot_manager=self.slot_manager,
            order_executor=self.order_executor,
            risk_manager=self.risk_manager,
            client=self.client,
            db=self.db,
            notifier=self.notifier,
        )

        # Kill switch
        self.kill_switch = KillSwitch(
            config=config.risk,
            slot_manager=self.slot_manager,
            risk_manager=self.risk_manager,
            order_executor=self.order_executor,
            client=self.client,
            db=self.db,
            notifier=self.notifier,
        )

        # WebSocket manager
        self.ws = BybitWSManager(
            public_url=config.exchange.ws_public_url,
            private_url=config.exchange.ws_private_url,
            api_key=config.exchange.api_key,
            api_secret=config.exchange.api_secret,
        )

    async def start(self):
        """Full startup sequence."""
        logger.info("=" * 60)
        logger.info("   HA FLIP TRADING BOT — STARTING")
        logger.info("=" * 60)

        # 1. Connect database
        os.makedirs(os.path.dirname(self.config.storage.db_path) or "data", exist_ok=True)
        self.db.connect()

        # 2. Check kill switch state
        if self.db.get_state("kill_switch_triggered") == "true":
            logger.critical(
                "[BOOT] Kill switch was previously triggered. "
                "Clear it manually: UPDATE bot_state SET value='false' WHERE key='kill_switch_triggered'"
            )
            await self.notifier.send("🚨 Bot attempted to start but kill switch is still triggered.")
            return

        # 3. Initialize slots
        self.slot_manager.initialize()

        # 4. Recover any open trades from DB
        self.risk_manager.load_active_trades()

        # 5. Select top 20 coins
        logger.info("[BOOT] Fetching top coins by volume...")
        added, removed = await self.coin_selector.refresh(self.client)
        logger.info(f"[BOOT] Tracking {len(self.coin_selector.symbols)} coins")

        # 6. Load historical data for each coin
        await self._load_historical_data()

        # 7. Register WebSocket handlers
        self.ws.on("kline.240", self.signal_engine.on_kline_240)
        self.ws.on("kline.5", self.signal_engine.on_kline_5)
        self.ws.on("kline.15", self.signal_engine.on_kline_15)
        self.ws.on("tickers", self.signal_engine.on_ticker)
        self.ws.on("position", self.signal_engine.on_position_update)
        self.ws.on("execution", self.signal_engine.on_execution)

        # 8. Subscribe to coin streams
        await self.ws.subscribe_symbols(self.coin_selector.symbols)

        # 9. Send startup notification
        await self.notifier.send_bot_status(
            f"Started ✅\n"
            f"Coins: {len(self.coin_selector.symbols)}\n"
            f"Slots: {self.slot_manager.count_available()} available, "
            f"{self.slot_manager.count_in_trade()} in trade\n"
            f"Total balance: ${self.slot_manager.get_total_balance():.2f}"
        )

        # 10. Run all async tasks
        self._running = True
        logger.info("[BOOT] ✅ All systems go. Running...")

        await asyncio.gather(
            self.ws.start(),
            self.kill_switch.start(),
            self._coin_refresh_loop(),
            self._daily_summary_loop(),
        )

    async def stop(self):
        """Graceful shutdown."""
        logger.info("[SHUTDOWN] Stopping bot...")
        self._running = False

        await self.kill_switch.stop()
        await self.ws.stop()
        await self.client.close()
        await self.notifier.send_bot_status("Stopped 🔴")
        await self.notifier.close()
        self.db.close()

        logger.info("[SHUTDOWN] Complete.")

    async def _load_historical_data(self):
        """Load historical 4H and 15M candles for all tracked coins."""
        symbols = self.coin_selector.symbols
        logger.info(f"[BOOT] Loading historical data for {len(symbols)} coins...")

        for symbol in symbols:
            try:
                # Fetch 4H candles for HA calculation
                raw_4h = await self.client.get_klines(
                    symbol=symbol,
                    interval="240",
                    limit=self.config.coins.ha_history_candles,
                )

                candles_4h = self._parse_klines(raw_4h)
                if candles_4h:
                    self.ha_engine.build_from_history(symbol, candles_4h)
                else:
                    logger.warning(f"[BOOT] {symbol}: No 4H candle data")

                # Fetch 15M candles for ATR
                raw_15m = await self.client.get_klines(
                    symbol=symbol,
                    interval="15",
                    limit=self.config.strategy.atr_period + 10,
                )

                candles_15m = self._parse_klines(raw_15m)
                if candles_15m:
                    self.atr_calc.initialize(symbol, candles_15m)
                else:
                    logger.warning(f"[BOOT] {symbol}: No 15M candle data")

                # Small delay to respect rate limits
                await asyncio.sleep(0.1)

            except Exception as e:
                logger.error(f"[BOOT] {symbol}: Error loading history: {e}")

        logger.info("[BOOT] Historical data loaded for all coins.")

    def _parse_klines(self, raw_klines: List) -> List[Candle]:
        """Parse raw Bybit kline data into Candle objects. Reverse to oldest-first."""
        candles = []
        for k in reversed(raw_klines):
            # Bybit V5 kline format: [startTime, open, high, low, close, volume, turnover]
            try:
                candles.append(Candle(
                    timestamp=int(k[0]),
                    open=Decimal(str(k[1])),
                    high=Decimal(str(k[2])),
                    low=Decimal(str(k[3])),
                    close=Decimal(str(k[4])),
                    volume=Decimal(str(k[5])),
                    confirmed=True,
                ))
            except (IndexError, ValueError) as e:
                logger.warning(f"[BOOT] Bad kline data: {k}: {e}")
        return candles

    async def _coin_refresh_loop(self):
        """Periodically refresh the coin list."""
        interval = self.config.coins.coin_refresh_interval_hours * 3600

        while self._running:
            await asyncio.sleep(interval)
            if not self._running:
                break

            try:
                logger.info("[COINS] Refreshing coin list...")
                added, removed = await self.coin_selector.refresh(self.client)

                # Subscribe to new coins
                if added:
                    await self.ws.subscribe_symbols(added)
                    for symbol in added:
                        await self._load_single_coin_history(symbol)

                # Unsubscribe removed coins (only if not in active trade)
                safe_to_remove = [
                    s for s in removed
                    if not self.coin_selector.is_in_trade(s)
                ]
                if safe_to_remove:
                    await self.ws.unsubscribe_symbols(safe_to_remove)
                    for symbol in safe_to_remove:
                        self.ha_engine.remove_symbol(symbol)
                        self.atr_calc.remove_symbol(symbol)

            except Exception as e:
                logger.error(f"[COINS] Refresh error: {e}", exc_info=True)

    async def _load_single_coin_history(self, symbol: str):
        """Load historical data for a single newly added coin."""
        try:
            raw_4h = await self.client.get_klines(symbol=symbol, interval="240", limit=50)
            candles_4h = self._parse_klines(raw_4h)
            if candles_4h:
                self.ha_engine.build_from_history(symbol, candles_4h)

            raw_15m = await self.client.get_klines(symbol=symbol, interval="15", limit=24)
            candles_15m = self._parse_klines(raw_15m)
            if candles_15m:
                self.atr_calc.initialize(symbol, candles_15m)

        except Exception as e:
            logger.error(f"[BOOT] {symbol}: Error loading history: {e}")

    async def _daily_summary_loop(self):
        """Send daily P&L summary at 00:05 UTC."""
        while self._running:
            now = datetime.utcnow()
            # Calculate seconds until next 00:05 UTC
            tomorrow = now.replace(hour=0, minute=5, second=0, microsecond=0)
            if now >= tomorrow:
                from datetime import timedelta
                tomorrow += timedelta(days=1)
            wait_seconds = (tomorrow - now).total_seconds()

            await asyncio.sleep(wait_seconds)
            if not self._running:
                break

            try:
                summary = self.slot_manager.get_status_summary()
                await self.notifier.send_daily_summary(summary)
            except Exception as e:
                logger.error(f"[DAILY] Summary error: {e}")


async def main():
    """Entry point."""
    config = BotConfig.from_env()

    # Validate critical config
    if not config.exchange.api_key or not config.exchange.api_secret:
        logger.critical("BYBIT_API_KEY and BYBIT_API_SECRET must be set!")
        sys.exit(1)

    bot = Bot(config)

    # Graceful shutdown handler
    if sys.platform != "win32":
        loop = asyncio.get_event_loop()
        def handle_signal(sig):
            logger.info(f"Received signal {sig}. Initiating shutdown...")
            asyncio.create_task(bot.stop())

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda s=sig: handle_signal(s))

    try:
        await bot.start()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received in main loop.")
        await bot.stop()
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        await bot.stop()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
