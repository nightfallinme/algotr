# hyperbot/ofi/filters.py
"""
Entry filters for OFI scalping.

- Volatility filter (volZ)
- Spread filter (spreadZ)
"""
from dataclasses import dataclass
from typing import Optional
from enum import Enum, auto

from .coin_state import CoinState
from ..ws.data_structures import RingBuffer
from ..logger import get_logger

log = get_logger(__name__)


class FilterStatus(Enum):
    """Filter check result."""
    PASS = auto()       # OK to trade
    BLOCK = auto()      # Block trading
    NOT_READY = auto()  # Insufficient data


@dataclass
class FilterResult:
    """Result of a filter check."""
    status: FilterStatus
    value: float = 0.0      # Computed metric (volZ, spreadZ)
    threshold: float = 0.0  # Threshold used
    reason: str = ""


class VolatilityFilter:
    """
    Filters trades based on microprice volatility with hysteresis.
    
    Logic:
        microATR = EMA(|delta_microprice|)
        avg_vol = rolling average of microATR
        volZ = microATR / avg_vol
        Block if volZ > cutoff
        Unblock if volZ < unblock_cutoff (hysteresis)
    """
    
    def __init__(
        self,
        window_ticks: int = 50,
        cutoff: float = 1.5,
        unblock_cutoff: float = 1.3,  # Hysteresis
        min_ready_count: int = 30,
    ):
        self.window_ticks = window_ticks
        self.cutoff = cutoff
        self.unblock_cutoff = unblock_cutoff
        self.min_ready_count = min_ready_count
        
        # Rolling average of ATR for baseline
        self._atr_history: dict[str, RingBuffer] = {}
        # Hysteresis state per symbol
        self._is_blocked: dict[str, bool] = {}
    
    def check(self, state: CoinState) -> FilterResult:
        """
        Check volatility filter with hysteresis.
        
        Returns:
            FilterResult with status and metrics
        """
        symbol = state.symbol
        
        # Check readiness
        if state.microprice_buf.size < self.min_ready_count:
            return FilterResult(
                status=FilterStatus.NOT_READY,
                reason=f"Need {self.min_ready_count} ticks, have {state.microprice_buf.size}"
            )
        
        # Get current microATR
        current_atr = state.micro_atr_ema.value
        if current_atr is None or current_atr <= 0:
            return FilterResult(status=FilterStatus.NOT_READY, reason="No ATR data")
        
        # Get or create ATR history
        atr_buf = self._get_atr_buffer(symbol)
        atr_buf.push(current_atr)
        
        # Calculate baseline (average ATR)
        if atr_buf.size < self.min_ready_count // 2:
            return FilterResult(status=FilterStatus.NOT_READY, reason="Building ATR baseline")
        
        avg_atr = atr_buf.mean()
        if avg_atr <= 0:
            return FilterResult(status=FilterStatus.NOT_READY, reason="Invalid ATR baseline")
        
        # Calculate volZ
        volZ = current_atr / avg_atr
        
        # Hysteresis logic
        was_blocked = self._is_blocked.get(symbol, False)
        
        if was_blocked:
            # Currently blocked - only unblock if below unblock threshold
            if volZ < self.unblock_cutoff:
                self._is_blocked[symbol] = False
                # Now unblocked, check if still above cutoff
                if volZ > self.cutoff:
                    self._is_blocked[symbol] = True
        else:
            # Currently not blocked - block if above cutoff
            if volZ > self.cutoff:
                self._is_blocked[symbol] = True
        
        is_blocked = self._is_blocked.get(symbol, False)
        
        if is_blocked:
            return FilterResult(
                status=FilterStatus.BLOCK,
                value=volZ,
                threshold=self.cutoff,
                reason=f"High volatility: volZ={volZ:.2f} > {self.cutoff}"
            )
        
        return FilterResult(
            status=FilterStatus.PASS,
            value=volZ,
            threshold=self.cutoff,
        )
    
    def _get_atr_buffer(self, symbol: str) -> RingBuffer:
        """Get or create ATR history buffer for symbol."""
        if symbol not in self._atr_history:
            # Use 4x window for baseline stability
            self._atr_history[symbol] = RingBuffer(maxlen=self.window_ticks * 4)
        return self._atr_history[symbol]
    
    def is_ready(self, state: CoinState) -> bool:
        """Check if filter has enough data."""
        return self.check(state).status != FilterStatus.NOT_READY


