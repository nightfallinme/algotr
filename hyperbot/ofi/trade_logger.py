# hyperbot/ofi/trade_logger.py
"""
OFI Trade Logger - JSON-based trade recording for analysis.

Logs all entries and exits with full context for later analysis:
- Entry: OFI signal, thresholds, filters, book state
- Exit: Reason, PnL breakdown, costs, duration

Output: ofi_trades.json (human-readable, append-only)
"""
import json
import os
from datetime import datetime
from dataclasses import dataclass, asdict, field
from typing import Optional, Dict, Any, List

from ..logger import get_logger

log = get_logger(__name__)


@dataclass
class OFITradeEntry:
    """Entry snapshot for a trade."""
    timestamp: str
    symbol: str
    side: str  # "buy" or "sell"
    qty: float
    limit_price: float
    
    # OFI Signal
    ofi_smooth: float
    ofi_threshold: float  # Effective threshold used
    
    # Book State
    best_bid: float
    best_ask: float
    spread: float
    spread_pct: float
    
    # Filters
    vol_z: float
    spread_z: float
    micro_atr: float
    
    # Costs (estimated)
    max_slip: float
    est_fee_pct: float


@dataclass
class OFITradeExit:
    """Exit snapshot for a trade."""
    timestamp: str
    reason: str  # "ofi_flip", "vol_spike", "stop_loss", "take_profit", "max_hold", etc.
    
    # Book State at Exit
    best_bid: float
    best_ask: float
    spread: float
    spread_pct: float
    
    # PnL Breakdown
    gross_pnl_pct: float
    spread_cost_pct: float
    fee_cost_pct: float
    slippage_est_pct: float
    net_pnl_pct: float
    
    # Timing
    hold_duration_sec: float


@dataclass
class OFITradeRecord:
    """Complete trade record with entry and exit."""
    trade_id: int
    symbol: str
    side: str
    mode: str  # "shadow", "paper", "live"
    
    entry: OFITradeEntry
    exit: Optional[OFITradeExit] = None
    
    # Final result (computed on exit)
    result: Optional[str] = None  # "WIN", "LOSS", "BREAK_EVEN"
    net_pnl_pct: Optional[float] = None


