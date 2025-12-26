# hyperbot/vacuum/__init__.py
"""
Liquidity Vacuum Scalper - Industrial-grade structural scalping.

Core components:
- WallAnalyzer: Industrial wall detection with strength, erosion, churn
- RegimeManager: Score-based regime with hysteresis
- VacuumDetector: Thin liquidity zone identification
- BinanceBookState: Binance structural leader state
"""

from .wall_analyzer import WallAnalyzer, WallAnalyzerConfig, WallCandidate, WallState
from .regime_manager import RegimeManager, RegimeConfig, Regime
from .vacuum_detector import VacuumDetector, VacuumSignal
from .wall_tracker import WallTracker, WallTouch
from .lir_calculator import LIRCalculator, LIRSignal
from .liquidity_map import LiquidityMap, LiquidityWall, LiquidityLevel
from .vacuum_state import VacuumCoinState
from .binance_state import BinanceBookState

__all__ = [
    # Industrial analyzers
    "WallAnalyzer",
    "WallAnalyzerConfig",
    "WallCandidate",
    "WallState",
    "RegimeManager",
    "RegimeConfig",
    "Regime",
    # Vacuum detection
    "VacuumDetector",
    "VacuumSignal",
    # Legacy (for Hyper-only mode)
    "LiquidityMap",
    "LiquidityWall",
    "LiquidityLevel",
    "WallTracker",
    "WallTouch",
    "LIRCalculator",
    "LIRSignal",
    # State containers
    "VacuumCoinState",
    "BinanceBookState",
]
