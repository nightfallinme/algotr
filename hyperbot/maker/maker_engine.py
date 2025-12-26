# hyperbot/maker/maker_engine.py
"""
Maker-First Micro-Reversion Engine.

Core logic for OFI spike fading in low-range markets.
"""
import time
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, Callable, Awaitable
from enum import Enum, auto

from ..ofi.coin_state import CoinState
from ..ofi.ofi_calculator import OFISignal
from ..ws.book_manager import TopOfBook
from .maker_config import MakerConfig
from ..logger import get_logger

log = get_logger(__name__)


class MakerOrderState(Enum):
    """State of maker order lifecycle."""
    NONE = auto()
    PENDING = auto()      # Order placed, waiting for fill
    FILLED = auto()       # Entry filled, position open
    EXITING = auto()      # Exit order pending
    CANCELLED = auto()    # Order cancelled (timeout)


@dataclass
class MakerPosition:
    """Tracks maker position state."""
    symbol: str
    side: str = ""              # "long" or "short"
    entry_price: float = 0.0
    size: float = 0.0
    entry_time_ms: int = 0
    state: MakerOrderState = MakerOrderState.NONE
    
    # Order tracking
    entry_order_id: str = ""
    entry_order_placed_ms: int = 0
    exit_order_id: str = ""
    exit_order_placed_ms: int = 0
    
    # Stats
    replace_count: int = 0
    
    def reset(self) -> None:
        """Reset position to empty state."""
        self.side = ""
        self.entry_price = 0.0
        self.size = 0.0
        self.entry_time_ms = 0
        self.state = MakerOrderState.NONE
        self.entry_order_id = ""
        self.entry_order_placed_ms = 0
        self.exit_order_id = ""
        self.exit_order_placed_ms = 0
        self.replace_count = 0
    
    @property
    def has_position(self) -> bool:
        return self.state == MakerOrderState.FILLED
    
    @property
    def has_pending_order(self) -> bool:
        return self.state in (MakerOrderState.PENDING, MakerOrderState.EXITING)


@dataclass  
class MakerStats:
    """Daily statistics for maker mode."""
    trades_count: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl_bps: float = 0.0
    max_drawdown_bps: float = 0.0
    fill_count: int = 0
    timeout_count: int = 0
    blocked_by_binance: int = 0
    
    def reset(self) -> None:
        """Reset daily stats."""
        self.trades_count = 0
        self.wins = 0
        self.losses = 0
        self.total_pnl_bps = 0.0
        self.max_drawdown_bps = 0.0
        self.fill_count = 0
        self.timeout_count = 0
        self.blocked_by_binance = 0


