# hyperbot/execution/rate_limiter.py
"""
Rate Limiter for order execution.

Features:
- Sliding window order counter
- Rate limit error detection
- Automatic backoff on 429 responses
- Cooldown management
"""
import time
from dataclasses import dataclass, field
from typing import Optional, List, Deque
from collections import deque
from enum import Enum, auto

from ..logger import get_logger

log = get_logger(__name__)


class RateLimitStatus(Enum):
    """Rate limit check result."""
    OK = auto()           # Can proceed
    SOFT_LIMIT = auto()   # Approaching limit, slow down
    HARD_LIMIT = auto()   # At limit, must wait
    BACKOFF = auto()      # In backoff period from rate limit error


@dataclass
class RateLimitConfig:
    """Rate limit configuration."""
    max_orders_per_window: int = 80       # Conservative limit (Hyperliquid: 100/10s)
    window_sec: float = 10.0              # Window duration
    soft_limit_pct: float = 0.7           # Warn at 70% of limit
    backoff_initial_sec: float = 1.0      # Initial backoff on rate limit error
    backoff_max_sec: float = 30.0         # Maximum backoff duration
    backoff_multiplier: float = 2.0       # Backoff multiplier


class RateLimiter:
    """
    Sliding window rate limiter for order execution.
    
    Usage:
        limiter = RateLimiter()
        
        # Before sending order
        status = limiter.check()
        if status == RateLimitStatus.HARD_LIMIT:
            log.warning("Rate limited, waiting...")
            return
        
        # Send order...
        limiter.record_order()
        
        # If order returns rate limit error
        if response.get("error") == "rate_limited":
            limiter.on_rate_limit_error()
    """
    
    def __init__(self, config: Optional[RateLimitConfig] = None):
        self.config = config or RateLimitConfig()
        
        # Sliding window of order timestamps
        self._order_times: Deque[float] = deque()
        
        # Backoff state
        self._backoff_until: float = 0.0
        self._backoff_duration: float = self.config.backoff_initial_sec
        self._rate_limit_errors: int = 0
    
    def check(self) -> RateLimitStatus:
        """
        Check if we can send an order.
        
        Returns:
            RateLimitStatus indicating whether to proceed
        """
        now = time.time()
        
        # Check if in backoff period
        if now < self._backoff_until:
            remaining = self._backoff_until - now
            log.debug(f"Rate limiter in backoff: {remaining:.1f}s remaining")
            return RateLimitStatus.BACKOFF
        
        # Clean old entries from window
        self._cleanup_window(now)
        
        # Count orders in window
        order_count = len(self._order_times)
        limit = self.config.max_orders_per_window
        
        # Check limits
        if order_count >= limit:
            return RateLimitStatus.HARD_LIMIT
        
        soft_limit = int(limit * self.config.soft_limit_pct)
        if order_count >= soft_limit:
            return RateLimitStatus.SOFT_LIMIT
        
        return RateLimitStatus.OK
    
    def record_order(self) -> None:
        """Record that an order was sent."""
        now = time.time()
        self._order_times.append(now)
        self._cleanup_window(now)
    
    def on_rate_limit_error(self) -> None:
        """
        Called when a rate limit error (429) is received.
        
        Increases backoff duration exponentially.
        """
        now = time.time()
        self._rate_limit_errors += 1
        
        # Set backoff period
        self._backoff_until = now + self._backoff_duration
        
        log.warning(
            f"Rate limit error #{self._rate_limit_errors}. "
            f"Backing off for {self._backoff_duration:.1f}s"
        )
        
        # Increase backoff for next time (exponential)
        self._backoff_duration = min(
            self._backoff_duration * self.config.backoff_multiplier,
            self.config.backoff_max_sec,
        )
    
    def on_success(self) -> None:
        """Called when an order succeeds. Resets backoff."""
        # Reset backoff on success
        self._backoff_duration = self.config.backoff_initial_sec
    
    def _cleanup_window(self, now: float) -> None:
        """Remove entries older than window."""
        cutoff = now - self.config.window_sec
        while self._order_times and self._order_times[0] < cutoff:
            self._order_times.popleft()
    
    def get_wait_time(self) -> float:
        """
        Get recommended wait time before next order.
        
        Returns 0 if OK to proceed immediately.
        """
        now = time.time()
        
        # If in backoff, return remaining time
        if now < self._backoff_until:
            return self._backoff_until - now
        
        self._cleanup_window(now)
        order_count = len(self._order_times)
        
        # If at limit, wait for oldest order to expire
        if order_count >= self.config.max_orders_per_window:
            if self._order_times:
                oldest = self._order_times[0]
                expire_time = oldest + self.config.window_sec
                return max(0.0, expire_time - now)
        
        return 0.0
    
    def get_stats(self) -> dict:
        """Get rate limiter statistics."""
        now = time.time()
        self._cleanup_window(now)
        
        return {
            "orders_in_window": len(self._order_times),
            "max_orders": self.config.max_orders_per_window,
            "window_sec": self.config.window_sec,
            "in_backoff": now < self._backoff_until,
            "backoff_remaining": max(0, self._backoff_until - now),
            "rate_limit_errors": self._rate_limit_errors,
        }
