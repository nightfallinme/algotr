# hyperbot/vacuum/lir_calculator.py
"""
Liquidity Imbalance Ratio (LIR) Calculator.

Mathematical Definition:
    LIR = sum(bid_size within X bps) / sum(ask_size within X bps)
    
    Interpretation:
        LIR > 1.0 = More bid depth = bullish bias
        LIR < 1.0 = More ask depth = bearish bias
        LIR ~= 1.0 = Balanced
    
    Trading thresholds:
        LONG: LIR > 1.5 (strong bid support)
        SHORT: LIR < 0.66 (strong ask pressure)
"""
from dataclasses import dataclass
from typing import Optional

from .liquidity_map import LiquidityMap
from ..ws.data_structures import EMACalculator
from ..logger import get_logger

log = get_logger(__name__)


@dataclass
class LIRSignal:
    """LIR calculation result."""
    lir_raw: float = 1.0  # Raw LIR value
    lir_smooth: float = 1.0  # EMA-smoothed LIR
    bid_depth: float = 0.0  # Total bid depth in range
    ask_depth: float = 0.0  # Total ask depth in range
    depth_bps: float = 0.0  # Range used for calculation
    is_ready: bool = False
    
    @property
    def is_bullish(self) -> bool:
        """True if LIR indicates bullish (more bids)."""
        return self.lir_smooth > 1.0
    
    @property
    def is_bearish(self) -> bool:
        """True if LIR indicates bearish (more asks)."""
        return self.lir_smooth < 1.0
    
    def to_dict(self) -> dict:
        return {
            "lir_raw": round(self.lir_raw, 3),
            "lir_smooth": round(self.lir_smooth, 3),
            "bid_depth": round(self.bid_depth, 4),
            "ask_depth": round(self.ask_depth, 4),
            "depth_bps": self.depth_bps,
            "ready": self.is_ready,
        }


class LIRCalculator:
    """
    Calculates Liquidity Imbalance Ratio from book depth.
    
    Why LIR is better than OFI for this strategy:
    - OFI tracks CHANGES in depth (noisy, spoofable)
    - LIR tracks CURRENT depth state (structural)
    - LIR is stable with slow book updates
    - LIR is less dependent on timing
    
    Usage:
        calc = LIRCalculator(depth_bps=10.0)
        signal = calc.compute(liquidity_map)
        if signal.lir_smooth > 1.5:
            # Strong bid support -> long bias
    """
    
    def __init__(
        self,
        depth_bps: float = 10.0,
        ema_span: int = 10,
        min_depth: float = 0.001,  # Avoid division by zero
        long_threshold: float = 1.5,
        short_threshold: float = 0.66,
    ):
        """
        Args:
            depth_bps: Range from mid to measure depth
            ema_span: EMA smoothing span
            min_depth: Minimum depth to avoid div/0
            long_threshold: LIR > this = bullish
            short_threshold: LIR < this = bearish
        """
        self.depth_bps = depth_bps
        self.ema_span = ema_span
        self.min_depth = min_depth
        self.long_threshold = long_threshold
        self.short_threshold = short_threshold
        
        # Per-symbol EMA smoothers
        self._ema_smoothers: dict[str, EMACalculator] = {}
        self._update_count: dict[str, int] = {}
    
    def compute(
        self,
        liquidity_map: LiquidityMap,
        symbol: str = "default",
    ) -> LIRSignal:
        """
        Compute LIR from current book state.
        
        Args:
            liquidity_map: Current liquidity map
            symbol: Symbol identifier for EMA state
            
        Returns:
            LIRSignal with calculation result
        """
        signal = LIRSignal(depth_bps=self.depth_bps)
        
        if not liquidity_map.is_ready:
            return signal
        
        # Get depth within range
        bid_depth = liquidity_map.get_depth_within_bps("bid", self.depth_bps)
        ask_depth = liquidity_map.get_depth_within_bps("ask", self.depth_bps)
        
        signal.bid_depth = bid_depth
        signal.ask_depth = ask_depth
        
        # Calculate raw LIR
        denominator = max(ask_depth, self.min_depth)
        signal.lir_raw = bid_depth / denominator
        
        # Apply EMA smoothing
        ema = self._get_ema(symbol)
        signal.lir_smooth = ema.update(signal.lir_raw)
        
        # Update count
        self._update_count[symbol] = self._update_count.get(symbol, 0) + 1
        
        # Check if ready
        signal.is_ready = self._update_count.get(symbol, 0) >= self.ema_span // 2
        
        return signal
    
    def is_long_signal(self, signal: LIRSignal) -> bool:
        """Check if LIR indicates long entry."""
        return signal.is_ready and signal.lir_smooth > self.long_threshold
    
    def is_short_signal(self, signal: LIRSignal) -> bool:
        """Check if LIR indicates short entry."""
        return signal.is_ready and signal.lir_smooth < self.short_threshold
    
    def _get_ema(self, symbol: str) -> EMACalculator:
        """Get or create EMA smoother for symbol."""
        if symbol not in self._ema_smoothers:
            self._ema_smoothers[symbol] = EMACalculator(span=self.ema_span)
        return self._ema_smoothers[symbol]
    
    def reset(self, symbol: Optional[str] = None) -> None:
        """Reset EMA state for symbol or all."""
        if symbol:
            if symbol in self._ema_smoothers:
                self._ema_smoothers[symbol].reset()
            self._update_count[symbol] = 0
        else:
            self._ema_smoothers.clear()
            self._update_count.clear()
