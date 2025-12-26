# hyperbot/vacuum/vacuum_detector.py
"""
Liquidity Vacuum Detector.

Identifies thin liquidity zones between current price and nearest wall.

Mathematical Definition:
    vacuum_zone: [price, wall_price]
    vacuum_bps = |wall_price - price| / mid * 10000
    thin_liquidity = sum(size) in vacuum_zone
    
    Vacuum is valid if:
        vacuum_bps >= min_vacuum_bps
        thin_liquidity <= median_depth * vacuum_thin_mult
"""
from dataclasses import dataclass
from typing import Optional

from .liquidity_map import LiquidityMap, LiquidityWall
from ..ws.book_manager import TopOfBook
from ..logger import get_logger

log = get_logger(__name__)


@dataclass
class VacuumSignal:
    """Detected liquidity vacuum."""
    side: str  # "bid" | "ask" - which side has vacuum (entry direction)
    vacuum_bps: float  # Size of vacuum in basis points
    wall_price: float  # Price of nearest wall
    wall_size: float  # Size at wall
    thin_liquidity: float  # Total liquidity in vacuum zone
    median_depth: float  # Reference median depth
    is_valid: bool = False  # True if vacuum meets criteria
    
    @property
    def vacuum_quality(self) -> float:
        """
        Quality metric: how thin is the vacuum relative to median.
        Lower = better vacuum (less liquidity to absorb).
        """
        if self.median_depth <= 0:
            return float('inf')
        return self.thin_liquidity / self.median_depth
    
    @property
    def quality_score(self) -> float:
        """
        Combined quality score: higher = better vacuum opportunity.
        Combines vacuum width (bps) with thinness ratio.
        """
        if self.median_depth <= 0 or not self.is_valid:
            return 0.0
        # Thinness: 1.0 = no liquidity, 0.0 = median liquidity
        thinness = max(0.0, 1.0 - (self.thin_liquidity / self.median_depth))
        # Score = vacuum width * thinness factor
        return self.vacuum_bps * thinness
    
    def to_dict(self) -> dict:
        return {
            "side": self.side,
            "vacuum_bps": round(self.vacuum_bps, 2),
            "wall_price": self.wall_price,
            "wall_size": round(self.wall_size, 4),
            "thin_liquidity": round(self.thin_liquidity, 4),
            "quality": round(self.vacuum_quality, 2),
            "quality_score": round(self.quality_score, 2),
            "valid": self.is_valid,
        }


