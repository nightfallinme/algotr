# hyperbot/vacuum/wall_tracker.py
"""
Wall Touch & Rejection Tracker.

Tracks when price touches a wall and detects rejections.

Mathematical Definition:
    wall_touch: price within touch_threshold_bps of wall
    rejection: after touch, price moves away for rejection_ticks consecutive ticks
    wall_stable: wall size not decreasing during touch period
"""
from dataclasses import dataclass, field
from typing import Optional, Dict
from collections import deque
import time

from .liquidity_map import LiquidityWall
from ..logger import get_logger

log = get_logger(__name__)


@dataclass
class WallTouch:
    """Wall touch event tracking."""
    wall_price: float
    wall_side: str  # "bid" | "ask"
    touch_price: float  # Price at moment of touch
    initial_wall_size: float
    current_wall_size: float
    touch_time_ms: int
    touch_count: int = 1  # How many ticks we've been touching
    rejection_ticks: int = 0  # Consecutive ticks price moved away
    max_rejection_distance_bps: float = 0.0  # Furthest we've been from wall during rejection
    size_history: list = None  # Last N wall sizes for erosion tracking
    
    def __post_init__(self):
        if self.size_history is None:
            self.size_history = [self.initial_wall_size]
    
    @property
    def wall_stable(self) -> bool:
        """True if wall size hasn't decreased significantly."""
        return self.current_wall_size >= self.initial_wall_size * 0.8
    
    @property
    def wall_eroding(self) -> bool:
        """True if wall size is decreasing (being absorbed)."""
        return self.current_wall_size < self.initial_wall_size * 0.7
    
    @property
    def erosion_rate(self) -> float:
        """
        Erosion rate over history window.
        Negative = shrinking (being absorbed)
        Positive = growing (getting stronger)
        """
        if len(self.size_history) < 2:
            return 0.0
        first_size = self.size_history[0]
        if first_size <= 0:
            return 0.0
        return (self.current_wall_size - first_size) / first_size
    
    def update_size_history(self, new_size: float, max_history: int = 10) -> None:
        """Add new size to history, maintaining max length."""
        self.size_history.append(new_size)
        if len(self.size_history) > max_history:
            self.size_history.pop(0)
        self.current_wall_size = new_size
    
    def to_dict(self) -> dict:
        return {
            "wall_price": self.wall_price,
            "side": self.wall_side,
            "touch_count": self.touch_count,
            "rejection_ticks": self.rejection_ticks,
            "wall_stable": self.wall_stable,
            "wall_eroding": self.wall_eroding,
            "erosion_rate": round(self.erosion_rate, 3),
            "rejection_bps": round(self.max_rejection_distance_bps, 2),
        }


