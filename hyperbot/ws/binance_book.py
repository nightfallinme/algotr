# hyperbot/ws/binance_book.py
"""
Binance Local Orderbook - Maintains state from delta updates.

Binance depth stream sends deltas (only changes), not full snapshots.
This class maintains local orderbook state by applying deltas.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional
import time

from ..logger import get_logger

log = get_logger(__name__)


@dataclass
class BinanceLocalBook:
    """
    Maintains local orderbook from Binance delta updates.
    
    Usage:
        book = BinanceLocalBook(symbol="ETH", depth=20)
        book.apply_depth_update(bids, asks)
        levels = book.get_levels()  # Returns sorted (price, size) tuples
    """
    symbol: str
    max_depth: int = 20
    
    # Local state: price -> size
    _bids: Dict[float, float] = field(default_factory=dict)
    _asks: Dict[float, float] = field(default_factory=dict)
    
    # Metadata
    last_update_ms: int = 0
    update_count: int = 0
    
    def apply_depth_update(
        self,
        bids: List[Tuple[float, float]],
        asks: List[Tuple[float, float]],
        timestamp_ms: Optional[int] = None,
    ) -> None:
        """
        Apply Binance depth update.
        
        NOTE: Binance depth@20 stream sends FULL SNAPSHOT each time,
        not deltas! So we replace the entire book on each update.
        """
        now_ms = timestamp_ms or int(time.time() * 1000)
        self.last_update_ms = now_ms
        self.update_count += 1
        
        # FULL SNAPSHOT: Clear and replace entire book
        self._bids.clear()
        self._asks.clear()
        
        # Add all bid levels (skip size=0)
        for price, size in bids:
            if size > 0:
                self._bids[price] = size
        
        # Add all ask levels (skip size=0)
        for price, size in asks:
            if size > 0:
                self._asks[price] = size
        
        # Trim to max depth (just in case)
        self._trim_to_depth()
    
    def _trim_to_depth(self) -> None:
        """Keep only top N levels."""
        if len(self._bids) > self.max_depth:
            # Keep highest prices for bids
            sorted_bids = sorted(self._bids.keys(), reverse=True)
            for price in sorted_bids[self.max_depth:]:
                del self._bids[price]
        
        if len(self._asks) > self.max_depth:
            # Keep lowest prices for asks
            sorted_asks = sorted(self._asks.keys())
            for price in sorted_asks[self.max_depth:]:
                del self._asks[price]
    
    def get_bids(self) -> List[Tuple[float, float]]:
        """Get bids sorted by price descending."""
        return sorted(
            [(p, s) for p, s in self._bids.items()],
            key=lambda x: x[0],
            reverse=True
        )
    
    def get_asks(self) -> List[Tuple[float, float]]:
        """Get asks sorted by price ascending."""
        return sorted(
            [(p, s) for p, s in self._asks.items()],
            key=lambda x: x[0]
        )
    
    def get_levels(self) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
        """Get (bids, asks) as sorted tuples."""
        return self.get_bids(), self.get_asks()
    
    @property
    def best_bid(self) -> float:
        """Get best bid price."""
        if not self._bids:
            return 0.0
        return max(self._bids.keys())
    
    @property
    def best_ask(self) -> float:
        """Get best ask price."""
        if not self._asks:
            return 0.0
        return min(self._asks.keys())
    
    @property
    def mid(self) -> float:
        """Get mid price."""
        bb, ba = self.best_bid, self.best_ask
        if bb <= 0 or ba <= 0:
            return 0.0
        return (bb + ba) / 2
    
    @property
    def spread_bps(self) -> float:
        """Get spread in basis points."""
        mid = self.mid
        if mid <= 0:
            return 0.0
        return (self.best_ask - self.best_bid) / mid * 10000
    
    @property
    def is_valid(self) -> bool:
        """Check if book has valid data."""
        return len(self._bids) >= 5 and len(self._asks) >= 5
    
    def reset(self) -> None:
        """Clear all data."""
        self._bids.clear()
        self._asks.clear()
        self.update_count = 0