class OFITradeLogger:
    """
    Trade logger for OFI scalper.
    
    Writes detailed JSON records for analysis.
    """
    
    def __init__(self, output_file: str = "ofi_trades.json"):
        self.output_file = output_file
        self.trades: List[OFITradeRecord] = []
        self._trade_counter = 0
        self._pending: Dict[str, OFITradeRecord] = {}  # symbol -> pending trade
        
        # Load existing trades
        self._load()
        log.info(f"OFI Trade Logger initialized: {len(self.trades)} historical trades")
    
    def _load(self) -> None:
        """Load existing trades from file."""
        if os.path.exists(self.output_file):
            try:
                with open(self.output_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._trade_counter = data.get("trade_counter", 0)
                    
                    # Reconstruct trade records
                    for t in data.get("trades", []):
                        entry_data = t.pop("entry", {})
                        exit_data = t.pop("exit", None)
                        
                        entry = OFITradeEntry(**entry_data) if entry_data else None
                        exit = OFITradeExit(**exit_data) if exit_data else None
                        
                        record = OFITradeRecord(
                            entry=entry,
                            exit=exit,
                            **t
                        )
                        self.trades.append(record)
            except Exception as e:
                log.warning(f"Could not load trades: {e}")
    
    def _save(self) -> None:
        """Save all trades to file."""
        try:
            data = {
                "trade_counter": self._trade_counter,
                "last_updated": datetime.now().isoformat(),
                "summary": self._compute_summary(),
                "trades": [asdict(t) for t in self.trades]
            }
            with open(self.output_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str)
        except Exception as e:
            log.error(f"Could not save trades: {e}")
    
    def _compute_summary(self) -> Dict[str, Any]:
        """Compute summary statistics."""
        completed = [t for t in self.trades if t.exit is not None]
        if not completed:
            return {"total": 0}
        
        wins = sum(1 for t in completed if t.result == "WIN")
        losses = sum(1 for t in completed if t.result == "LOSS")
        
        net_pnls = [t.net_pnl_pct or 0 for t in completed]
        
        # Exit reason breakdown
        exit_reasons = {}
        for t in completed:
            reason = t.exit.reason if t.exit else "unknown"
            exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
        
        return {
            "total": len(completed),
            "wins": wins,
            "losses": losses,
            "win_rate": wins / len(completed) if completed else 0,
            "avg_net_pnl_pct": sum(net_pnls) / len(net_pnls) if net_pnls else 0,
            "total_net_pnl_pct": sum(net_pnls),
            "exit_reasons": exit_reasons,
        }
    
    def log_entry(
        self,
        symbol: str,
        side: str,
        qty: float,
        limit_price: float,
        ofi_smooth: float,
        ofi_threshold: float,
        best_bid: float,
        best_ask: float,
        spread: float,
        vol_z: float,
        spread_z: float,
        micro_atr: float,
        max_slip: float,
        taker_fee_bps: float,
        mode: str = "shadow",
    ) -> None:
        """Log trade entry."""
        self._trade_counter += 1
        
        microprice = (best_bid + best_ask) / 2 if best_ask > 0 else 1
        spread_pct = (spread / microprice) * 100 if microprice > 0 else 0
        
        entry = OFITradeEntry(
            timestamp=datetime.now().isoformat(),
            symbol=symbol,
            side=side,
            qty=qty,
            limit_price=limit_price,
            ofi_smooth=ofi_smooth,
            ofi_threshold=ofi_threshold,
            best_bid=best_bid,
            best_ask=best_ask,
            spread=spread,
            spread_pct=spread_pct,
            vol_z=vol_z,
            spread_z=spread_z,
            micro_atr=micro_atr,
            max_slip=max_slip,
            est_fee_pct=(taker_fee_bps / 100) * 2,
        )
        
        record = OFITradeRecord(
            trade_id=self._trade_counter,
            symbol=symbol,
            side=side,
            mode=mode,
            entry=entry,
        )
        
        self._pending[symbol] = record
        log.debug(f"Trade #{self._trade_counter} entry logged: {symbol} {side}")
    
    def log_exit(
        self,
        symbol: str,
        reason: str,
        best_bid: float,
        best_ask: float,
        spread: float,
        gross_pnl_pct: float,
        spread_cost_pct: float,
        fee_cost_pct: float,
        slippage_est_pct: float,
        net_pnl_pct: float,
        entry_time_ms: int,
        now_ms: int,
    ) -> None:
        """Log trade exit."""
        if symbol not in self._pending:
            log.warning(f"log_exit called but no pending trade for {symbol}")
            return
        
        record = self._pending.pop(symbol)
        
        microprice = (best_bid + best_ask) / 2 if best_ask > 0 else 1
        spread_pct = (spread / microprice) * 100 if microprice > 0 else 0
        hold_sec = (now_ms - entry_time_ms) / 1000.0 if entry_time_ms > 0 else 0
        
        exit = OFITradeExit(
            timestamp=datetime.now().isoformat(),
            reason=reason,
            best_bid=best_bid,
            best_ask=best_ask,
            spread=spread,
            spread_pct=spread_pct,
            gross_pnl_pct=gross_pnl_pct,
            spread_cost_pct=spread_cost_pct,
            fee_cost_pct=fee_cost_pct,
            slippage_est_pct=slippage_est_pct,
            net_pnl_pct=net_pnl_pct,
            hold_duration_sec=hold_sec,
        )
        
        record.exit = exit
        record.net_pnl_pct = net_pnl_pct
        
        # Determine result
        if net_pnl_pct > 0.001:  # > 0.001% = WIN
            record.result = "WIN"
        elif net_pnl_pct < -0.001:  # < -0.001% = LOSS
            record.result = "LOSS"
        else:
            record.result = "BREAK_EVEN"
        
        self.trades.append(record)
        self._save()
        
        log.debug(f"Trade #{record.trade_id} exit logged: {reason} net={net_pnl_pct:+.3f}%")
    
    def get_recent_trades(self, n: int = 10) -> List[OFITradeRecord]:
        """Get most recent N trades."""
        return self.trades[-n:] if self.trades else []
    
    def get_exit_reason_stats(self) -> Dict[str, Dict[str, Any]]:
        """Get statistics grouped by exit reason."""
        stats = {}
        
        for trade in self.trades:
            if trade.exit is None:
                continue
            
            reason = trade.exit.reason
            if reason not in stats:
                stats[reason] = {
                    "count": 0,
                    "wins": 0,
                    "losses": 0,
                    "total_net_pnl": 0,
                    "avg_hold_sec": 0,
                }
            
            s = stats[reason]
            s["count"] += 1
            s["total_net_pnl"] += trade.net_pnl_pct or 0
            s["avg_hold_sec"] += trade.exit.hold_duration_sec
            
            if trade.result == "WIN":
                s["wins"] += 1
            elif trade.result == "LOSS":
                s["losses"] += 1
        
        # Compute averages
        for reason, s in stats.items():
            if s["count"] > 0:
                s["avg_net_pnl"] = s["total_net_pnl"] / s["count"]
                s["avg_hold_sec"] = s["avg_hold_sec"] / s["count"]
                s["win_rate"] = s["wins"] / s["count"]
        
        return stats
    
    # === MAKER TRADE LOGGING ===
    
    def log_maker_entry(
        self,
        symbol: str,
        side: str,  # "long" or "short"
        qty: float,
        limit_price: float,
        ofi_smooth: float,
        binance_ofi: float,
        best_bid: float,
        best_ask: float,
        mode: str = "shadow",
    ) -> None:
        """Log maker fade entry."""
        self._trade_counter += 1
        
        spread = best_ask - best_bid
        microprice = (best_bid + best_ask) / 2 if best_ask > 0 else 1
        spread_pct = (spread / microprice) * 100 if microprice > 0 else 0
        
        # Convert "long"/"short" to "buy"/"sell" for consistency
        trade_side = "buy" if side == "long" else "sell"
        
        entry = OFITradeEntry(
            timestamp=datetime.now().isoformat(),
            symbol=symbol,
            side=trade_side,
            qty=qty,
            limit_price=limit_price,
            ofi_smooth=ofi_smooth,
            ofi_threshold=0.35,  # maker spike threshold
            best_bid=best_bid,
            best_ask=best_ask,
            spread=spread,
            spread_pct=spread_pct,
            vol_z=0.0,  # Not tracked for maker
            spread_z=0.0,
            micro_atr=0.0,
            max_slip=0.0,
            est_fee_pct=0.0,  # 0.0 bps Maker entry fee
        )
        
        record = OFITradeRecord(
            trade_id=self._trade_counter,
            symbol=symbol,
            side=trade_side,
            mode=f"maker_{mode}",  # Prefix with "maker_" to distinguish
            entry=entry,
        )
        
        # Store with maker prefix
        self._pending[f"maker_{symbol}"] = record
        log.debug(f"Maker Trade #{self._trade_counter} entry logged: {symbol} {side}")
    
    def log_maker_exit(
        self,
        symbol: str,
        reason: str,  # "TP", "SL", "MAX_HOLD", "REGIME_CHANGE"
        best_bid: float,
        best_ask: float,
        pnl_bps: float,
        entry_time_ms: int,
        now_ms: int,
        maker_fee_bps: float = 0.0,
        taker_fee_bps: float = 2.5,
    ) -> None:
        """Log maker fade exit."""
        key = f"maker_{symbol}"
        if key not in self._pending:
            log.debug(f"log_maker_exit called but no pending maker trade for {symbol}")
            return
        
        record = self._pending.pop(key)
        
        spread = best_ask - best_bid
        microprice = (best_bid + best_ask) / 2 if best_ask > 0 else 1
        spread_pct = (spread / microprice) * 100 if microprice > 0 else 0
        hold_sec = (now_ms - entry_time_ms) / 1000.0 if entry_time_ms > 0 else 0
        
        # Convert bps to pct
        gross_pnl_pct = pnl_bps / 100.0
        
        # Fee calc (Dynamic based on config):
        # Entry Fee: Always Maker (maker_fee_bps) - Deducted implicitly or tracked separately? 
        # Actually, pnl_bps passed here is usually Gross PnL from price diff.
        # We need to deduct BOTH Entry and Exit fees for Net PnL.
        
        # 1. Entry Fee (Maker)
        entry_fee_pct = maker_fee_bps / 100.0
        
        # 2. Exit Fee
        # TP = Maker Exit, Others = Taker Exit
        exit_fee_pct = 0.0
        if reason.upper() == "TP":
            exit_fee_pct = maker_fee_bps / 100.0
        else:
            exit_fee_pct = taker_fee_bps / 100.0
            
        total_fee_pct = entry_fee_pct + exit_fee_pct
        net_pnl_pct = gross_pnl_pct - total_fee_pct
        
        exit = OFITradeExit(
            timestamp=datetime.now().isoformat(),
            reason=f"maker_{reason.lower()}",  # "maker_tp", "maker_sl", etc.
            best_bid=best_bid,
            best_ask=best_ask,
            spread=spread,
            spread_pct=spread_pct,
            gross_pnl_pct=gross_pnl_pct,
            spread_cost_pct=0.0,  # Maker = no spread cost
            fee_cost_pct=total_fee_pct,
            slippage_est_pct=0.0,
            net_pnl_pct=net_pnl_pct,
            hold_duration_sec=hold_sec,
        )
        
        record.exit = exit
        record.net_pnl_pct = net_pnl_pct
        
        # Determine result
        if net_pnl_pct > 0.001:
            record.result = "WIN"
        elif net_pnl_pct < -0.001:
            record.result = "LOSS"
        else:
            record.result = "BREAK_EVEN"
        
        self.trades.append(record)
        self._save()
        
        log.debug(f"Maker Trade #{record.trade_id} exit logged: {reason} net={net_pnl_pct:+.3f}%")