class WallTracker:
    """
    Tracks wall touches and detects rejection patterns.
    
    For FADE setup (MOD 2):
        - Price touches wall
        - Wall remains stable (doesn't erode)
        - Price moves away (rejection)
        - Entry: fade in opposite direction
    
    For BREAK setup detection:
        - Price touches wall
        - Wall eroding (size decreasing)
        - Aggressive trades in wall direction
        - Entry: follow the break
    
    Usage:
        tracker = WallTracker(touch_threshold_bps=1.0)
        tracker.update(current_price, mid, bid_wall, ask_wall)
        if tracker.is_rejection("ask", min_ticks=2):
            # Fade short opportunity
    """
    
    def __init__(
        self,
        touch_threshold_bps: float = 1.0,
        rejection_threshold_bps: float = 0.5,
        max_touch_duration_ms: int = 10000,  # Forget old touches
    ):
        """
        Args:
            touch_threshold_bps: Price within this of wall = touch
            rejection_threshold_bps: Price must move at least this much for rejection tick
            max_touch_duration_ms: Reset touch tracking after this duration
        """
        self.touch_threshold_bps = touch_threshold_bps
        self.rejection_threshold_bps = rejection_threshold_bps
        self.max_touch_duration_ms = max_touch_duration_ms
        
        # Current touch states per side
        self._bid_touch: Optional[WallTouch] = None
        self._ask_touch: Optional[WallTouch] = None
        
        # Price history for rejection detection
        self._price_history: deque = deque(maxlen=20)
        self._last_update_ms: int = 0
    
    def update(
        self,
        current_price: float,
        mid: float,
        bid_wall: Optional[LiquidityWall],
        ask_wall: Optional[LiquidityWall],
        now_ms: Optional[int] = None,
    ) -> None:
        """
        Update wall touch tracking.
        
        Args:
            current_price: Current price (microprice or mid)
            mid: Midprice for bps calculations
            bid_wall: Current bid wall (or None)
            ask_wall: Current ask wall (or None)
            now_ms: Current timestamp
        """
        now = now_ms or int(time.time() * 1000)
        self._last_update_ms = now
        
        self._price_history.append(current_price)
        
        # Update bid wall touch
        self._bid_touch = self._update_touch(
            self._bid_touch,
            current_price,
            mid,
            bid_wall,
            "bid",
            now,
        )
        
        # Update ask wall touch
        self._ask_touch = self._update_touch(
            self._ask_touch,
            current_price,
            mid,
            ask_wall,
            "ask",
            now,
        )
    
    def _update_touch(
        self,
        current_touch: Optional[WallTouch],
        price: float,
        mid: float,
        wall: Optional[LiquidityWall],
        side: str,
        now_ms: int,
    ) -> Optional[WallTouch]:
        """Update touch state for one side."""
        
        # No wall = no touch
        if wall is None:
            return None
        
        # Calculate distance to wall
        distance = abs(price - wall.price)
        distance_bps = distance / mid * 10000 if mid > 0 else float('inf')
        
        is_touching = distance_bps <= self.touch_threshold_bps
        
        # If currently touching
        if is_touching:
            if current_touch is None:
                # New touch
                return WallTouch(
                    wall_price=wall.price,
                    wall_side=side,
                    touch_price=price,
                    initial_wall_size=wall.size,
                    current_wall_size=wall.size,
                    touch_time_ms=now_ms,
                )
            else:
                # Continue touch - update with history tracking
                current_touch.touch_count += 1
                current_touch.update_size_history(wall.size)
                current_touch.rejection_ticks = 0  # Reset rejection on re-touch
                return current_touch
        
        # Not touching
        if current_touch is None:
            return None
        
        # Check if touch is too old
        age_ms = now_ms - current_touch.touch_time_ms
        if age_ms > self.max_touch_duration_ms:
            return None
        
        # Price moved away - count as rejection tick?
        # For ask wall: rejection = price moving down (away from wall)
        # For bid wall: rejection = price moving up (away from wall)
        if side == "ask":
            is_rejecting = price < current_touch.touch_price - (mid * self.rejection_threshold_bps / 10000)
        else:  # bid
            is_rejecting = price > current_touch.touch_price + (mid * self.rejection_threshold_bps / 10000)
        
        if is_rejecting:
            current_touch.rejection_ticks += 1
            rejection_bps = abs(price - current_touch.touch_price) / mid * 10000
            current_touch.max_rejection_distance_bps = max(
                current_touch.max_rejection_distance_bps,
                rejection_bps
            )
        
        # Update wall size history if wall still exists
        current_touch.update_size_history(wall.size)
        
        return current_touch
    
    def get_touch(self, side: str) -> Optional[WallTouch]:
        """Get current touch state for a side."""
        return self._ask_touch if side == "ask" else self._bid_touch
    
    def is_rejection(
        self,
        side: str,
        min_rejection_ticks: int = 2,
        require_stable_wall: bool = True,
    ) -> bool:
        """
        Check if we have a valid rejection from a wall.
        
        Args:
            side: "bid" or "ask" - which wall
            min_rejection_ticks: Minimum ticks away from wall
            require_stable_wall: Wall must not be eroding
        
        Returns:
            True if valid rejection pattern
        """
        touch = self.get_touch(side)
        
        if touch is None:
            return False
        
        if touch.rejection_ticks < min_rejection_ticks:
            return False
        
        if require_stable_wall and touch.wall_eroding:
            return False
        
        return True
    
    def is_break(
        self,
        side: str,
        min_touch_ticks: int = 1,
    ) -> bool:
        """
        Check if wall is being broken (for continuation).
        
        Args:
            side: "bid" or "ask"
            min_touch_ticks: Minimum ticks at wall
        
        Returns:
            True if wall is eroding (being absorbed)
        """
        touch = self.get_touch(side)
        
        if touch is None:
            return False
        
        if touch.touch_count < min_touch_ticks:
            return False
        
        return touch.wall_eroding
    
    def get_last_price(self) -> Optional[float]:
        """Get most recent price."""
        return self._price_history[-1] if self._price_history else None
    
    def reset(self) -> None:
        """Reset all tracking state."""
        self._bid_touch = None
        self._ask_touch = None
        self._price_history.clear()
