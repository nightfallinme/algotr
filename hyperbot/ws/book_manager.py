# hyperbot/ws/book_manager.py
"""
L2 Order Book Manager with state integrity checks.

Features:
- Snapshot initialization (WS or REST)
- Delta updates with gap detection
- Invariant validation
- Automatic resync on corruption
- Warm-up gate
"""
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple, Callable, Any
from enum import Enum, auto
import time

from ..logger import get_logger

log = get_logger(__name__)


class BookState(Enum):
    """Book manager states."""
    UNINITIALIZED = auto()
    WARMING_UP = auto()
    READY = auto()
    RESYNC_REQUIRED = auto()


@dataclass
class TopOfBook:
    """Top-of-book snapshot."""
    best_bid: float = 0.0
    best_ask: float = 0.0
    bid_size: float = 0.0
    ask_size: float = 0.0
    timestamp_ms: int = 0
    
    # Derived values 
    @property
    def spread(self) -> float:
        if self.best_ask > 0 and self.best_bid > 0:
            return self.best_ask - self.best_bid
        return 0.0
    
    @property
    def mid(self) -> float:
        if self.best_ask > 0 and self.best_bid > 0:
            return (self.best_ask + self.best_bid) / 2.0
        return 0.0
    
    @property
    def microprice(self) -> float:
        """Size-weighted mid price."""
        total_size = self.bid_size + self.ask_size
        if total_size > 0 and self.best_ask > 0 and self.best_bid > 0:
            return (self.best_ask * self.bid_size + self.best_bid * self.ask_size) / total_size
        return self.mid
    
    def is_valid(self) -> bool:
        """Check invariants."""
        if self.best_bid <= 0 or self.best_ask <= 0:
            return False
        if self.best_ask <= self.best_bid:
            return False  # Crossed/negative spread
        if self.bid_size < 0 or self.ask_size < 0:
            return False
        return True


@dataclass
class BookDelta:
    """Change in top-of-book sizes since last update."""
    delta_bid_size: float = 0.0
    delta_ask_size: float = 0.0
    timestamp_ms: int = 0


