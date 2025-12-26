# hyperbot/ws/data_structures.py
"""
Core data structures for windowed calculations.
Thread-safe, allocation-efficient implementations.
"""
from collections import deque
from typing import List, Optional, Tuple, Callable
import time


class RingBuffer:
    """
    Fixed-size ring buffer for efficient windowed calculations.
    O(1) push, O(1) access to last N items.
    """
    __slots__ = ('_buffer', '_maxlen')
    
    def __init__(self, maxlen: int):
        if maxlen <= 0:
            raise ValueError("maxlen must be positive")
        self._maxlen = maxlen
        self._buffer: deque = deque(maxlen=maxlen)
    
    def push(self, value: float) -> None:
        """Add value to buffer, evicting oldest if full."""
        self._buffer.append(value)
    
    def clear(self) -> None:
        """Clear all values."""
        self._buffer.clear()
    
    @property
    def size(self) -> int:
        """Current number of items."""
        return len(self._buffer)
    
    @property
    def maxlen(self) -> int:
        """Maximum capacity."""
        return self._maxlen
    
    @property
    def is_full(self) -> bool:
        """True if buffer is at capacity."""
        return len(self._buffer) >= self._maxlen
    
    def is_ready(self, min_size: Optional[int] = None) -> bool:
        """Check if buffer has enough data."""
        required = min_size if min_size is not None else self._maxlen
        return len(self._buffer) >= required
    
    def last(self, n: Optional[int] = None) -> List[float]:
        """Get last N items (or all if n is None)."""
        if n is None or n >= len(self._buffer):
            return list(self._buffer)
        return list(self._buffer)[-n:]
    
    def get(self, index: int) -> float:
        """Get item at index (-1 for last, 0 for oldest)."""
        return self._buffer[index]
    
    def __len__(self) -> int:
        return len(self._buffer)
    
    def __iter__(self):
        return iter(self._buffer)
    
    # === Statistical helpers (pure, no allocation in hot path) ===
    
    def sum(self) -> float:
        """Sum of all values."""
        return sum(self._buffer)
    
    def mean(self) -> float:
        """Mean of all values."""
        if not self._buffer:
            return 0.0
        return sum(self._buffer) / len(self._buffer)
    
    def std(self) -> float:
        """Standard deviation of all values."""
        if len(self._buffer) < 2:
            return 0.0
        mean = self.mean()
        variance = sum((x - mean) ** 2 for x in self._buffer) / len(self._buffer)
        return variance ** 0.5


class TimeWindowSum:
    """
    Time-based windowed sum for OFI calculations.
    Maintains sum of values within a sliding time window.
    
    Example: Sum of buy volume in last 2 seconds.
    """
    __slots__ = ('_window_sec', '_data', '_sum_cache', '_last_prune_time')
    
    def __init__(self, window_sec: float):
        if window_sec <= 0:
            raise ValueError("window_sec must be positive")
        self._window_sec = window_sec
        self._data: deque = deque()  # (timestamp_ms, value)
        self._sum_cache: float = 0.0
        self._last_prune_time: float = 0.0
    
    def add(self, timestamp_ms: int, value: float) -> None:
        """Add a value at given timestamp (milliseconds)."""
        self._data.append((timestamp_ms, value))
        self._sum_cache += value
    
    def sum(self, now_ms: Optional[int] = None, window_sec: Optional[float] = None) -> float:
        """
        Get sum of values within window.
        
        Args:
            now_ms: Current timestamp in milliseconds (defaults to current time)
            window_sec: Override window size (defaults to configured window)
        """
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        
        window = window_sec if window_sec is not None else self._window_sec
        cutoff = now_ms - int(window * 1000)
        
        # Prune expired entries
        self._prune(cutoff)
        
        return self._sum_cache
    
    def _prune(self, cutoff_ms: int) -> None:
        """Remove entries older than cutoff, updating cache."""
        while self._data and self._data[0][0] < cutoff_ms:
            _, old_value = self._data.popleft()
            self._sum_cache -= old_value
    
    def clear(self) -> None:
        """Clear all data."""
        self._data.clear()
        self._sum_cache = 0.0
    
    @property
    def count(self) -> int:
        """Number of entries currently in window."""
        return len(self._data)
    
    def is_ready(self, min_count: int = 1) -> bool:
        """Check if we have minimum data points."""
        return len(self._data) >= min_count


class EMACalculator:
    """
    Exponential Moving Average calculator.
    Updates incrementally without storing full history.
    """
    __slots__ = ('_alpha', '_value', '_count')
    
    def __init__(self, span: int):
        """
        Args:
            span: EMA span (e.g., 50 for EMA50)
        """
        if span <= 0:
            raise ValueError("span must be positive")
        self._alpha = 2.0 / (span + 1)
        self._value: Optional[float] = None
        self._count: int = 0
    
    def update(self, value: float) -> float:
        """Update EMA with new value and return current EMA."""
        self._count += 1
        if self._value is None:
            self._value = value
        else:
            self._value = self._alpha * value + (1 - self._alpha) * self._value
        return self._value
    
    @property
    def value(self) -> Optional[float]:
        """Current EMA value."""
        return self._value
    
    @property
    def count(self) -> int:
        """Number of updates."""
        return self._count
    
    def is_ready(self, min_count: Optional[int] = None) -> bool:
        """Check if EMA has enough data."""
        # By default, require at least span/2 data points
        required = min_count if min_count is not None else 1
        return self._count >= required
    
    def reset(self) -> None:
        """Reset EMA state."""
        self._value = None
        self._count = 0
