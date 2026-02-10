
"""
WebSocket Stream Module

Responsibilities:
- Manage Bybit V5 WebSocket connections (Public & Private)
- Subscriptions:
  - kline.240 (4H) -> 20 coins
  - kline.15 (15M) -> 20 coins
  - tickers -> 20 coins
  - execution (Fill notifications)
  - order (Status updates)
  - position (Updates)
- Handle "confirm": true for candle closes
"""
class StreamManager:
    pass
