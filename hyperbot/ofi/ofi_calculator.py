# hyperbot/ofi/ofi_calculator.py
"""
OFI (Order Flow Imbalance) Calculator.

Combines book-based and trade-based OFI into a normalized signal.
"""
from dataclasses import dataclass
from typing import Optional
import time

from .coin_state import CoinState
from ..ws.data_structures import EMACalculator
from ..logger import get_logger

log = get_logger(__name__)


@dataclass
class OFISignal:
    """OFI calculation result."""
    ofi_book: float = 0.0      # Book-based OFI (delta sizes)
    ofi_trade: float = 0.0     # Trade-based OFI (buy - sell volume)
    ofi_raw: float = 0.0       # Combined raw OFI
    ofi_norm: float = 0.0      # Normalized OFI
    ofi_smooth: float = 0.0    # EMA-smoothed OFI
    is_ready: bool = False     # True if calculation is valid
    
    @property
    def is_long_signal(self) -> bool:
        """True if OFI indicates bullish."""
        return self.is_ready and self.ofi_smooth > 0
    
    @property
    def is_short_signal(self) -> bool:
        """True if OFI indicates bearish."""
        return self.is_ready and self.ofi_smooth < 0


class OFICalculator:
    """
    Calculates Order Flow Imbalance from book and trade data.
    
    Formula:
        ofi_book = sum(delta_bid_size) - sum(delta_ask_size)
        ofi_trade = buy_volume - sell_volume
        raw_ofi = w1 * ofi_book + w2 * ofi_trade
        ofi_norm = raw_ofi / max(rolling_depth, epsilon)
        ofi_smooth = EMA(ofi_norm)
    
    Usage:
        calc = OFICalculator()
        signal = calc.compute(coin_state, now_ms)
        if signal.is_ready and abs(signal.ofi_smooth) > threshold:
            # Trade signal
    """
    
    def __init__(
        self,
        book_weight: float = 0.5,
        trade_weight: float = 0.5,
        window_sec: float = 2.0,
        ema_span: int = 10,
        min_depth: float = 0.001,  # Epsilon to avoid division by zero
    ):
        self.book_weight = book_weight
        self.trade_weight = trade_weight
        self.window_sec = window_sec
        self.min_depth = min_depth
        
        # Per-symbol EMA smoothers
        self._ema_smoothers: dict[str, EMACalculator] = {}
        self._ema_span = ema_span
    
    def compute(self, state: CoinState, now_ms: Optional[int] = None) -> OFISignal:
        """
        Compute OFI for a coin.
        
        Args:
            state: CoinState with book and trade data
            now_ms: Current timestamp (defaults to now)
            
        Returns:
            OFISignal with calculation results
        """
        now = now_ms or int(time.time() * 1000)
        signal = OFISignal()
        
        # Check readiness
        if not state.is_book_ready:
            return signal
        
        # Get book OFI (sum of deltas in window)
        bid_delta_sum = state.book_bid_deltas.sum(now, self.window_sec)
        ask_delta_sum = state.book_ask_deltas.sum(now, self.window_sec)
        signal.ofi_book = bid_delta_sum - ask_delta_sum
        
        # Get trade OFI
        signal.ofi_trade = state.trades.get_ofi_trade(now)
        
        # Combine
        signal.ofi_raw = (
            self.book_weight * signal.ofi_book +
            self.trade_weight * signal.ofi_trade
        )
        
        # Normalize by current depth
        tob = state.book.top_of_book
        depth = max(tob.bid_size + tob.ask_size, self.min_depth)
        signal.ofi_norm = signal.ofi_raw / depth
        
        # Apply EMA smoothing
        ema = self._get_ema(state.symbol)
        signal.ofi_smooth = ema.update(signal.ofi_norm)
        
        # Store in state buffer
        state.ofi_buf.push(signal.ofi_smooth)
        
        # Check if ready - EMA needs minimum samples
        # Note: trades.is_ready is optional - book OFI works without trade data
        signal.is_ready = ema.is_ready(min_count=5)
        
        return signal
    
    def _get_ema(self, symbol: str) -> EMACalculator:
        """Get or create EMA smoother for symbol."""
        if symbol not in self._ema_smoothers:
            self._ema_smoothers[symbol] = EMACalculator(span=self._ema_span)
        return self._ema_smoothers[symbol]
    
    def reset(self, symbol: Optional[str] = None) -> None:
        """Reset EMA state for symbol or all."""
        if symbol:
            if symbol in self._ema_smoothers:
                self._ema_smoothers[symbol].reset()
        else:
            self._ema_smoothers.clear()
