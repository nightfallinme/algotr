# hyperbot/vacuum/binance_state.py
"""
Binance Book State - Industrial-grade structural leader.

Uses WallAnalyzer for wall detection and RegimeManager for regime classification.
Maintains local orderbook from delta updates via BinanceLocalBook.
"""
from dataclasses import dataclass, field
from typing import Optional, Tuple, List
import time
import logging

log = logging.getLogger(__name__)

from ..ws.binance_book import BinanceLocalBook
from .wall_analyzer import WallAnalyzer, WallAnalyzerConfig, WallCandidate, WallState
from .regime_manager import RegimeManager, RegimeConfig, Regime
from .vacuum_detector import VacuumDetector, VacuumSignal


@dataclass
class BinanceBookState:
    """
    Industrial-grade Binance structural leader state.
    
    Components:
    - BinanceLocalBook: Maintains orderbook from deltas
    - WallAnalyzer: Wall detection with strength, erosion, churn
    - RegimeManager: Score-based regime with hysteresis
    - VacuumDetector: Thin liquidity zones
    """
    symbol: str
    
    # Local orderbook (handles delta updates)
    local_book: BinanceLocalBook = field(default_factory=lambda: BinanceLocalBook(symbol="", max_depth=20))
    
    # Industrial-grade analyzers
    wall_analyzer: WallAnalyzer = field(default_factory=lambda: WallAnalyzer())
    regime_manager: RegimeManager = field(default_factory=lambda: RegimeManager())
    vacuum_detector: VacuumDetector = field(default_factory=lambda: VacuumDetector())
    
    # State
    mid: float = 0.0
    best_bid: float = 0.0
    best_ask: float = 0.0
    tick_count: int = 0
    last_update_ms: int = 0
    
    # Current signals
    bid_wall: Optional[WallCandidate] = None
    ask_wall: Optional[WallCandidate] = None
    vacuum: Optional[VacuumSignal] = None
    regime: Regime = Regime.UNKNOWN
    
    # v2: Directional vacuum (0-1, lower = more vacuum)
    vacuum_up: float = 1.0    # Ask side vacuum (for UP break)
    vacuum_down: float = 1.0  # Bid side vacuum (for DOWN break)
    
    # Aggressor tracking (trade flow veto)
    # Positive = net buy aggressor, Negative = net sell aggressor
    aggressor_bias: float = 0.0
    _aggressor_decay: float = 0.9  # EMA decay per update
    
    def __post_init__(self):
        """Initialize components."""
        self.local_book = BinanceLocalBook(symbol=self.symbol, max_depth=20)
        
        # Configure analyzers
        wall_config = WallAnalyzerConfig(
            r_min=3.0,
            lambda_bps=10.0,
            max_distance_bps=50.0,
            T_persist_ms=5000,
            T_erosion_ms=4000,
        )
        self.wall_analyzer = WallAnalyzer(wall_config)
        
        regime_config = RegimeConfig(
            theta_range_enter=1.2,
            theta_range_exit=0.9,
            theta_mom_enter=1.0,
            theta_mom_exit=0.7,
            min_regime_hold_sec=5.0,
        )
        self.regime_manager = RegimeManager(regime_config)
    
    def apply_depth_delta(
        self,
        bids: List[Tuple[float, float]],
        asks: List[Tuple[float, float]],
        timestamp_ms: Optional[int] = None,
    ) -> None:
        """
        Apply Binance depth DELTA update.
        
        Flow:
        1. Apply delta to local_book
        2. Analyze walls with WallAnalyzer
        3. Detect vacuum
        4. Update regime
        """
        now_ms = timestamp_ms or int(time.time() * 1000)
        
        # Apply delta to local book
        self.local_book.apply_depth_update(bids, asks, now_ms)
        
        # Skip if book not yet valid
        if not self.local_book.is_valid:
            return
        
        # Get full levels
        full_bids, full_asks = self.local_book.get_levels()
        
        # Update state
        self.last_update_ms = now_ms
        self.tick_count += 1
        self.best_bid = self.local_book.best_bid
        self.best_ask = self.local_book.best_ask
        self.mid = self.local_book.mid
        
        # 1) Analyze walls
        self.bid_wall, self.ask_wall = self.wall_analyzer.analyze(
            full_bids, full_asks, self.mid, now_ms
        )
        
        # 2) Vacuum detection (simplified)
        # 2) Directional vacuum detection (v2)
        self.vacuum_up = self._calc_directional_vacuum(full_asks, "up")
        self.vacuum_down = self._calc_directional_vacuum(full_bids, "down")
        
        # 3) Update regime with directional vacuums
        self.regime = self.regime_manager.update(
            self.bid_wall, self.ask_wall, 
            self.vacuum_up, self.vacuum_down,
            now_ms
        )
    
    def _calc_directional_vacuum(
        self,
        levels: List[Tuple[float, float]],
        direction: str,
        range_bps: float = 10.0,
    ) -> float:
        """
        Calculate directional vacuum fill ratio (v2.1 - self-contained).
        
        Formula: Vac = clip(vol_in_range / (D_med * L_delta + ε), 0, 1)
        
        Where:
        - D_med: median size of ALL levels on this side
        - L_delta: actual count of levels within the range
        - ε: small constant to avoid div-by-zero
        
        Returns: 0-1, lower = more vacuum (less liquidity)
        """
        if not self.mid or self.mid <= 0 or not levels:
            return 1.0
        
        # D_med: median of ALL levels on this side (self-contained)
        sizes = sorted([s for _, s in levels])
        D_med = sizes[len(sizes) // 2] if sizes else 1.0
        
        range_mult = range_bps / 10000
        
        if direction == "up":
            # Ask levels within [mid, mid + Δbps]
            in_range = [
                (p, s) for p, s in levels
                if self.mid <= p <= self.mid * (1 + range_mult)
            ]
        else:
            # Bid levels within [mid - Δbps, mid]
            in_range = [
                (p, s) for p, s in levels
                if self.mid * (1 - range_mult) <= p <= self.mid
            ]
        
        # L_delta: actual level count in range
        L_delta = len(in_range)
        vol = sum(s for _, s in in_range)
        
        # Expected = D_med × L_delta
        expected = D_med * L_delta + 0.01
        
        return min(1.0, max(0.0, vol / expected))
    
    # === Decision helpers ===
    
    def is_ready(self, min_ticks: int = 10) -> bool:
        """Check if state has enough data."""
        return self.tick_count >= min_ticks and self.mid > 0
    
    def apply_trade(self, is_buyer_maker: bool, size: float) -> None:
        """
        Apply Binance aggTrade for aggressor tracking (Fix 3: Eat Rate).
        
        Binance aggTrade:
        - is_buyer_maker=True → SELL aggressor (taker sold)
        - is_buyer_maker=False → BUY aggressor (taker bought)
        
        Updates:
        1. aggressor_bias (EMA of net flow)
        2. Wall eat_rate (via WallAnalyzer)
        """
        # Convert to signed flow
        signed_qty = size if not is_buyer_maker else -size
        
        # EMA update
        self.aggressor_bias = (
            self._aggressor_decay * self.aggressor_bias +
            (1 - self._aggressor_decay) * signed_qty
        )
        
        # Fix 3: Update eat rate for the wall being attacked
        if is_buyer_maker:
            # Sell aggressor attacks BID wall
            self.wall_analyzer.update_eat_rate("bid", size)
        else:
            # Buy aggressor attacks ASK wall
            self.wall_analyzer.update_eat_rate("ask", size)

    # === v2.1: Fix 6 & 8 Helpers ===
    
    def is_fresh(self, now_ms: int, max_stale_ms: int = 400) -> bool:
        """Fix 8: Check if Binance state is fresh (not stale)."""
        return (now_ms - self.last_update_ms) < max_stale_ms
    
    def get_deviation_bps(self, hyper_mid: float) -> float:
        """
        Fix 6: Calculate deviation using Binance Mid as reference.
        
        Returns: (Hyper - Binance) / Binance * 10000
        """
        if self.mid <= 0 or hyper_mid <= 0:
            return 0.0
        return (hyper_mid - self.mid) / self.mid * 10000
    
    def aggressor_vetoes_fade(self, fade_side: str, threshold: float = 0.5) -> bool:
        """
        Check if aggressor flow vetoes the fade direction.
        
        VETO CONDITIONS:
        - Fading SHORT but aggressor bias is strongly positive (buying pressure) → VETO
        - Fading LONG but aggressor bias is strongly negative (selling pressure) → VETO
        
        Args:
            fade_side: "short" or "long"
            threshold: Minimum aggressor bias magnitude to veto
            
        Returns:
            True if fade should be vetoed (aggressor against you)
        """
        if abs(self.aggressor_bias) < threshold:
            return False  # Weak flow, no veto
        
        if fade_side.lower() == "short":
            # Fading short means we expect price to go down
            # If aggressor is buying (positive), it's against us
            return self.aggressor_bias > threshold
        else:
            # Fading long means we expect price to go up
            # If aggressor is selling (negative), it's against us
            return self.aggressor_bias < -threshold
            
    def log_veto_reason(self, fade_side: str, dev: float, threshold: float) -> None:
        """Log why the fade was vetoed."""
        log.info(f"[DEBUG] Fade {fade_side.upper()} VETOED by Aggressor. Bias={self.aggressor_bias:.2f}. Dev={dev:.1f}bps")
    
    @property
    def break_direction(self) -> str:
        """Fix 5: Expose break direction from regime manager."""
        return self.regime_manager._state.break_direction
    
    def get_deviation_from_hyper(self, hyper_mid: float) -> float:
        """Get deviation in bps (positive = Hyper higher)."""
        if self.mid <= 0 or hyper_mid <= 0:
            return 0.0
        return (hyper_mid - self.mid) / self.mid * 10000
    
    def should_fade_ask(
        self,
        hyper_mid: float,
        min_deviation_bps: float = 2.5,
    ) -> bool:
        """
        FADE SHORT: Price near stable ask wall.
        
        Conditions:
        - Regime = RANGE_MAKER
        - Stable ask wall exists
        - Hyper price above wall (deviation >= threshold)
        """
        if self.regime != Regime.RANGE_MAKER:
            return False
        
        if not self.ask_wall or self.ask_wall.state != WallState.STABLE:
            return False
        
        # Deviation: how much Hyper is above Binance wall
        ref = self.ask_wall.price
        deviation = (hyper_mid - ref) / ref * 10000
        
        return deviation >= min_deviation_bps
    
    def should_fade_bid(
        self,
        hyper_mid: float,
        min_deviation_bps: float = 2.5,
    ) -> bool:
        """
        FADE LONG: Price near stable bid wall.
        
        Conditions:
        - Regime = RANGE_MAKER
        - Stable bid wall exists
        - Hyper price below wall (deviation <= -threshold)
        """
        if self.regime != Regime.RANGE_MAKER:
            return False
        
        if not self.bid_wall or self.bid_wall.state != WallState.STABLE:
            return False
        
        ref = self.bid_wall.price
        deviation = (hyper_mid - ref) / ref * 10000
        
        return deviation <= -min_deviation_bps
    
    def should_break_ask(self) -> bool:
        """
        BREAK LONG: Ask wall eroding/broken + vacuum.
        
        Conditions:
        - Regime = MOMENTUM_BREAK
        - Ask wall eroding or broken
        """
        if self.regime != Regime.MOMENTUM_BREAK:
            return False
        
        if not self.ask_wall:
            return True  # No wall = vacuum ahead
        
        return self.ask_wall.state in {WallState.ERODING, WallState.BROKEN}
    
    def should_break_bid(self) -> bool:
        """
        BREAK SHORT: Bid wall eroding/broken + vacuum.
        """
        if self.regime != Regime.MOMENTUM_BREAK:
            return False
        
        if not self.bid_wall:
            return True
        
        return self.bid_wall.state in {WallState.ERODING, WallState.BROKEN}
    
    # === Status helpers ===
    
    def get_status_line(self, hyper_mid: float = 0.0) -> str:
        """Get compact status for logging (includes deviation if hyper_mid provided)."""
        bid_state = self.bid_wall.state.name[:3] if self.bid_wall else "N/A"
        ask_state = self.ask_wall.state.name[:3] if self.ask_wall else "N/A"
        bid_price = f"{self.bid_wall.price:.2f}" if self.bid_wall else "N/A"
        ask_price = f"{self.ask_wall.price:.2f}" if self.ask_wall else "N/A"
        
        # ToB spread
        tob_spread = self.local_book.spread_bps
        
        # Deviation from walls (if hyper_mid provided)
        dev_str = ""
        if hyper_mid > 0 and self.mid > 0:
            dev = (hyper_mid - self.mid) / self.mid * 10000
            dev_str = f"dev={dev:+.1f}bps "
        
        return (
            f"mid={self.mid:.2f} "
            f"tob_sprd={tob_spread:.1f}bps "
            f"{dev_str}"
            f"regime={self.regime.name} "
            f"bid_wall={bid_price}({bid_state}) "
            f"ask_wall={ask_price}({ask_state})"
        )