class VacuumDetector:
    """
    Detects liquidity vacuums - thin zones price can move through quickly.
    
    For LONG setup (ask-side vacuum):
        - Wall above current price
        - Little liquidity between price and wall
        - Entry: buy expecting quick move to wall
    
    For SHORT setup (bid-side vacuum):
        - Wall below current price
        - Little liquidity between price and wall
        - Entry: sell expecting quick move to wall
    
    Usage:
        detector = VacuumDetector(min_vacuum_bps=1.5)
        signal = detector.detect(liquidity_map, top_of_book)
        if signal and signal.is_valid:
            # Trade opportunity
    """
    
    def __init__(
        self,
        min_vacuum_bps: float = 1.5,
        vacuum_thin_mult: float = 0.3,  # Vacuum zone has < median * this
        max_vacuum_bps: float = 15.0,   # Don't chase too far
    ):
        """
        Args:
            min_vacuum_bps: Minimum vacuum size to be valid
            vacuum_thin_mult: Maximum liquidity in vacuum (as mult of median)
            max_vacuum_bps: Maximum vacuum size (reject if too large)
        """
        self.min_vacuum_bps = min_vacuum_bps
        self.vacuum_thin_mult = vacuum_thin_mult
        self.max_vacuum_bps = max_vacuum_bps
    
    def detect_ask_vacuum(
        self,
        liquidity_map: LiquidityMap,
        tob: TopOfBook,
    ) -> Optional[VacuumSignal]:
        """
        Detect vacuum on ask side (for LONG entry).
        
        Vacuum exists if:
        - Ask wall detected above current price
        - Thin liquidity between best_ask and wall
        """
        _, ask_wall = liquidity_map.get_walls(require_stable=True)
        
        if ask_wall is None:
            return None
        
        mid = liquidity_map.mid
        if mid <= 0:
            return None
        
        # Calculate vacuum size
        vacuum_bps = (ask_wall.price - tob.best_ask) / mid * 10000
        
        if vacuum_bps < self.min_vacuum_bps:
            return None
        
        if vacuum_bps > self.max_vacuum_bps:
            return None
        
        # Calculate liquidity in vacuum zone
        thin_liquidity = self._calculate_zone_liquidity(
            liquidity_map.ask_levels,
            tob.best_ask,
            ask_wall.price,
        )
        
        median = liquidity_map.get_median_depth()
        threshold = median * self.vacuum_thin_mult * (vacuum_bps / self.min_vacuum_bps)
        
        is_valid = thin_liquidity <= threshold and median > 0
        
        return VacuumSignal(
            side="ask",
            vacuum_bps=vacuum_bps,
            wall_price=ask_wall.price,
            wall_size=ask_wall.size,
            thin_liquidity=thin_liquidity,
            median_depth=median,
            is_valid=is_valid,
        )
    
    def detect_bid_vacuum(
        self,
        liquidity_map: LiquidityMap,
        tob: TopOfBook,
    ) -> Optional[VacuumSignal]:
        """
        Detect vacuum on bid side (for SHORT entry).
        
        Vacuum exists if:
        - Bid wall detected below current price
        - Thin liquidity between best_bid and wall
        """
        bid_wall, _ = liquidity_map.get_walls(require_stable=True)
        
        if bid_wall is None:
            return None
        
        mid = liquidity_map.mid
        if mid <= 0:
            return None
        
        # Calculate vacuum size
        vacuum_bps = (tob.best_bid - bid_wall.price) / mid * 10000
        
        if vacuum_bps < self.min_vacuum_bps:
            return None
        
        if vacuum_bps > self.max_vacuum_bps:
            return None
        
        # Calculate liquidity in vacuum zone
        thin_liquidity = self._calculate_zone_liquidity(
            liquidity_map.bid_levels,
            bid_wall.price,
            tob.best_bid,
        )
        
        median = liquidity_map.get_median_depth()
        threshold = median * self.vacuum_thin_mult * (vacuum_bps / self.min_vacuum_bps)
        
        is_valid = thin_liquidity <= threshold and median > 0
        
        return VacuumSignal(
            side="bid",
            vacuum_bps=vacuum_bps,
            wall_price=bid_wall.price,
            wall_size=bid_wall.size,
            thin_liquidity=thin_liquidity,
            median_depth=median,
            is_valid=is_valid,
        )
    
    def detect(
        self,
        liquidity_map: LiquidityMap,
        tob: TopOfBook,
    ) -> Optional[VacuumSignal]:
        """
        Detect best vacuum opportunity.
        
        Returns the vacuum with better quality (thinner liquidity).
        Returns None if no valid vacuum exists.
        """
        ask_vacuum = self.detect_ask_vacuum(liquidity_map, tob)
        bid_vacuum = self.detect_bid_vacuum(liquidity_map, tob)
        
        # Filter valid only
        valid_vacuums = [v for v in [ask_vacuum, bid_vacuum] if v and v.is_valid]
        
        if not valid_vacuums:
            return None
        
        if len(valid_vacuums) == 1:
            return valid_vacuums[0]
        
        # Return the one with better quality (lower = thinner = better)
        return min(valid_vacuums, key=lambda v: v.vacuum_quality)
    
    def _calculate_zone_liquidity(
        self,
        levels: list,
        price_start: float,
        price_end: float,
    ) -> float:
        """Calculate total liquidity between two prices."""
        total = 0.0
        min_price = min(price_start, price_end)
        max_price = max(price_start, price_end)
        
        for lvl in levels:
            if min_price <= lvl.price <= max_price:
                total += lvl.size
        
        return total
