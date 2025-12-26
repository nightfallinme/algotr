# hyperbot/execution/kill_switch.py
"""
Kill-Switch system for safety controls.

Triggers on:
- Data integrity issues (book invalid, gap, negative spread)
- Execution issues (no ack, reconcile mismatch)
- Microstructure issues (vol spike, spread spike)
"""
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, Callable, List
from enum import Enum, auto
from datetime import datetime
import time

from ..logger import get_logger

log = get_logger(__name__)


class KillReason(Enum):
    """Kill-switch trigger reasons."""
    # Data integrity
    BOOK_RESYNC = auto()
    NEGATIVE_SPREAD = auto()
    SEQUENCE_GAP = auto()
    TIMESTAMP_ANOMALY = auto()
    
    # Execution integrity
    ORDER_STUCK = auto()
    RECONCILE_MISMATCH = auto()
    NO_ACK = auto()
    
    # Microstructure
    HIGH_VOLATILITY = auto()
    WIDE_SPREAD = auto()
    
    # Rate limiting
    RATE_LIMITED = auto()
    
    # Manual
    MANUAL = auto()
    
    # System
    WS_DISCONNECT = auto()
    ERROR = auto()


@dataclass
class KillEvent:
    """Record of a kill-switch trigger."""
    reason: KillReason
    symbol: str
    timestamp_ms: int
    details: str = ""
    action_taken: str = ""


class KillSwitch:
    """
    Centralized kill-switch controller.
    
    Usage:
        ks = KillSwitch(on_trigger=handle_kill)
        ks.trigger("ETH", KillReason.BOOK_RESYNC, "Sequence gap detected")
        
        if ks.is_active("ETH"):
            # Don't trade
    """
    
    def __init__(
        self,
        cooldown_sec: float = 30.0,
        on_trigger: Optional[Callable[[str, KillReason, str], None]] = None,
    ):
        self.cooldown_sec = cooldown_sec
        self._on_trigger = on_trigger
        
        # Per-symbol state
        self._active: Dict[str, bool] = {}
        self._cooldown_until: Dict[str, int] = {}  # symbol -> timestamp_ms
        
        # History
        self._events: List[KillEvent] = []
        self._trigger_counts: Dict[KillReason, int] = {}
    
    def trigger(
        self,
        symbol: str,
        reason: KillReason,
        details: str = "",
        should_flatten: bool = False,
    ) -> None:
        """
        Trigger kill-switch for a symbol.
        
        Args:
            symbol: Coin symbol
            reason: Trigger reason
            details: Additional context
            should_flatten: If True, suggests flattening position
        """
        now_ms = int(time.time() * 1000)
        
        # Record event
        event = KillEvent(
            reason=reason,
            symbol=symbol,
            timestamp_ms=now_ms,
            details=details,
        )
        self._events.append(event)
        
        # Update counts
        self._trigger_counts[reason] = self._trigger_counts.get(reason, 0) + 1
        
        # Activate
        self._active[symbol] = True
        self._cooldown_until[symbol] = now_ms + int(self.cooldown_sec * 1000)
        
        log.warning(f"KILL-SWITCH: [{symbol}] {reason.name} - {details}")
        
        # Callback
        if self._on_trigger:
            try:
                self._on_trigger(symbol, reason, details)
            except Exception as e:
                log.error(f"Kill-switch callback error: {e}")
    
    def is_active(self, symbol: str) -> bool:
        """Check if kill-switch is active for symbol."""
        return self._active.get(symbol, False)
    
    def check_cooldown(self, symbol: str, now_ms: Optional[int] = None) -> bool:
        """
        Check if cooldown has expired and clear if so.
        
        Returns: True if still in cooldown, False if cleared
        """
        if not self._active.get(symbol, False):
            return False
        
        now = now_ms or int(time.time() * 1000)
        until = self._cooldown_until.get(symbol, 0)
        
        if now >= until:
            self.clear(symbol)
            return False
        
        return True
    
    def clear(self, symbol: str) -> None:
        """Clear kill-switch for symbol."""
        if self._active.get(symbol, False):
            self._active[symbol] = False
            log.info(f"Kill-switch cleared: {symbol}")
    
    def clear_all(self) -> None:
        """Clear all kill-switches."""
        for symbol in list(self._active.keys()):
            self._active[symbol] = False
        log.info("All kill-switches cleared")
    
    def get_stats(self) -> Dict[str, Any]:
        """Get kill-switch statistics."""
        return {
            "active_symbols": [s for s, v in self._active.items() if v],
            "total_triggers": len(self._events),
            "counts_by_reason": {r.name: c for r, c in self._trigger_counts.items()},
        }
    
    def get_recent_events(self, limit: int = 10) -> List[KillEvent]:
        """Get recent kill events."""
        return self._events[-limit:]
