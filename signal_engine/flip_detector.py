
"""
Flip Detector Module

Responsibilities:
- Detect HA color flip (Bullish/Bearish) on completed 4H candles
- Logic:
  bullish_flip = current_HA_Close > current_HA_Open AND prev_HA_Close < prev_HA_Open
  bearish_flip = current_HA_Close < current_HA_Open AND prev_HA_Close > prev_HA_Open
- Enforce per-asset cooldown (30 mins)
"""
class FlipDetector:
    pass
