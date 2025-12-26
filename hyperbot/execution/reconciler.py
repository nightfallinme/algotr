# hyperbot/execution/reconciler.py
"""
Position Reconciliation Module.

Periodically checks positions via REST API and reconciles with local state.
Triggers kill-switch on significant mismatches.
"""
import asyncio
import time
from dataclasses import dataclass
from typing import Optional, Dict, Any, List, Callable
from enum import Enum, auto

from ..logger import get_logger

log = get_logger(__name__)


class ReconcileResult(Enum):
    """Result of reconciliation check."""
    OK = auto()              # Positions match
    ADJUSTED = auto()        # Minor mismatch, adjusted local
    MISMATCH = auto()        # Major mismatch, kill-switch triggered
    ERROR = auto()           # Failed to fetch positions


@dataclass
class ReconcileEvent:
    """Record of a reconciliation event."""
    timestamp_ms: int
    symbol: str
    result: ReconcileResult
    local_qty: float
    rest_qty: float
    delta: float
    action_taken: str = ""


class PositionReconciler:
    """
    Periodically reconciles local position state with REST API.
    
    Features:
    - Configurable check interval
    - Tolerance for minor mismatches (rounding, fees)
    - Kill-switch trigger on major mismatches
    - Event history for debugging
    
    Usage:
        reconciler = PositionReconciler(
            get_position_func=exchange.get_position,
            get_local_qty_func=get_local_qty,
            on_mismatch=handle_mismatch,
        )
        await reconciler.start()
    """
    
    def __init__(
        self,
        get_position_func: Callable[[str], Optional[Dict]],
        get_local_qty_func: Callable[[str], float],
        on_mismatch: Optional[Callable[[str, float, float], None]] = None,
        interval_sec: float = 5.0,
        tolerance_qty: float = 0.001,
        symbols: Optional[List[str]] = None,
    ):
        """
        Args:
            get_position_func: Function to get position from REST (exchange.get_position)
            get_local_qty_func: Function to get local position qty
            on_mismatch: Callback on major mismatch (symbol, local_qty, rest_qty)
            interval_sec: Reconciliation interval in seconds
            tolerance_qty: Tolerance for minor mismatches (lot size)
            symbols: Symbols to reconcile (if None, reconcile all)
        """
        self._get_position = get_position_func
        self._get_local_qty = get_local_qty_func
        self._on_mismatch = on_mismatch
        self._interval_sec = interval_sec
        self._tolerance_qty = tolerance_qty
        self._symbols = symbols or []
        
        # State
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._events: List[ReconcileEvent] = []
        
        # Stats
        self._check_count = 0
        self._mismatch_count = 0
    
    async def start(self, symbols: Optional[List[str]] = None) -> None:
        """Start the reconciliation loop."""
        if symbols:
            self._symbols = symbols
        
        self._running = True
        self._task = asyncio.create_task(self._loop())
        log.info(f"Reconciler started: interval={self._interval_sec}s, symbols={self._symbols}")
    
    async def stop(self) -> None:
        """Stop the reconciliation loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("Reconciler stopped")
    
    async def _loop(self) -> None:
        """Main reconciliation loop."""
        while self._running:
            try:
                await asyncio.sleep(self._interval_sec)
                await self.check_all()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Reconciler loop error: {e}")
    
    async def check_all(self) -> Dict[str, ReconcileResult]:
        """Check all configured symbols."""
        results = {}
        for symbol in self._symbols:
            result = await self.check_symbol(symbol)
            results[symbol] = result
        return results
    
    async def check_symbol(self, symbol: str) -> ReconcileResult:
        """
        Check single symbol position against REST.
        
        Returns:
            ReconcileResult indicating match status
        """
        now_ms = int(time.time() * 1000)
        self._check_count += 1
        
        try:
            # Get REST position (blocking call, run in executor)
            loop = asyncio.get_event_loop()
            rest_position = await loop.run_in_executor(
                None, self._get_position, symbol
            )
            
            # Get local position
            local_qty = self._get_local_qty(symbol)
            
            # Extract REST qty (signed)
            if rest_position:
                rest_qty = rest_position.get("size", 0)
                if rest_position.get("side") == "short":
                    rest_qty = -rest_qty
            else:
                rest_qty = 0.0
            
            # Calculate delta
            delta = abs(local_qty - rest_qty)
            
            # Determine result
            if delta < 1e-9:
                result = ReconcileResult.OK
                action = ""
            elif delta <= self._tolerance_qty:
                result = ReconcileResult.ADJUSTED
                action = f"Adjusted local from {local_qty} to {rest_qty}"
                log.debug(f"[{symbol}] Minor reconcile: {action}")
            else:
                result = ReconcileResult.MISMATCH
                action = f"MISMATCH local={local_qty} rest={rest_qty}"
                self._mismatch_count += 1
                log.warning(f"[{symbol}] {action}")
                
                # Trigger callback
                if self._on_mismatch:
                    try:
                        self._on_mismatch(symbol, local_qty, rest_qty)
                    except Exception as e:
                        log.error(f"Mismatch callback error: {e}")
            
            # Record event
            event = ReconcileEvent(
                timestamp_ms=now_ms,
                symbol=symbol,
                result=result,
                local_qty=local_qty,
                rest_qty=rest_qty,
                delta=delta,
                action_taken=action,
            )
            self._events.append(event)
            
            # Trim history
            if len(self._events) > 1000:
                self._events = self._events[-500:]
            
            return result
            
        except Exception as e:
            log.error(f"[{symbol}] Reconcile error: {e}")
            return ReconcileResult.ERROR
    
    def force_check(self, symbol: str) -> None:
        """Schedule immediate check for a symbol."""
        asyncio.create_task(self.check_symbol(symbol))
    
    def get_stats(self) -> Dict[str, Any]:
        """Get reconciler statistics."""
        return {
            "check_count": self._check_count,
            "mismatch_count": self._mismatch_count,
            "symbols": self._symbols,
            "interval_sec": self._interval_sec,
            "running": self._running,
        }
    
    def get_recent_events(self, limit: int = 20) -> List[ReconcileEvent]:
        """Get recent reconciliation events."""
        return self._events[-limit:]
