# hyperbot/vacuum/liquidity_map.py
"""
L2 Order Book Liquidity Mapper.

Analyzes L2 book depth to:
- Aggregate liquidity in bps-based buckets
- Detect liquidity walls (high-volume clusters)
- Compute depth profiles for bid/ask sides

Mathematical Definitions:
    mid = (best_bid + best_ask) / 2
    distance_bps(price) = abs(price - mid) / mid * 10000
    
    Liquidity Wall:
        size >= median_depth * wall_min_size_mult
        distance_bps <= wall_max_distance_bps
        persists for >= wall_stability_ticks consecutive updates
"""
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict
from collections import deque
import time

from ..logger import get_logger

log = get_logger(__name__)


@dataclass
class LiquidityLevel:
    """Single price level with liquidity metrics."""
    price: float
    size: float
    cum_size: float  # Cumulative size from BBO
    distance_bps: float  # Distance from mid in basis points


@dataclass
class LiquidityWall:
    """Detected liquidity wall."""
    price: float
    size: float
    side: str  # "bid" | "ask"
    distance_bps: float
    stability_ticks: int = 0  # Consecutive ticks this wall existed
    first_seen_ms: int = 0
    
    def is_stable(self, min_ticks: int = 2) -> bool:
        """True if wall has persisted for minimum ticks."""
        return self.stability_ticks >= min_ticks


@dataclass
class DepthProfile:
    """Aggregated depth within bps ranges."""
    total_size: float = 0.0
    level_count: int = 0
    levels: List[LiquidityLevel] = field(default_factory=list)
    
    @property
    def avg_size(self) -> float:
        """Average size per level."""
        return self.total_size / self.level_count if self.level_count > 0 else 0.0