class SpreadFilter:
    """
    Filters trades based on bid-ask spread with hysteresis.
    
    Logic:
        spreadZ = current_spread / rolling_avg_spread
        Block if spreadZ > cutoff (wide spread = expensive taker)
        Unblock if spreadZ < unblock_cutoff (hysteresis)
    """
    
    def __init__(
        self,
        window_ticks: int = 200,
        cutoff: float = 2.0,
        unblock_cutoff: float = 1.7,  # Hysteresis
        min_ready_count: int = 50,
    ):
        self.window_ticks = window_ticks
        self.cutoff = cutoff
        self.unblock_cutoff = unblock_cutoff
        self.min_ready_count = min_ready_count
        # Hysteresis state per symbol
        self._is_blocked: dict[str, bool] = {}
    
    def check(self, state: CoinState) -> FilterResult:
        """
        Check spread filter with hysteresis.
        
        Returns:
            FilterResult with status and metrics
        """
        symbol = state.symbol
        
        # Check readiness
        if state.spread_buf.size < self.min_ready_count:
            return FilterResult(
                status=FilterStatus.NOT_READY,
                reason=f"Need {self.min_ready_count} spreads, have {state.spread_buf.size}"
            )
        
        # Get current spread
        current_spread = state.book.top_of_book.spread
        if current_spread <= 0:
            return FilterResult(status=FilterStatus.NOT_READY, reason="Invalid spread")
        
        # Calculate average spread
        avg_spread = state.spread_buf.mean()
        if avg_spread <= 0:
            return FilterResult(status=FilterStatus.NOT_READY, reason="Invalid avg spread")
        
        # Calculate spreadZ
        spreadZ = current_spread / avg_spread
        
        # Hysteresis logic
        was_blocked = self._is_blocked.get(symbol, False)
        
        if was_blocked:
            # Currently blocked - only unblock if below unblock threshold
            if spreadZ < self.unblock_cutoff:
                self._is_blocked[symbol] = False
                if spreadZ > self.cutoff:
                    self._is_blocked[symbol] = True
        else:
            # Currently not blocked - block if above cutoff
            if spreadZ > self.cutoff:
                self._is_blocked[symbol] = True
        
        is_blocked = self._is_blocked.get(symbol, False)
        
        if is_blocked:
            return FilterResult(
                status=FilterStatus.BLOCK,
                value=spreadZ,
                threshold=self.cutoff,
                reason=f"Wide spread: spreadZ={spreadZ:.2f} > {self.cutoff}"
            )
        
        return FilterResult(
            status=FilterStatus.PASS,
            value=spreadZ,
            threshold=self.cutoff,
        )
    
    def is_ready(self, state: CoinState) -> bool:
        """Check if filter has enough data."""
        return self.check(state).status != FilterStatus.NOT_READY


class NetEdgeChecker:
    """
    Validates that expected edge exceeds costs.
    
    Logic:
        expected_edge = target_tp_bps - (taker_fee_bps * 2) - est_slippage_bps
        if expected_edge <= min_edge_bps: BLOCK
    """
    
    def __init__(
        self,
        target_tp_bps: float = 10.0,      # 0.10%
        taker_fee_bps: float = 2.5,       # 0.025% each way
        min_edge_bps: float = 1.0,        # Minimum net edge required
    ):
        self.target_tp_bps = target_tp_bps
        self.taker_fee_bps = taker_fee_bps
        self.min_edge_bps = min_edge_bps
    
    def check(
        self,
        state: CoinState,
        est_slippage_bps: float = 0.0,
    ) -> FilterResult:
        """
        Check if trade has positive expected edge.
        
        Args:
            state: CoinState
            est_slippage_bps: Estimated slippage in basis points
            
        Returns:
            FilterResult
        """
        # Calculate net edge
        total_cost = (self.taker_fee_bps * 2) + est_slippage_bps
        net_edge = self.target_tp_bps - total_cost
        
        if net_edge <= self.min_edge_bps:
            return FilterResult(
                status=FilterStatus.BLOCK,
                value=net_edge,
                threshold=self.min_edge_bps,
                reason=f"Insufficient edge: {net_edge:.2f}bps vs min {self.min_edge_bps}bps"
            )
        
        return FilterResult(
            status=FilterStatus.PASS,
            value=net_edge,
            threshold=self.min_edge_bps,
        )
