# hyperbot/ofi/coin_state.py
"""
Per-coin state container for OFI scalping.

Centralizes all state needed for signal generation and execution.
"""
from dataclasses import dataclass, field
from datetime import datetime
from collections import deque
from typing import Optional, Dict, Any, Deque, Tuple

from ..ws.data_structures import RingBuffer, TimeWindowSum, EMACalculator
from ..ws.book_manager import BookManager, BookState
from ..ws.trade_stream import TradeStreamManager


@dataclass
class DecisionSnapshot:
    """
    Snapshot of state at decision time.
    Used for consistent latency/slippage measurements.
    """
    timestamp_ms: int = 0
    best_bid: float = 0.0
    best_ask: float = 0.0
    bid_size: float = 0.0
    ask_size: float = 0.0
    microprice: float = 0.0
    spread: float = 0.0
    ofi_norm: float = 0.0
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.timestamp_ms,
            "bid": self.best_bid,
            "ask": self.best_ask,
            "microprice": self.microprice,
            "spread": self.spread,
            "ofi": self.ofi_norm,
        }


@dataclass  
class CoinState:
    """
    Complete state for a single coin.
    
    Contains:
    - Book manager (L2 state)
    - Trade stream (buy/sell volumes)
    - Feature buffers (microprice, spread, OFI history)
    - Position state
    - Execution locks
    """
    symbol: str
    
    # === Data Managers ===
    book: BookManager = field(default=None)
    trades: TradeStreamManager = field(default=None)
    
    # === Feature Buffers ===
    microprice_buf: RingBuffer = field(default=None)  # For volatility
    spread_buf: RingBuffer = field(default=None)      # For spread filter
    ofi_buf: RingBuffer = field(default=None)         # For OFI persistence
    
    # === Advanced Filter History ===
    # Stores (timestamp_ms, value) tuples
    ofi_history: Deque[Tuple[int, float]] = field(default_factory=deque)
    price_history: Deque[Tuple[int, float]] = field(default_factory=deque)
    
    # Stop-run cooling
    stoprun_cooldown_until_ms: int = 0
    
    # === Book OFI Windows ===
    book_bid_deltas: TimeWindowSum = field(default=None)
    book_ask_deltas: TimeWindowSum = field(default=None)
    
    # === EMA Calculators ===
    micro_atr_ema: EMACalculator = field(default=None)  # EMA of |delta microprice|
    
    # === Position State ===
    position_qty: float = 0.0  # Signed: positive=long, negative=short
    avg_entry: float = 0.0
    entry_time_ms: int = 0
    
    # === Execution Locks ===
    entry_inflight: bool = False
    exit_inflight: bool = False
    active_exit_signal: bool = False
    cooldown_until_ms: int = 0
    
    # === System State ===
    kill_switch: bool = False
    last_decision_ms: int = 0
    last_microprice: float = 0.0
    last_atr_update_ms: int = 0  # For time-based ATR sampling
    
    def __post_init__(self):
        """Initialize sub-objects with defaults."""
        if self.book is None:
            self.book = BookManager(symbol=self.symbol)
        if self.trades is None:
            self.trades = TradeStreamManager(symbol=self.symbol)
        if self.microprice_buf is None:
            self.microprice_buf = RingBuffer(maxlen=200)
        if self.spread_buf is None:
            self.spread_buf = RingBuffer(maxlen=200)
        if self.ofi_buf is None:
            self.ofi_buf = RingBuffer(maxlen=100)
        if self.book_bid_deltas is None:
            self.book_bid_deltas = TimeWindowSum(window_sec=2.0)
        if self.book_ask_deltas is None:
            self.book_ask_deltas = TimeWindowSum(window_sec=2.0)
        if self.micro_atr_ema is None:
            self.micro_atr_ema = EMACalculator(span=20)
    
    @property
    def is_book_ready(self) -> bool:
        """True if book data is ready."""
        return self.book.is_ready
    
    @property
    def is_system_ready(self) -> bool:
        """
        True if entire system is ready for trading.
        
        Requires:
        - Book ready
        - Trade stream ready
        - No kill-switch
        - Not in cooldown
        """
        if self.kill_switch:
            return False
        if not self.book.is_ready:
            return False
        if not self.trades.is_ready:
            return False
        return True
    
    @property
    def has_position(self) -> bool:
        """True if holding a position."""
        return abs(self.position_qty) > 1e-9
    
    @property
    def position_side(self) -> Optional[str]:
        """'long', 'short', or None."""
        if self.position_qty > 1e-9:
            return "long"
        elif self.position_qty < -1e-9:
            return "short"
        return None
    
    def can_enter(self, now_ms: int) -> bool:
        """
        Check if entry is allowed.
        
        Blocks if:
        - System not ready
        - Any order inflight
        - Active exit signal
        - In cooldown
        - Kill-switch active
        """
        if not self.is_system_ready:
            return False
        if self.entry_inflight or self.exit_inflight:
            return False
        if self.active_exit_signal:
            return False
        if now_ms < self.cooldown_until_ms:
            return False
        if now_ms < self.stoprun_cooldown_until_ms:
            return False
        return True
    
    def capture_decision_snapshot(self, ofi_norm: float) -> DecisionSnapshot:
        """Capture current state for order tracking."""
        tob = self.book.top_of_book
        return DecisionSnapshot(
            timestamp_ms=tob.timestamp_ms,
            best_bid=tob.best_bid,
            best_ask=tob.best_ask,
            bid_size=tob.bid_size,
            ask_size=tob.ask_size,
            microprice=tob.microprice,
            spread=tob.spread,
            ofi_norm=ofi_norm,
        )
    
    def reset_buffers(self) -> None:
        """Clear all feature buffers (called on resync)."""
        self.microprice_buf.clear()
        self.spread_buf.clear()
        self.ofi_buf.clear()
        self.ofi_history.clear()
        self.price_history.clear()
        self.stoprun_cooldown_until_ms = 0
        self.book_bid_deltas.clear()
        self.book_ask_deltas.clear()
        self.micro_atr_ema.reset()
        self.trades.clear()
    
    def update_position(self, qty: float, avg_price: float, now_ms: int) -> None:
        """Update position from fill."""
        if abs(qty) > 1e-9:
            # Opening or adding
            if not self.has_position:
                self.entry_time_ms = now_ms
            total_value = (self.position_qty * self.avg_entry) + (qty * avg_price)
            self.position_qty += qty
            if abs(self.position_qty) > 1e-9:
                self.avg_entry = total_value / self.position_qty
        else:
            # Closing
            self.position_qty = 0.0
            self.avg_entry = 0.0
            self.entry_time_ms = 0
    
    def close_position(self) -> None:
        """Clear position state."""
        self.position_qty = 0.0
        self.avg_entry = 0.0
        self.entry_time_ms = 0