class LiquidityMap:
    """
    Maps L2 orderbook into liquidity landscape.
    
    Usage:
        liq_map = LiquidityMap(wall_min_size_mult=3.0)
        liq_map.update(bids, asks, mid)
        bid_wall, ask_wall = liq_map.get_walls()
    """
    
    def __init__(
        self,
        wall_min_size_mult: float = 3.0,
        wall_max_distance_bps: float = 20.0,
        wall_stability_ticks: int = 2,
        depth_levels: int = 20,  # How many L2 levels to analyze
    ):
        """
        Args:
            wall_min_size_mult: Wall size must be >= median * this
            wall_max_distance_bps: Only detect walls within this distance
            wall_stability_ticks: Ticks wall must persist to be valid
            depth_levels: Number of book levels to process
        """
        self.wall_min_size_mult = wall_min_size_mult
        self.wall_max_distance_bps = wall_max_distance_bps
        self.wall_stability_ticks = wall_stability_ticks
        self.depth_levels = depth_levels
        
        # Current state
        self._bid_levels: List[LiquidityLevel] = []
        self._ask_levels: List[LiquidityLevel] = []
        self._mid: float = 0.0
        self._last_update_ms: int = 0
        
        # Wall tracking (for stability)
        self._last_bid_wall: Optional[LiquidityWall] = None
        self._last_ask_wall: Optional[LiquidityWall] = None
        
        # Median depth calculation (rolling)
        self._size_history: deque = deque(maxlen=200)
        self._median_cache: float = 0.0
        self._median_dirty: bool = True
    
    @property
    def is_ready(self) -> bool:
        """True if map has enough data for calculations."""
        return len(self._bid_levels) > 0 and len(self._ask_levels) > 0 and self._mid > 0
    
    def update(
        self,
        bids: List[Tuple[float, float]],  # [(price, size), ...]
        asks: List[Tuple[float, float]],
        mid: Optional[float] = None,
        now_ms: Optional[int] = None,
    ) -> None:
        """
        Update liquidity map with new book state.
        
        Args:
            bids: List of (price, size) tuples, sorted descending by price
            asks: List of (price, size) tuples, sorted ascending by price
            mid: Midprice (computed if not provided)
            now_ms: Current timestamp
        """
        now = now_ms or int(time.time() * 1000)
        
        # Compute mid if not provided
        if mid is None and bids and asks:
            mid = (bids[0][0] + asks[0][0]) / 2
        
        if mid is None or mid <= 0:
            return
        
        self._mid = mid
        self._last_update_ms = now
        
        # Process bids
        self._bid_levels = self._process_levels(bids, mid, "bid")
        
        # Process asks
        self._ask_levels = self._process_levels(asks, mid, "ask")
        
        # Update size history for median calculation
        all_sizes = [lvl.size for lvl in self._bid_levels + self._ask_levels if lvl.size > 0]
        if all_sizes:
            self._size_history.extend(all_sizes)
            self._median_dirty = True
        
        # Update wall stability
        self._update_wall_tracking(now)
    
    def _process_levels(
        self,
        levels: List[Tuple[float, float]],
        mid: float,
        side: str,
    ) -> List[LiquidityLevel]:
        """Convert raw levels to LiquidityLevel objects."""
        result = []
        cum_size = 0.0
        
        for i, (price, size) in enumerate(levels[:self.depth_levels]):
            if price <= 0 or size < 0:
                continue
            
            cum_size += size
            distance_bps = abs(price - mid) / mid * 10000
            
            result.append(LiquidityLevel(
                price=price,
                size=size,
                cum_size=cum_size,
                distance_bps=distance_bps,
            ))
        
        return result
    
    def _update_wall_tracking(self, now_ms: int) -> None:
        """Track wall stability across updates."""
        median = self.get_median_depth()
        if median <= 0:
            return
        
        threshold = median * self.wall_min_size_mult
        
        # Find current bid wall
        current_bid_wall = self._find_wall(self._bid_levels, "bid", threshold)
        
        # Check stability vs previous
        if current_bid_wall and self._last_bid_wall:
            # Same wall if price matches
            if abs(current_bid_wall.price - self._last_bid_wall.price) < 0.01:
                current_bid_wall.stability_ticks = self._last_bid_wall.stability_ticks + 1
                current_bid_wall.first_seen_ms = self._last_bid_wall.first_seen_ms
            else:
                current_bid_wall.first_seen_ms = now_ms
        elif current_bid_wall:
            current_bid_wall.first_seen_ms = now_ms
        
        self._last_bid_wall = current_bid_wall
        
        # Find current ask wall
        current_ask_wall = self._find_wall(self._ask_levels, "ask", threshold)
        
        if current_ask_wall and self._last_ask_wall:
            if abs(current_ask_wall.price - self._last_ask_wall.price) < 0.01:
                current_ask_wall.stability_ticks = self._last_ask_wall.stability_ticks + 1
                current_ask_wall.first_seen_ms = self._last_ask_wall.first_seen_ms
            else:
                current_ask_wall.first_seen_ms = now_ms
        elif current_ask_wall:
            current_ask_wall.first_seen_ms = now_ms
        
        self._last_ask_wall = current_ask_wall
    
    def _find_wall(
        self,
        levels: List[LiquidityLevel],
        side: str,
        threshold: float,
    ) -> Optional[LiquidityWall]:
        """Find largest wall within distance limit."""
        best: Optional[LiquidityWall] = None
        
        for lvl in levels:
            if lvl.distance_bps > self.wall_max_distance_bps:
                break  # Levels are sorted by distance
            
            if lvl.size >= threshold:
                if best is None or lvl.size > best.size:
                    best = LiquidityWall(
                        price=lvl.price,
                        size=lvl.size,
                        side=side,
                        distance_bps=lvl.distance_bps,
                    )
        
        return best
    
    def get_walls(
        self,
        require_stable: bool = True,
    ) -> Tuple[Optional[LiquidityWall], Optional[LiquidityWall]]:
        """
        Get current bid and ask walls.
        
        Args:
            require_stable: Only return walls with sufficient stability
            
        Returns:
            (bid_wall, ask_wall) - either can be None
        """
        bid_wall = self._last_bid_wall
        ask_wall = self._last_ask_wall
        
        if require_stable:
            if bid_wall and not bid_wall.is_stable(self.wall_stability_ticks):
                bid_wall = None
            if ask_wall and not ask_wall.is_stable(self.wall_stability_ticks):
                ask_wall = None
        
        return bid_wall, ask_wall
    
    def get_depth_within_bps(self, side: str, bps: float) -> float:
        """
        Get total liquidity within specified bps from mid.
        
        Args:
            side: "bid" or "ask"
            bps: Maximum distance in basis points
            
        Returns:
            Total size within range
        """
        levels = self._bid_levels if side == "bid" else self._ask_levels
        
        total = 0.0
        for lvl in levels:
            if lvl.distance_bps <= bps:
                total += lvl.size
            else:
                break
        
        return total
    
    def get_depth_profile(self, side: str, max_bps: float = 20.0) -> DepthProfile:
        """Get full depth profile for a side."""
        levels = self._bid_levels if side == "bid" else self._ask_levels
        
        filtered = [lvl for lvl in levels if lvl.distance_bps <= max_bps]
        
        return DepthProfile(
            total_size=sum(lvl.size for lvl in filtered),
            level_count=len(filtered),
            levels=filtered,
        )
    
    def get_median_depth(self) -> float:
        """Get median level size (rolling)."""
        if self._median_dirty and self._size_history:
            sorted_sizes = sorted(self._size_history)
            mid_idx = len(sorted_sizes) // 2
            self._median_cache = sorted_sizes[mid_idx]
            self._median_dirty = False
        return self._median_cache
    
    @property
    def mid(self) -> float:
        """Current midprice."""
        return self._mid
    
    @property
    def bid_levels(self) -> List[LiquidityLevel]:
        """Current bid levels."""
        return self._bid_levels
    
    @property
    def ask_levels(self) -> List[LiquidityLevel]:
        """Current ask levels."""
        return self._ask_levels
    
    @property
    def is_ready(self) -> bool:
        """True if map has enough data."""
        return len(self._size_history) >= 20 and self._mid > 0
    
    def reset(self) -> None:
        """Reset all state."""
        self._bid_levels.clear()
        self._ask_levels.clear()
        self._mid = 0.0
        self._last_bid_wall = None
        self._last_ask_wall = None
        self._size_history.clear()
        self._median_dirty = True
        self._median_cache = 0.0
