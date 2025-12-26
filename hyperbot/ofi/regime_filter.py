# hyperbot/ofi/regime_filter.py
"""
Micro-Trend Regime Filter.

Detects trending vs ranging market conditions using:
- Price slope (linear regression)
- OFI persistence (consecutive same-sign signals)

When in TREND mode, scalping is disabled to avoid counter-trend trades.
"""
from dataclasses import dataclass
from typing import Optional, List
from enum import Enum, auto
import statistics

from ..ws.data_structures import RingBuffer
from ..logger import get_logger

log = get_logger(__name__)


class RegimeType(Enum):
    """Market regime classification."""
    RANGE = auto()      # Sideways, good for scalping
    TREND_UP = auto()   # Strong uptrend, avoid shorts
    TREND_DOWN = auto() # Strong downtrend, avoid longs
    UNKNOWN = auto()    # Insufficient data


@dataclass
class RegimeState:
    """Current regime detection state."""
    regime: RegimeType = RegimeType.UNKNOWN
    slope: float = 0.0
    slope_z: float = 0.0
    ofi_persistence: int = 0
    is_ready: bool = False
    reason: str = ""


class RegimeFilter:
    """
    Detects micro-trend regime from price action and OFI.
    
    Logic:
        slope = linear regression of price over window
        slopeZ = abs(slope) / avg_abs_slope
        ofi_persistence = consecutive same-sign OFI updates
        
        if slopeZ > threshold AND persistence > min_persistence:
            TREND (UP or DOWN based on slope sign)
        else:
            RANGE
    
    Usage:
        filter = RegimeFilter()
        state = filter.update(current_price, ofi_sign)
        if state.regime == RegimeType.TREND_UP:
            # Avoid short entries
    """
    
    def __init__(
        self,
        window_ticks: int = 100,
        slope_z_threshold: float = 2.0,
        min_persistence: int = 5,
        min_ready_ticks: int = 50,
    ):
        self.window_ticks = window_ticks
        self.slope_z_threshold = slope_z_threshold
        self.min_persistence = min_persistence
        self.min_ready_ticks = min_ready_ticks
        
        # Price history for slope calculation
        self._price_buffer: RingBuffer = RingBuffer(maxlen=window_ticks)
        
        # Slope history for baseline
        self._slope_buffer: RingBuffer = RingBuffer(maxlen=window_ticks * 2)
        
        # OFI persistence tracking
        self._last_ofi_sign: int = 0
        self._persistence_count: int = 0
    
    def update(self, price: float, ofi_sign: int) -> RegimeState:
        """
        Update regime detection with new data.
        
        Args:
            price: Current microprice
            ofi_sign: OFI direction (+1, -1, or 0)
            
        Returns:
            Current regime state
        """
        state = RegimeState()
        
        # Update price buffer
        self._price_buffer.push(price)
        
        # Update OFI persistence
        if ofi_sign != 0:
            if ofi_sign == self._last_ofi_sign:
                self._persistence_count += 1
            else:
                self._persistence_count = 1
                self._last_ofi_sign = ofi_sign
        
        state.ofi_persistence = self._persistence_count
        
        # Check readiness
        if self._price_buffer.size < self.min_ready_ticks:
            state.reason = f"Need {self.min_ready_ticks} ticks, have {self._price_buffer.size}"
            return state
        
        # Calculate slope
        prices = self._price_buffer.last()
        slope = self._calculate_slope(prices)
        state.slope = slope
        
        # Update slope history
        self._slope_buffer.push(abs(slope))
        
        # Calculate slope Z-score
        if self._slope_buffer.size >= self.min_ready_ticks // 2:
            avg_slope = self._slope_buffer.mean()
            if avg_slope > 1e-9:
                state.slope_z = abs(slope) / avg_slope
        
        state.is_ready = True
        
        # Determine regime
        if state.slope_z > self.slope_z_threshold and state.ofi_persistence >= self.min_persistence:
            if slope > 0:
                state.regime = RegimeType.TREND_UP
                state.reason = f"slopeZ={state.slope_z:.2f} persistence={state.ofi_persistence}"
            else:
                state.regime = RegimeType.TREND_DOWN
                state.reason = f"slopeZ={state.slope_z:.2f} persistence={state.ofi_persistence}"
        else:
            state.regime = RegimeType.RANGE
        
        return state
    
    def _calculate_slope(self, prices: List[float]) -> float:
        """
        Calculate linear regression slope.
        
        Simple approach: (last - first) / n
        More robust: least squares regression
        """
        n = len(prices)
        if n < 2:
            return 0.0
        
        # Simple slope: (end - start) / n
        # Normalized by average price to make it comparable
        avg_price = statistics.mean(prices)
        if avg_price <= 0:
            return 0.0
        
        raw_slope = (prices[-1] - prices[0]) / n
        return raw_slope / avg_price * 10000  # Return as bps per tick
    
    def is_ready(self) -> bool:
        """Check if filter has enough data."""
        return self._price_buffer.size >= self.min_ready_ticks
    
    def reset(self) -> None:
        """Reset filter state."""
        self._price_buffer.clear()
        self._slope_buffer.clear()
        self._last_ofi_sign = 0
        self._persistence_count = 0
