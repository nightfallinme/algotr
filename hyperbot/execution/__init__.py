# hyperbot/execution/__init__.py
"""
Execution module for OFI scalping.

Provides order management, state tracking, and safety mechanisms.
"""
from .order_manager import OrderManager, Order, OrderState, OrderIntent
from .kill_switch import KillSwitch, KillReason
from .reconciler import PositionReconciler, ReconcileResult
from .metrics import MetricsCollector
from .rate_limiter import RateLimiter, RateLimitStatus, RateLimitConfig

__all__ = [
    "OrderManager",
    "Order",
    "OrderState",
    "OrderIntent",
    "KillSwitch",
    "KillReason",
    "PositionReconciler",
    "ReconcileResult",
    "MetricsCollector",
    "RateLimiter",
    "RateLimitStatus",
    "RateLimitConfig",
]
