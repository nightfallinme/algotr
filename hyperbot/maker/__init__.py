# hyperbot/maker/__init__.py
"""
Maker-First Micro-Reversion Engine for low-range markets.

This module provides an alternative trading strategy that:
- Activates only in quiet/range-bound markets
- FADES OFI spikes instead of chasing them
- Uses maker orders for both entry and exit
- Targets mean reversion to mid-price
"""
from .maker_config import MakerConfig, MakerRegimeConfig
from .regime_detector import RegimeDetector, MarketRegime
from .maker_engine import MakerEngine

__all__ = [
    "MakerConfig",
    "MakerRegimeConfig",
    "RegimeDetector",
    "MarketRegime",
    "MakerEngine",
]
