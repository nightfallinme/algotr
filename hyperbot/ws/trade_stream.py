# hyperbot/ws/trade_stream.py
"""
Trade Stream Manager for real-time trade aggregation.

Features:
- Volume normalization (base units)
- Aggressor side resolution
- Rolling window sums for buy/sell volume
"""
from dataclasses import dataclass
from typing import Optional, Dict, Any, Literal
from enum import Enum, auto
import time

from .data_structures import TimeWindowSum, RingBuffer
from ..logger import get_logger

log = get_logger(__name__)


class AggressorSide(Enum):
    """Trade aggressor side."""
    BUY = "buy"     # Buyer initiated (lifted ask)
    SELL = "sell"   # Seller initiated (hit bid)
    UNKNOWN = "unknown"


@dataclass
class NormalizedTrade:
    """Normalized trade data."""
    timestamp_ms: int
    side: AggressorSide
    price: float
    size_base: float  # Always in base units
    is_buyer_maker: bool = False


class TradeStreamManager:
    """
    Manages real-time trade stream with volume aggregation.
    
    Usage:
        stream = TradeStreamManager(symbol="ETH", window_sec=2.0)
        stream.on_trade(trade_data, book_bid, book_ask)
        
        buy_vol = stream.buy_volume.sum(now_ms)
        sell_vol = stream.sell_volume.sum(now_ms)
        ofi_trade = buy_vol - sell_vol
    """
    
    def __init__(
        self,
        symbol: str,
        window_sec: float = 2.0,
        volume_unit: str = "base",  # "base" or "quote"
        min_trades_ready: int = 5,
    ):
        self.symbol = symbol
        self._window_sec = window_sec
        self._volume_unit = volume_unit
        self._min_trades_ready = min_trades_ready
        
        # Rolling sums
        self.buy_volume = TimeWindowSum(window_sec)
        self.sell_volume = TimeWindowSum(window_sec)
        self.trade_count_window = TimeWindowSum(window_sec)  # Track trade count in window
        
        # Price tracking for unit conversion
        self._last_price: float = 0.0
        
        # Stats
        self._trade_count: int = 0
        self._unknown_side_count: int = 0
    
    @property
    def is_ready(self) -> bool:
        """True if we have enough trades."""
        return self._trade_count >= self._min_trades_ready
    
    def on_trade(
        self,
        data: Dict[str, Any],
        book_bid: Optional[float] = None,
        book_ask: Optional[float] = None,
    ) -> Optional[NormalizedTrade]:
        """
        Process incoming trade message.
        
        Expected Hyperliquid format:
        {
            "coin": "ETH",
            "side": "A" or "B",  # A=Ask (buy aggressor), B=Bid (sell aggressor)  
            "px": "1234.5",
            "sz": "0.1",
            "time": 1234567890123,
            "hash": "0x..."
        }
        
        Returns: Normalized trade or None if invalid
        """
        try:
            coin = data.get("coin", "")
            if coin and coin != self.symbol:
                return None
            
            price = float(data.get("px", 0))
            size = float(data.get("sz", 0))
            timestamp_ms = int(data.get("time", time.time() * 1000))
            
            if price <= 0 or size <= 0:
                return None
            
            # Update price tracking
            self._last_price = price
            
            # Normalize size to base units
            size_base = self._normalize_size(size, price)
            
            # Resolve aggressor side
            side = self._resolve_side(data, price, book_bid, book_ask)
            
            # Create normalized trade
            trade = NormalizedTrade(
                timestamp_ms=timestamp_ms,
                side=side,
                price=price,
                size_base=size_base,
            )
            
            # Add to rolling sums
            if side == AggressorSide.BUY:
                self.buy_volume.add(timestamp_ms, size_base)
            elif side == AggressorSide.SELL:
                self.sell_volume.add(timestamp_ms, size_base)
            else:
                self._unknown_side_count += 1
            
            # Count trades in window
            self.trade_count_window.add(timestamp_ms, 1.0)
            self._trade_count += 1
            return trade
            
        except Exception as e:
            log.debug(f"[{self.symbol}] Trade parse error: {e}")
            return None
    
    def _normalize_size(self, size: float, price: float) -> float:
        """
        Convert size to base units if needed.
        
        If exchange sends quote-sized trades, convert: base = quote / price
        """
        if self._volume_unit == "quote" and price > 0:
            return size / price
        return size  # Already base
    
    def _resolve_side(
        self,
        data: Dict[str, Any],
        price: float,
        book_bid: Optional[float],
        book_ask: Optional[float],
    ) -> AggressorSide:
        """
        Resolve aggressor side from trade data.
        
        Priority:
        1. Explicit side field from exchange
        2. Derive from price vs book (price >= ask = buy, price <= bid = sell)
        """
        # Try explicit side (Hyperliquid format)
        raw_side = data.get("side", "").upper()
        if raw_side == "A":  # Ask side = buy aggressor
            return AggressorSide.BUY
        elif raw_side == "B":  # Bid side = sell aggressor
            return AggressorSide.SELL
        
        # Try is_buyer_maker format (some exchanges)
        if "is_buyer_maker" in data:
            # If buyer is maker, seller is aggressor -> SELL
            return AggressorSide.SELL if data["is_buyer_maker"] else AggressorSide.BUY
        
        # Derive from price vs book
        if book_ask is not None and price >= book_ask:
            return AggressorSide.BUY
        if book_bid is not None and price <= book_bid:
            return AggressorSide.SELL
        
        # Cannot determine
        return AggressorSide.UNKNOWN
    
    def get_ofi_trade(self, now_ms: Optional[int] = None) -> float:
        """
        Get trade-based OFI: buy_volume - sell_volume.
        
        Returns 0.0 if not ready.
        """
        if not self.is_ready:
            return 0.0
        
        now = now_ms or int(time.time() * 1000)
        return self.buy_volume.sum(now) - self.sell_volume.sum(now)
    
    def get_total_volume(self, now_ms: Optional[int] = None) -> float:
        """Get total volume in window."""
        now = now_ms or int(time.time() * 1000)
        return self.buy_volume.sum(now) + self.sell_volume.sum(now)
    
    def get_buy_ratio(self, now_ms: Optional[int] = None) -> float:
        """Get ratio of buy volume to total volume (0.0 to 1.0)."""
        now = now_ms or int(time.time() * 1000)
        buy = self.buy_volume.sum(now)
        sell = self.sell_volume.sum(now)
        total = buy + sell
        if total <= 1e-9:
            return 0.5  # Neutral if no volume
        return buy / total
    
    def get_trade_count(self, now_ms: Optional[int] = None) -> int:
        """Get number of trades in window."""
        now = now_ms or int(time.time() * 1000)
        return int(self.trade_count_window.sum(now))
    
    def clear(self) -> None:
        """Clear all state."""
        self.buy_volume.clear()
        self.sell_volume.clear()
        self.trade_count_window.clear()
        self._trade_count = 0
        self._unknown_side_count = 0
    
    def get_stats(self) -> Dict[str, Any]:
        """Get stream statistics."""
        return {
            "trade_count": self._trade_count,
            "unknown_side_count": self._unknown_side_count,
            "is_ready": self.is_ready,
        }