class MakerEngine:
    """
    Maker-First Micro-Reversion trading engine.
    
    This engine FADES OFI spikes in low-range markets:
    - OFI spike UP -> SELL limit (fade the buying pressure)
    - OFI spike DOWN -> BUY limit (fade the selling pressure)
    
    Key differences from taker engine:
    1. Uses LIMIT orders, not IOC
    2. INVERTS Binance veto logic (blocks if venues agree)
    3. Higher OFI threshold (wants extreme spikes to fade)
    4. Shorter timeouts and tighter stops
    """
    
    def __init__(
        self,
        config: MakerConfig,
        trade_logger=None,  # OFITradeLogger instance for logging trades
        on_place_order: Optional[Callable[..., Awaitable[str]]] = None,
        on_cancel_order: Optional[Callable[..., Awaitable[bool]]] = None,
        on_modify_order: Optional[Callable[..., Awaitable[bool]]] = None,
    ):
        self.config = config
        self._trade_logger = trade_logger  # Optional trade logger
        
        # Callbacks for order execution (set by orchestrator)
        self._on_place_order = on_place_order
        self._on_cancel_order = on_cancel_order
        self._on_modify_order = on_modify_order
        
        # Per-symbol state
        self._positions: Dict[str, MakerPosition] = {}
        self._cooldown_until: Dict[str, int] = {}
        
        # Stats
        self._stats = MakerStats()
        
        # Log rate limiting
        self._last_log_ms: Dict[str, int] = {}
    
    def get_position(self, symbol: str) -> MakerPosition:
        """Get or create position tracker for symbol."""
        if symbol not in self._positions:
            self._positions[symbol] = MakerPosition(symbol=symbol)
        return self._positions[symbol]
    
    def _is_on_cooldown(self, symbol: str, now_ms: int) -> bool:
        """Check if symbol is on cooldown."""
        return now_ms < self._cooldown_until.get(symbol, 0)
    
    def _set_cooldown(self, symbol: str, now_ms: int) -> None:
        """Set cooldown for symbol."""
        self._cooldown_until[symbol] = now_ms + self.config.cooldown_after_cancel_ms
    
    def _log_throttled(self, symbol: str, msg: str, now_ms: int, interval_ms: int = 5000) -> None:
        """Log message with throttling."""
        key = f"{symbol}:{msg[:20]}"
        if now_ms - self._last_log_ms.get(key, 0) > interval_ms:
            self._last_log_ms[key] = now_ms
            log.info(msg)
    
    async def on_tick(
        self,
        symbol: str,
        state: CoinState,
        ofi_signal: OFISignal,
        binance_ofi: float,
        now_ms: int,
        is_shadow: bool = True,
        allow_entry: bool = True,
    ) -> None:
        """
        Main tick handler for maker engine.
        
        Args:
            allow_entry: If False, only manage existing positions (graceful exit)
        """
        pos = self.get_position(symbol)
        tob = state.book.top_of_book
        
        # Check daily limits
        if self._stats.trades_count >= self.config.max_daily_trades:
            allow_entry = False
        if self._stats.total_pnl_bps <= -self.config.max_daily_loss_bps:
            allow_entry = False
        
        # === If position open, check exit ===
        if pos.has_position:
            await self._check_exit(symbol, pos, state, tob, now_ms, is_shadow)
            return

        # === If pending order, manage it ===
        if pos.state == MakerOrderState.PENDING:
            if not allow_entry:
                # Graceful cancellation (wait 2s)
                if now_ms - pos.entry_order_placed_ms > 2000:
                    if not is_shadow and self._on_cancel_order:
                        await self._on_cancel_order(pos.entry_order_id)
                    log.info(f"[{symbol}] MAKER GRACEFUL CANCEL: Regime changed")
                    pos.reset()
                return

            await self._manage_order(symbol, pos, state, tob, now_ms, is_shadow)
            return
            
        
        # === If no position, check entry (only if allowed) ===
        if allow_entry:
            if self._is_on_cooldown(symbol, now_ms):
                return
            await self._check_entry(symbol, state, ofi_signal, binance_ofi, tob, now_ms, is_shadow)
            return
    
    async def _check_entry(
        self,
        symbol: str,
        state: CoinState,
        ofi_signal: OFISignal,
        binance_ofi: float,
        tob: TopOfBook,
        now_ms: int,
        is_shadow: bool,
    ) -> None:
        """Check for maker fade entry opportunity."""
        ofi = ofi_signal.ofi_smooth
        
        # === OFI Spike Detection ===
        if abs(ofi) < self.config.ofi_spike_abs:
            return  # No spike, no trade
        
        # === Binance Veto (INVERTED for fade strategy) ===
        # Block if Binance CONFIRMS Hyper direction (real impulse, don't fade)
        if self.config.binance_confirm_blocks and abs(binance_ofi) >= self.config.binance_min_ofi:
            hyper_sign = 1 if ofi > 0 else -1
            binance_sign = 1 if binance_ofi > 0 else -1
            
            if hyper_sign == binance_sign:
                self._stats.blocked_by_binance += 1
                self._log_throttled(
                    symbol,
                    f"[{symbol}] MAKER BLOCKED: Binance confirms impulse "
                    f"(Hyp={ofi:.2f} Bin={binance_ofi:.2f})",
                    now_ms,
                )
                return
        
        # === Spread Check ===
        max_spread_bps = 3.0  # Don't trade if spread too wide
        spread_bps = (tob.spread / tob.microprice) * 10000 if tob.microprice > 0 else 999
        if spread_bps > max_spread_bps:
            return
        
        # === Determine Fade Direction ===
        if ofi > 0:
            # Positive OFI spike -> FADE SHORT (Sell Limit)
            side = "sell"
            fade_side = "short"
            entry_offset = tob.microprice * (self.config.entry_offset_bps / 10000)
            limit_price = tob.best_ask - entry_offset
        else:
            # Negative OFI spike -> FADE LONG (Buy Limit)
            side = "buy"
            fade_side = "long"
            entry_offset = tob.microprice * (self.config.entry_offset_bps / 10000)
            limit_price = tob.best_bid + entry_offset
        
        # === Calculate size ===
        size_usd = self.config.max_position_usd
        size = size_usd / tob.microprice if tob.microprice > 0 else 0
        
        if size <= 0:
            return
        
        # === Place order (or shadow log) ===
        pos = self.get_position(symbol)
        
        if is_shadow:
            log.info(
                f"[{symbol}] MAKER SHADOW ENTRY: FADE {fade_side.upper()} "
                f"limit={limit_price:.2f} size={size:.4f} "
                f"(OFI spike {ofi:+.2f}, Binance {binance_ofi:+.2f})"
            )
            # Simulate fill for shadow mode
            pos.side = fade_side
            pos.entry_price = limit_price
            pos.size = size
            pos.entry_time_ms = now_ms
            pos.state = MakerOrderState.FILLED
            self._stats.fill_count += 1
            
            # Log to trade logger
            if self._trade_logger:
                self._trade_logger.log_maker_entry(
                    symbol=symbol,
                    side=fade_side,
                    qty=size,
                    limit_price=limit_price,
                    ofi_smooth=ofi,
                    binance_ofi=binance_ofi,
                    best_bid=tob.best_bid,
                    best_ask=tob.best_ask,
                    mode="shadow",
                )
        else:
            if self._on_place_order:
                order_id = await self._on_place_order(
                    symbol=symbol,
                    side=side,
                    price=limit_price,
                    size=size,
                    order_type="limit",
                    post_only=True,  # Maker only
                )
                if order_id:
                    pos.entry_order_id = order_id
                    pos.entry_order_placed_ms = now_ms
                    pos.side = fade_side
                    pos.entry_price = limit_price
                    pos.size = size
                    pos.state = MakerOrderState.PENDING
                    log.info(
                        f"[{symbol}] MAKER ENTRY ORDER: {side.upper()} "
                        f"limit={limit_price:.2f} size={size:.4f} order_id={order_id}"
                    )
    
    async def _manage_order(
        self,
        symbol: str,
        pos: MakerPosition,
        state: CoinState,
        tob: TopOfBook,
        now_ms: int,
        is_shadow: bool,
    ) -> None:
        """Manage pending maker order (timeout, replace)."""
        
        # === Entry order pending ===
        if pos.state == MakerOrderState.PENDING:
            elapsed_sec = (now_ms - pos.entry_order_placed_ms) / 1000.0
            
            if elapsed_sec > self.config.order_timeout_sec:
                # Timeout - cancel and cooldown
                if not is_shadow and self._on_cancel_order:
                    await self._on_cancel_order(pos.entry_order_id)
                
                self._stats.timeout_count += 1
                self._set_cooldown(symbol, now_ms)
                pos.reset()
                
                log.info(f"[{symbol}] MAKER ORDER TIMEOUT: cancelled after {elapsed_sec:.1f}s")
                return
        
        # === Exit order pending ===
        elif pos.state == MakerOrderState.EXITING:
            elapsed_sec = (now_ms - pos.exit_order_placed_ms) / 1000.0
            
            if elapsed_sec > self.config.maker_exit_timeout_sec:
                # Switch to taker exit
                log.info(f"[{symbol}] MAKER EXIT TIMEOUT: switching to taker")
                # Note: In real impl, would place IOC order here
                await self._exit_taker(symbol, pos, state, tob, now_ms, is_shadow)
    
    async def _check_exit(
        self,
        symbol: str,
        pos: MakerPosition,
        state: CoinState,
        tob: TopOfBook,
        now_ms: int,
        is_shadow: bool,
    ) -> None:
        """Check exit conditions for open position."""
        
        # === Calculate P&L ===
        if pos.side == "long":
            pnl_bps = ((tob.microprice - pos.entry_price) / pos.entry_price) * 10000
            tp_price = pos.entry_price * (1 + self.config.tp_mid_offset_bps / 10000)
        else:  # short
            pnl_bps = ((pos.entry_price - tob.microprice) / pos.entry_price) * 10000
            tp_price = pos.entry_price * (1 - self.config.tp_mid_offset_bps / 10000)
        
        # === Take Profit ===
        if pnl_bps >= self.config.tp_mid_offset_bps:
            await self._exit_with_profit(symbol, pos, tob, pnl_bps, now_ms, is_shadow)
            return
        
        # === Stop Loss ===
        if pnl_bps <= -self.config.sl_bps:
            await self._exit_taker(symbol, pos, state, tob, now_ms, is_shadow, "SL")
            return
        
        # === Max Hold Time ===
        hold_sec = (now_ms - pos.entry_time_ms) / 1000.0
        if hold_sec > self.config.max_hold_sec:
            await self._exit_taker(symbol, pos, state, tob, now_ms, is_shadow, "MAX_HOLD")
            return
    
    def __init__(
        self,
        config: MakerConfig,
        trade_logger: Optional["OFITradeLogger"] = None,
        maker_fee_bps: float = 2.0,
        taker_fee_bps: float = 5.5,
    ):
        self.config = config
        self._trade_logger = trade_logger
        self.maker_fee_bps = maker_fee_bps
        self.taker_fee_bps = taker_fee_bps
        
        self._positions: Dict[str, MakerPosition] = {}
        
        # Stats
        self._stats = MakerStats()
        
        # Throttling
        self._last_log_ms: Dict[str, int] = {}
        self._last_cancel_log_ms: Dict[str, int] = {}
        self._cooldown_until: Dict[str, int] = {}
        
        # Event hooks
        self._on_place_order = None
        self._on_cancel_order = None
        
        log.info(f"MakerEngine init: Maker Fee={self.maker_fee_bps} bps, Taker Fee={self.taker_fee_bps} bps")

    def set_order_callbacks(self, on_place, on_cancel):
        """Set execution callbacks."""
        self._on_place_order = on_place
        self._on_cancel_order = on_cancel
        
    def get_position(self, symbol: str) -> MakerPosition:
        """Get position state for symbol."""
        if symbol not in self._positions:
            self._positions[symbol] = MakerPosition(symbol=symbol)
        return self._positions[symbol]

    def _log_throttled(self, symbol: str, msg: str, now_ms: int, interval_ms: int = 5000) -> None:
        """Log message with throttling."""
        key = f"{symbol}:{msg[:20]}"
        if now_ms - self._last_log_ms.get(key, 0) > interval_ms:
            self._last_log_ms[key] = now_ms
            log.info(msg)
            
    async def _exit_with_profit(
        self,
        symbol: str,
        pos: MakerPosition,
        tob: TopOfBook,
        pnl_bps: float,
        now_ms: int,
        is_shadow: bool,
    ) -> None:
        """Exit position at profit."""
        # Deduct Fees: Entry (Maker) + Exit (Maker)
        total_fee_bps = self.maker_fee_bps + self.maker_fee_bps
        net_pnl_bps = pnl_bps - total_fee_bps
        
        self._stats.trades_count += 1
        # Use Net PnL for Win/Loss determination
        if net_pnl_bps >= 0:
            self._stats.wins += 1
        else:
            self._stats.losses += 1
            
        self._stats.total_pnl_bps += net_pnl_bps
        
        log.info(f"[{symbol}] MAKER TP: +{pnl_bps:.1f} bps (Net: {net_pnl_bps:.1f} bps)")
        
        # Log to trade logger
        if self._trade_logger:
            self._trade_logger.log_maker_exit(
                symbol=symbol,
                reason="TP",
                best_bid=tob.best_bid,
                best_ask=tob.best_ask,
                pnl_bps=pnl_bps,
                entry_time_ms=pos.entry_time_ms,
                now_ms=now_ms,
                maker_fee_bps=self.maker_fee_bps,
                taker_fee_bps=self.taker_fee_bps
            )
        
        pos.reset()
    
    async def _exit_taker(
        self,
        symbol: str,
        pos: MakerPosition,
        state: CoinState,
        tob: TopOfBook,
        now_ms: int,
        is_shadow: bool,
        reason: str = "",
    ) -> None:
        """Exit position with taker order."""
        if pos.side == "long":
            pnl_bps = ((tob.microprice - pos.entry_price) / pos.entry_price) * 10000
        else:
            pnl_bps = ((pos.entry_price - tob.microprice) / pos.entry_price) * 10000
        
        # Deduct Fees: Entry (Maker) + Exit (Taker)
        total_fee_bps = self.maker_fee_bps + self.taker_fee_bps
        net_pnl_bps = pnl_bps - total_fee_bps
        
        self._stats.trades_count += 1
        if net_pnl_bps >= 0:
            self._stats.wins += 1
        else:
            self._stats.losses += 1
        self._stats.total_pnl_bps += net_pnl_bps
        
        if net_pnl_bps < self._stats.max_drawdown_bps:
            self._stats.max_drawdown_bps = net_pnl_bps
        
        log.info(f"[{symbol}] MAKER EXIT ({reason}): {pnl_bps:+.1f} bps (Net: {net_pnl_bps:+.1f} bps)")
        
        # Log to trade logger
        if self._trade_logger:
            self._trade_logger.log_maker_exit(
                symbol=symbol,
                reason=reason,
                best_bid=tob.best_bid,
                best_ask=tob.best_ask,
                pnl_bps=pnl_bps,
                entry_time_ms=pos.entry_time_ms,
                now_ms=now_ms,
                maker_fee_bps=self.maker_fee_bps,  # Pass active fees
                taker_fee_bps=self.taker_fee_bps
            )
        
        pos.reset()
    
    def on_fill(self, symbol: str, order_id: str, fill_price: float, fill_size: float) -> None:
        """Handle fill event from exchange."""
        pos = self.get_position(symbol)
        
        if pos.state == MakerOrderState.PENDING and pos.entry_order_id == order_id:
            pos.entry_price = fill_price
            pos.size = fill_size
            pos.entry_time_ms = int(time.time() * 1000)
            pos.state = MakerOrderState.FILLED
            self._stats.fill_count += 1
            log.info(f"[{symbol}] MAKER ENTRY FILLED: {fill_price:.2f} x {fill_size:.4f}")
        
        elif pos.state == MakerOrderState.EXITING and pos.exit_order_id == order_id:
            # Exit filled - position closed
            pos.reset()
            log.info(f"[{symbol}] MAKER EXIT FILLED")
    
    def get_stats(self) -> MakerStats:
        """Get current session statistics."""
        return self._stats
    
    def reset_stats(self) -> None:
        """Reset statistics (e.g., daily reset)."""
        self._stats.reset()

    async def force_exit(
        self,
        symbol: str,
        state: CoinState,
        tob: TopOfBook,
        now_ms: int,
        is_shadow: bool
    ) -> None:
        """Force exit position (e.g., on regime change)."""
        pos = self._positions.get(symbol)
        if not pos:
            return

        # Cancel pending entry
        if pos.state == MakerOrderState.PENDING:
            if not is_shadow and self._on_cancel_order:
                await self._on_cancel_order(pos.entry_order_id)
            log.info(f"[{symbol}] MAKER FORCE EXIT: Cancelled pending entry")
            pos.reset()
            return
            
        # Exit open position
        if pos.has_position:
            await self._exit_taker(
                symbol, 
                pos, 
                state, 
                tob, 
                now_ms, 
                is_shadow, 
                reason="REGIME_CHANGE"
            )
