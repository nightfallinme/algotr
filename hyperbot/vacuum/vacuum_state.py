# hyperbot/vacuum/vacuum_state.py
"""
Per-coin state container for Vacuum Scalping.

Centralizes all state needed for signal generation and execution.
Extends CoinState pattern from OFI module with vacuum-specific tracking.
"""
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, Tuple
from collections import deque
import time

from ..ws.data_structures import RingBuffer, EMACalculator
from ..ws.book_manager import BookManager, BookState
from ..ws.trade_stream import TradeStreamManager
from .liquidity_map import LiquidityMap, LiquidityWall
from .vacuum_detector import VacuumDetector, VacuumSignal
from .wall_tracker import WallTracker, WallTouch
from .lir_calculator import LIRCalculator, LIRSignal
from ..logger import get_logger

log = get_logger(__name__)


@dataclass
class DecisionSnapshot:
    """Snapshot of state at decision time."""
    timestamp_ms: int = 0
    best_bid: float = 0.0
    best_ask: float = 0.0
    microprice: float = 0.0
    spread: float = 0.0
    lir: float = 1.0
    vacuum_side: str = ""
    vacuum_bps: float = 0.0
    entry_mode: str = ""  # "break" | "fade"
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.timestamp_ms,
            "bid": self.best_bid,
            "ask": self.best_ask,
            "microprice": self.microprice,
            "spread": self.spread,
            "lir": round(self.lir, 3),
            "vacuum_side": self.vacuum_side,
            "vacuum_bps": round(self.vacuum_bps, 2),
            "entry_mode": self.entry_mode,
        }