class BookManager:
    """
    Manages L2 order book state with integrity guarantees.
    
    Usage:
        book = BookManager(symbol="ETH", warm_up_ticks=50)
        book.apply_snapshot(snapshot_data)  # Initialize
        book.apply_delta(delta_data)        # Update
        if book.is_ready:
            tob = book.top_of_book          # Safe to use
    """
    
    def __init__(
        self,
        symbol: str,
        warm_up_ticks: int = 50,
        warm_up_seconds: float = 3.0,
        on_resync_required: Optional[Callable[[], None]] = None,
    ):
        self.symbol = symbol
        self._warm_up_ticks = warm_up_ticks
        self._warm_up_seconds = warm_up_seconds
        self._on_resync_required = on_resync_required
        
        # State
        self._state = BookState.UNINITIALIZED
        self._tob = TopOfBook()
        self._prev_tob = TopOfBook()
        
        # Tracking
        self._last_seq: Optional[int] = None
        self._last_update_ms: int = 0
        self._update_count: int = 0
        self._init_time_ms: int = 0
        
        # L2 levels (optional, for deeper book)
        self._bids: Dict[float, float] = {}  # price -> size
        self._asks: Dict[float, float] = {}  # price -> size
    
    @property
    def state(self) -> BookState:
        return self._state
    
    @property
    def is_ready(self) -> bool:
        """True if book is valid and warm-up complete."""
        return self._state == BookState.READY
    
    @property
    def top_of_book(self) -> TopOfBook:
        """Current top-of-book state."""
        return self._tob
    
    @property
    def last_delta(self) -> BookDelta:
        """Delta from previous update."""
        return BookDelta(
            delta_bid_size=self._tob.bid_size - self._prev_tob.bid_size,
            delta_ask_size=self._tob.ask_size - self._prev_tob.ask_size,
            timestamp_ms=self._tob.timestamp_ms,
        )
    
    def apply_snapshot(self, data: Dict[str, Any]) -> bool:
        """
        Initialize book from snapshot.
        
        Expected data format (Hyperliquid L2):
        {
            "coin": "ETH",
            "levels": [
                [[price, size], ...],  # bids
                [[price, size], ...],  # asks
            ],
            "time": 1234567890123  # optional
        }
        
        Returns: True if snapshot applied successfully
        """
        try:
            levels = data.get("levels", [])
            if len(levels) < 2:
                log.warning(f"[{self.symbol}] Invalid snapshot: missing levels")
                return False
            
            bids_raw = levels[0]
            asks_raw = levels[1]
            
            # Clear existing state
            self._bids.clear()
            self._asks.clear()
            
            # Parse bids
            for level in bids_raw:
                price, size = float(level["px"]), float(level["sz"])
                if size > 0:
                    self._bids[price] = size
            
            # Parse asks
            for level in asks_raw:
                price, size = float(level["px"]), float(level["sz"])
                if size > 0:
                    self._asks[price] = size
            
            # Update top-of-book
            self._update_tob(data.get("time", int(time.time() * 1000)))
            
            # Validate
            if not self._tob.is_valid():
                log.warning(f"[{self.symbol}] Snapshot failed validation")
                self._state = BookState.RESYNC_REQUIRED
                return False
            
            # Initialize tracking (only on first snapshot)
            self._last_seq = data.get("seq")
            now_ms = int(time.time() * 1000)
            
            if self._state == BookState.UNINITIALIZED:
                self._init_time_ms = now_ms
                self._update_count = 0
                self._state = BookState.WARMING_UP
            
            # Increment update count (each snapshot counts as an update)
            self._update_count += 1
            
            # Check warm-up completion
            self._check_warm_up(now_ms)
            
            # log.debug(f"[{self.symbol}] Snapshot applied: bid={self._tob.best_bid:.2f} ask={self._tob.best_ask:.2f}")
            return True
            
        except Exception as e:
            log.error(f"[{self.symbol}] Snapshot error: {e}")
            self._state = BookState.RESYNC_REQUIRED
            return False
    
    def apply_delta(self, data: Dict[str, Any]) -> bool:
        """
        Apply delta update to book.
        
        Handles both sequence-based and invariant-based validation.
        
        Returns: True if update applied successfully
        """
        if self._state == BookState.UNINITIALIZED:
            log.debug(f"[{self.symbol}] Delta ignored: waiting for snapshot")
            return False
        
        try:
            now_ms = int(time.time() * 1000)
            msg_time = data.get("time", now_ms)
            msg_seq = data.get("seq")
            
            # === Sequence-based gap detection ===
            if msg_seq is not None and self._last_seq is not None:
                if msg_seq != self._last_seq + 1:
                    log.warning(f"[{self.symbol}] Sequence gap: expected {self._last_seq + 1}, got {msg_seq}")
                    self._trigger_resync("SEQUENCE_GAP")
                    return False
                self._last_seq = msg_seq
            
            # === Apply updates ===
            levels = data.get("levels", [])
            if len(levels) >= 2:
                # Full level update
                for level in levels[0]:  # bids
                    price, size = float(level["px"]), float(level["sz"])
                    if size > 0:
                        self._bids[price] = size
                    elif price in self._bids:
                        del self._bids[price]
                
                for level in levels[1]:  # asks
                    price, size = float(level["px"]), float(level["sz"])
                    if size > 0:
                        self._asks[price] = size
                    elif price in self._asks:
                        del self._asks[price]
            
            # Update top-of-book
            self._update_tob(msg_time)
            
            # === Invariant validation ===
            if not self._tob.is_valid():
                self._trigger_resync("INVARIANT_VIOLATION")
                return False
            
            # Monotonicity check (time should not go backwards significantly)
            if self._last_update_ms > 0 and msg_time < self._last_update_ms - 1000:
                log.warning(f"[{self.symbol}] Time went backwards: {msg_time} < {self._last_update_ms}")
                # Not fatal, but log it
            
            self._last_update_ms = msg_time
            self._update_count += 1
            
            # Check warm-up completion
            self._check_warm_up(now_ms)
            
            return True
            
        except Exception as e:
            log.error(f"[{self.symbol}] Delta error: {e}")
            self._trigger_resync("DELTA_ERROR")
            return False
    
    def _update_tob(self, timestamp_ms: int) -> None:
        """Update top-of-book from current levels."""
        # Store previous for delta calculation
        self._prev_tob = TopOfBook(
            best_bid=self._tob.best_bid,
            best_ask=self._tob.best_ask,
            bid_size=self._tob.bid_size,
            ask_size=self._tob.ask_size,
            timestamp_ms=self._tob.timestamp_ms,
        )
        
        # Find best bid (highest)
        if self._bids:
            best_bid_price = max(self._bids.keys())
            self._tob.best_bid = best_bid_price
            self._tob.bid_size = self._bids[best_bid_price]
        else:
            self._tob.best_bid = 0.0
            self._tob.bid_size = 0.0
        
        # Find best ask (lowest)
        if self._asks:
            best_ask_price = min(self._asks.keys())
            self._tob.best_ask = best_ask_price
            self._tob.ask_size = self._asks[best_ask_price]
        else:
            self._tob.best_ask = 0.0
            self._tob.ask_size = 0.0
        
        self._tob.timestamp_ms = timestamp_ms
    
    def _check_warm_up(self, now_ms: int) -> None:
        """Check if warm-up is complete."""
        if self._state != BookState.WARMING_UP:
            return
        
        elapsed_ms = now_ms - self._init_time_ms
        elapsed_sec = elapsed_ms / 1000.0
        
        if self._update_count >= self._warm_up_ticks and elapsed_sec >= self._warm_up_seconds:
            self._state = BookState.READY
            log.info(f"[{self.symbol}] Book ready: {self._update_count} updates in {elapsed_sec:.1f}s")
    
    def _trigger_resync(self, reason: str) -> None:
        """Trigger resync and notify callback."""
        log.warning(f"[{self.symbol}] Resync required: {reason}")
        self._state = BookState.RESYNC_REQUIRED
        
        if self._on_resync_required:
            try:
                self._on_resync_required()
            except Exception as e:
                log.error(f"[{self.symbol}] Resync callback error: {e}")
    
    def reset(self) -> None:
        """Reset book state for resync."""
        self._state = BookState.UNINITIALIZED
        self._tob = TopOfBook()
        self._prev_tob = TopOfBook()
        self._bids.clear()
        self._asks.clear()
        self._last_seq = None
        self._update_count = 0
        log.info(f"[{self.symbol}] Book reset")
    
    def force_ready(self) -> None:
        """Force book to ready state (for testing)."""
        if self._tob.is_valid():
            self._state = BookState.READY
