# hyperbot/execution/metrics.py
"""
Metrics and Telemetry Module.

Tracks key performance indicators for OFI scalping:
- Latency (tick-to-ack, tick-to-fill)
- Slippage (realized vs expected)
- Fill rate
- Kill-switch triggers
"""
import time
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List, Deque
from collections import deque
import statistics

from ..logger import get_logger

log = get_logger(__name__)


@dataclass
class LatencyMetric:
    """Single latency measurement."""
    timestamp_ms: int
    symbol: str
    metric_type: str  # "tick_to_send", "send_to_ack", "ack_to_fill"
    value_ms: float


@dataclass
class SlippageMetric:
    """Single slippage measurement."""
    timestamp_ms: int
    symbol: str
    side: str
    decision_price: float
    fill_price: float
    slippage_bps: float  # Basis points


@dataclass
class TradeMetric:
    """Trade outcome metric."""
    timestamp_ms: int
    symbol: str
    side: str
    qty: float
    entry_price: float
    exit_price: float = 0.0
    pnl_pct: float = 0.0
    hold_time_sec: float = 0.0
    exit_reason: str = ""


class MetricsCollector:
    """
    Collects and aggregates trading metrics.
    
    Usage:
        metrics = MetricsCollector()
        metrics.record_latency("ETH", "tick_to_ack", 15.5)
        metrics.record_slippage("ETH", "buy", 1000.0, 1000.5)
        
        stats = metrics.get_summary()
    """
    
    def __init__(self, history_size: int = 1000):
        self._history_size = history_size
        
        # Latency tracking
        self._latencies: Deque[LatencyMetric] = deque(maxlen=history_size)
        
        # Slippage tracking
        self._slippages: Deque[SlippageMetric] = deque(maxlen=history_size)
        
        # Trade tracking
        self._trades: Deque[TradeMetric] = deque(maxlen=history_size)
        
        # Counters
        self._trade_count = 0
        self._win_count = 0
        self._fill_count = 0
        self._reject_count = 0
        self._timeout_count = 0
        
        # Kill-switch tracking
        self._kill_switch_counts: Dict[str, int] = {}
    
    def record_latency(
        self,
        symbol: str,
        metric_type: str,
        value_ms: float,
    ) -> None:
        """Record a latency measurement."""
        metric = LatencyMetric(
            timestamp_ms=int(time.time() * 1000),
            symbol=symbol,
            metric_type=metric_type,
            value_ms=value_ms,
        )
        self._latencies.append(metric)
    
    def record_slippage(
        self,
        symbol: str,
        side: str,
        decision_price: float,
        fill_price: float,
    ) -> None:
        """Record slippage from fill."""
        if decision_price <= 0:
            return
        
        # Calculate slippage in basis points
        if side == "buy":
            slippage_bps = ((fill_price - decision_price) / decision_price) * 10000
        else:
            slippage_bps = ((decision_price - fill_price) / decision_price) * 10000
        
        metric = SlippageMetric(
            timestamp_ms=int(time.time() * 1000),
            symbol=symbol,
            side=side,
            decision_price=decision_price,
            fill_price=fill_price,
            slippage_bps=slippage_bps,
        )
        self._slippages.append(metric)
    
    def record_trade(
        self,
        symbol: str,
        side: str,
        qty: float,
        entry_price: float,
        exit_price: float = 0.0,
        pnl_pct: float = 0.0,
        hold_time_sec: float = 0.0,
        exit_reason: str = "",
    ) -> None:
        """Record a completed trade."""
        metric = TradeMetric(
            timestamp_ms=int(time.time() * 1000),
            symbol=symbol,
            side=side,
            qty=qty,
            entry_price=entry_price,
            exit_price=exit_price,
            pnl_pct=pnl_pct,
            hold_time_sec=hold_time_sec,
            exit_reason=exit_reason,
        )
        self._trades.append(metric)
        self._trade_count += 1
        
        if pnl_pct > 0:
            self._win_count += 1
    
    def record_fill(self) -> None:
        """Record a fill event."""
        self._fill_count += 1
    
    def record_reject(self) -> None:
        """Record a reject event."""
        self._reject_count += 1
    
    def record_timeout(self) -> None:
        """Record a timeout event."""
        self._timeout_count += 1
    
    def record_kill_switch(self, reason: str) -> None:
        """Record a kill-switch trigger."""
        self._kill_switch_counts[reason] = self._kill_switch_counts.get(reason, 0) + 1
    
    def get_latency_stats(self, metric_type: Optional[str] = None) -> Dict[str, float]:
        """Get latency statistics."""
        values = [
            m.value_ms for m in self._latencies
            if metric_type is None or m.metric_type == metric_type
        ]
        
        if not values:
            return {"count": 0}
        
        return {
            "count": len(values),
            "mean_ms": statistics.mean(values),
            "median_ms": statistics.median(values),
            "p95_ms": sorted(values)[int(len(values) * 0.95)] if len(values) >= 20 else max(values),
            "max_ms": max(values),
        }
    
    def get_slippage_stats(self) -> Dict[str, float]:
        """Get slippage statistics."""
        values = [m.slippage_bps for m in self._slippages]
        
        if not values:
            return {"count": 0}
        
        return {
            "count": len(values),
            "mean_bps": statistics.mean(values),
            "median_bps": statistics.median(values),
            "p95_bps": sorted(values)[int(len(values) * 0.95)] if len(values) >= 20 else max(values),
            "max_bps": max(values),
        }
    
    def get_trade_stats(self) -> Dict[str, Any]:
        """Get trade statistics."""
        if self._trade_count == 0:
            return {"trade_count": 0}
        
        pnls = [t.pnl_pct for t in self._trades if t.pnl_pct != 0]
        hold_times = [t.hold_time_sec for t in self._trades if t.hold_time_sec > 0]
        
        stats = {
            "trade_count": self._trade_count,
            "win_count": self._win_count,
            "win_rate": self._win_count / self._trade_count if self._trade_count > 0 else 0,
        }
        
        if pnls:
            stats["avg_pnl_pct"] = statistics.mean(pnls)
            stats["total_pnl_pct"] = sum(pnls)
        
        if hold_times:
            stats["avg_hold_sec"] = statistics.mean(hold_times)
        
        return stats
    
    def get_summary(self) -> Dict[str, Any]:
        """Get full metrics summary."""
        return {
            "latency": {
                "tick_to_ack": self.get_latency_stats("tick_to_ack"),
                "ack_to_fill": self.get_latency_stats("ack_to_fill"),
            },
            "slippage": self.get_slippage_stats(),
            "trades": self.get_trade_stats(),
            "execution": {
                "fill_count": self._fill_count,
                "reject_count": self._reject_count,
                "timeout_count": self._timeout_count,
                "fill_rate": self._fill_count / (self._fill_count + self._reject_count) 
                    if (self._fill_count + self._reject_count) > 0 else 0,
            },
            "kill_switches": self._kill_switch_counts,
        }
    
    def log_summary(self) -> None:
        """Log current metrics summary."""
        summary = self.get_summary()
        
        trades = summary.get("trades", {})
        slippage = summary.get("slippage", {})
        execution = summary.get("execution", {})
        
        log.info(
            f"Metrics: trades={trades.get('trade_count', 0)} "
            f"win_rate={trades.get('win_rate', 0):.1%} "
            f"avg_slip={slippage.get('mean_bps', 0):.1f}bps "
            f"fill_rate={execution.get('fill_rate', 0):.1%}"
        )