@dataclass
class VacuumCoinState:
    """
    Complete state for a single coin in Vacuum Scalping.
    
    Contains:
    - Book manager (L2 state)
    - Liquidity analysis components
    - Position state
    - Execution locks
    """
    symbol: str
    
    # === Data Managers ===
    book: BookManager = field(default=None)
    trades: TradeStreamManager = field(default=None)
    
    # === Vacuum Components ===
    liquidity_map: LiquidityMap = field(default=None)
    vacuum_detector: VacuumDetector = field(default=None)
    wall_tracker: WallTracker = field(default=None)
    lir_calculator: LIRCalculator = field(default=None)
    
    # === Feature Buffers ===
    microprice_buf: RingBuffer = field(default=None)
    spread_buf: RingBuffer = field(default=None)
    micro_atr_ema: EMACalculator = field(default=None)
    
    # === Last Signals ===
    last_vacuum: Optional[VacuumSignal] = None
    last_lir: Optional[LIRSignal] = None
    last_bid_wall: Optional[LiquidityWall] = None
    last_ask_wall: Optional[LiquidityWall] = None
    
    # === Position State ===
    position_qty: float = 0.0
    avg_entry: float = 0.0
    entry_time_ms: int = 0
    entry_mode: str = ""  # "break" | "fade"
    entry_wall_price: float = 0.0  # For structural invalidation checks
    tp_trail_count: int = 0  # Number of times TP was extended
    last_tp_extend_ms: int = 0  # Last TP extension timestamp
    tp_extension_cooldown_ms: int = 5000  # Min time between extensions
    
    # === Execution Locks ===
    entry_inflight: bool = False
    exit_inflight: bool = False
    active_exit_signal: bool = False
    cooldown_until_ms: int = 0
    
    # === System State ===
    kill_switch: bool = False
    last_decision_ms: int = 0
    last_microprice: float = 0.0
    tick_count: int = 0
    
    def __post_init__(self):
        """Initialize sub-objects with defaults."""
        if self.book is None:
            self.book = BookManager(symbol=self.symbol)
        if self.trades is None:
            self.trades = TradeStreamManager(symbol=self.symbol)
        if self.liquidity_map is None:
            self.liquidity_map = LiquidityMap()
        if self.vacuum_detector is None:
            self.vacuum_detector = VacuumDetector()
        if self.wall_tracker is None:
            self.wall_tracker = WallTracker()
        if self.lir_calculator is None:
            self.lir_calculator = LIRCalculator()
        if self.microprice_buf is None:
            self.microprice_buf = RingBuffer(maxlen=200)
        if self.spread_buf is None:
            self.spread_buf = RingBuffer(maxlen=200)
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
        - Liquidity map ready
        - No kill-switch
        """
        if self.kill_switch:
            return False
        if not self.book.is_ready:
            return False
        if not self.liquidity_map.is_ready:
            return False
        return True
    
    @property
    def has_position(self) -> bool:
        """True if holding a position."""
        return abs(self.position_qty) > 1e-9
    
    @property
    def atr_bps(self) -> float:
        """Current micro ATR in bps (for dynamic TP/SL)."""
        if self.micro_atr_ema and self.micro_atr_ema.value > 0:
            return self.micro_atr_ema.value
        return 0.4  # Fallback minimum
    
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
        return True
    
    def capture_decision_snapshot(
        self,
        entry_mode: str,
        vacuum: Optional[VacuumSignal] = None,
        lir: Optional[LIRSignal] = None,
    ) -> DecisionSnapshot:
        """Capture current state for order tracking."""
        tob = self.book.top_of_book
        return DecisionSnapshot(
            timestamp_ms=tob.timestamp_ms,
            best_bid=tob.best_bid,
            best_ask=tob.best_ask,
            microprice=tob.microprice,
            spread=tob.spread,
            lir=lir.lir_smooth if lir else 1.0,
            vacuum_side=vacuum.side if vacuum else "",
            vacuum_bps=vacuum.vacuum_bps if vacuum else 0.0,
            entry_mode=entry_mode,
        )
    
    def update_signals(self) -> None:
        """Update all vacuum signals from current book state."""
        tob = self.book.top_of_book
        
        if not tob.is_valid:
            return
        
        # Parse L2 levels for liquidity map
        bids = [(lvl[0], lvl[1]) for lvl in self.book._bids.items()]
        asks = [(lvl[0], lvl[1]) for lvl in self.book._asks.items()]
        
        # Sort properly
        bids_sorted = sorted(bids, key=lambda x: -x[0])  # Descending
        asks_sorted = sorted(asks, key=lambda x: x[0])   # Ascending
        
        # Update liquidity map
        self.liquidity_map.update(bids_sorted, asks_sorted, tob.mid)
        
        # Get walls
        self.last_bid_wall, self.last_ask_wall = self.liquidity_map.get_walls()
        
        # Update wall tracker
        self.wall_tracker.update(
            tob.microprice,
            tob.mid,
            self.last_bid_wall,
            self.last_ask_wall,
        )
        
        # Detect vacuum
        self.last_vacuum = self.vacuum_detector.detect(self.liquidity_map, tob)
        
        # Calculate LIR
        self.last_lir = self.lir_calculator.compute(self.liquidity_map, self.symbol)
        
        # Update feature buffers
        self.microprice_buf.push(tob.microprice)
        self.spread_buf.push(tob.spread)
        
        # Update microATR
        if self.last_microprice > 0:
            delta = abs(tob.microprice - self.last_microprice)
            self.micro_atr_ema.update(delta)
        self.last_microprice = tob.microprice
        
        self.tick_count += 1
    
    def reset_buffers(self) -> None:
        """Clear all feature buffers (called on resync)."""
        self.microprice_buf.clear()
        self.spread_buf.clear()
        self.micro_atr_ema.reset()
        self.liquidity_map.reset()
        self.wall_tracker.reset()
        self.lir_calculator.reset(self.symbol)
        self.trades.clear()
        self.last_vacuum = None
        self.last_lir = None
        self.last_bid_wall = None
        self.last_ask_wall = None
        # Note: tick_count NOT reset - it should persist to track total updates
    
    def update_position(self, qty: float, avg_price: float, now_ms: int, mode: str = "") -> None:
        """Update position from fill."""
        if abs(qty) > 1e-9:
            if not self.has_position:
                self.entry_time_ms = now_ms
                self.entry_mode = mode
            total_value = (self.position_qty * self.avg_entry) + (qty * avg_price)
            self.position_qty += qty
            if abs(self.position_qty) > 1e-9:
                self.avg_entry = total_value / self.position_qty
        else:
            self.close_position()
    
    def close_position(self) -> None:
        """Clear position state."""
        self.position_qty = 0.0
        self.avg_entry = 0.0
        self.entry_time_ms = 0
        self.entry_mode = ""
        self.entry_wall_price = 0.0
        self.tp_trail_count = 0
        self.last_tp_extend_ms = 0
    
    def get_micro_atr(self) -> float:
        """Get current micro ATR value."""
        return self.micro_atr_ema.value or 0.0
    
    def get_avg_spread(self) -> float:
        """Get average spread from buffer."""
        return self.spread_buf.mean() if self.spread_buf.size > 0 else 0.0
