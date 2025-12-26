# hyperbot/ofi_orchestrator.py
"""
OFI Orchestrator - Main event loop for OFI scalping.

This is the async WebSocket-driven orchestrator that:
1. Receives real-time book/trade updates
2. Calculates OFI and filters
3. Makes entry/exit decisions
4. Routes to execution with safety controls
5. Handles order events

Supports:
- Shadow mode (signal generation without execution)
- Paper mode (small size real execution)
"""
import asyncio
import time
from dataclasses import dataclass
from typing import Optional, Dict, Any, List
from enum import Enum, auto

from .ws.ws_manager import WSManager, WSState, WSConfig
from .ws.binance_ws import BinanceWSManager, BinanceWSState
from .ws.bybit_ws import BybitWSManager, BybitWSState
from .ws.book_manager import BookManager, BookState, TopOfBook
from .ws.trade_stream import TradeStreamManager
from .ofi.coin_state import CoinState, DecisionSnapshot
from .ofi.ofi_calculator import OFICalculator, OFISignal
from .ofi.filters import VolatilityFilter, SpreadFilter, NetEdgeChecker, FilterStatus
from .ofi.trade_logger import OFITradeLogger, OFITradeRecord
from .analytics import Analytics
from .execution.order_manager import OrderManager, OrderIntent, Order
from .execution.kill_switch import KillSwitch, KillReason
from .maker import MakerConfig
from .maker.maker_engine import MakerEngine
from .maker.regime_detector import RegimeDetector, MarketRegime
from .config import Config
from .logger import get_logger

log = get_logger(__name__)


class ExecutionMode(Enum):
    """Orchestrator execution mode."""
    SHADOW = auto()   # Calculate signals, log only
    PAPER = auto()    # Small size real execution
    LIVE = auto()     # Full execution


@dataclass
class OFIConfig:
    """OFI scalping configuration."""
    # OFI thresholds
    ofi_long_threshold: float = 0.20
    ofi_short_threshold: float = -0.20
    ofi_window_sec: float = 2.0
    ofi_flip_debounce: int = 3
    
    # OFI Confirmation & Integral
    entry_confirm_ticks: int = 3
    entry_confirm_min_hits: int = 2
    ofi_area_window_sec: float = 3.0
    ofi_area_long: float = 0.45
    ofi_area_short: float = -0.45
    confirm_integral_mode: str = "AND"  # "AND" or "OR"
    
    # Microprice Drift Filter
    drift_ticks_lookback: int = 3
    drift_bypass_ofi_abs: float = 0.30
    micro_drift_spread_mult: float = 0.15
    
    # Trade Imbalance
    trade_imbalance_threshold: float = 0.55
    trade_imbalance_min_trades: int = 15  # Skip if fewer trades in window
    
    # Adaptive thresholds
    ofi_adaptive_threshold: bool = True
    ofi_vol_clamp_min: float = 0.8
    ofi_vol_clamp_max: float = 1.3
    min_ofi_abs: float = 0.20
    stoprun_cooldown_sec: float = 3.0
    extreme_ofi_mult: float = 2.0
    
    # Exit gating
    exit_require_net_nonnegative_for_flip: bool = True
    exit_require_net_nonnegative_for_vol_spike: bool = True
    exit_require_net_nonnegative_for_spread_spike: bool = True
    est_slippage_bps: float = 1.5
    
    # Volatility
    vol_window_ticks: int = 50
    volz_cutoff: float = 1.5
    volz_unblock: float = 1.3
    volz_exit_cutoff: float = 1.8
    vol_spike_min_hold_sec: float = 3.0
    
    # Spread
    spreadz_cutoff: float = 2.0
    spreadz_unblock: float = 1.7
    spreadz_exit_cutoff: float = 4.0  # Separate cutoff for exit (higher tolerance)
    hard_spread_stop_pct: float = 0.0002
    
    # Execution
    cooldown_ms: int = 200
    max_slip_spread_mult: float = 1.5
    max_slip_atr_mult: float = 0.25
    edge_buffer_bps: float = 1.5
    edge_atr_mult: float = 1.3
    
    # Risk
    tp_pct: float = 0.0010
    sl_pct: float = 0.0020
    max_hold_sec: float = 300.0
    taker_fee_bps: float = 2.5
    maker_fee_bps: float = 0.0  # Added for Bybit (usually negative or low)
    position_size_usd: float = 100.0
    
    # Mode
    mode: ExecutionMode = ExecutionMode.SHADOW
    
    # Filter Toggles
    filter_stoprun_enabled: bool = True
    filter_confirmation_enabled: bool = True
    filter_integral_enabled: bool = True
    filter_trade_imbalance_enabled: bool = True
    filter_drift_enabled: bool = True
    filter_edge_enabled: bool = True
    filter_vol_enabled: bool = True
    filter_spread_enabled: bool = True


class OFIOrchestrator:
    """
    WebSocket-driven OFI scalping orchestrator.
    
    Usage:
        orch = OFIOrchestrator(config, symbols=["ETH"])
        await orch.run()
    """
    
    def __init__(
        self,
        config: Config,
        symbols: List[str],
        execution_mode: ExecutionMode = ExecutionMode.SHADOW,
    ):
        self.config = config
        self.symbols = symbols
        self.mode = execution_mode
        
        # Load OFI config
        self.ofi_config = self._load_ofi_config()
        
        # Per-symbol state
        self._states: Dict[str, CoinState] = {}
        for symbol in symbols:
            self._states[symbol] = CoinState(symbol=symbol)
        
        # Calculators
        self._ofi_calc = OFICalculator(window_sec=self.ofi_config.ofi_window_sec)
        self._vol_filter = VolatilityFilter(
            window_ticks=self.ofi_config.vol_window_ticks,
            cutoff=self.ofi_config.volz_cutoff,
            unblock_cutoff=self.ofi_config.volz_unblock,
        )
        self._spread_filter = SpreadFilter(
            cutoff=self.ofi_config.spreadz_cutoff,
            unblock_cutoff=self.ofi_config.spreadz_unblock,
        )
        self._edge_checker = NetEdgeChecker(
            target_tp_bps=self.ofi_config.tp_pct * 10000,
            taker_fee_bps=self.ofi_config.taker_fee_bps,
        )
        
        # Execution
        self._order_manager = OrderManager(
            on_fill=self._on_order_fill,
            on_terminal=self._on_order_terminal,
        )
        self._kill_switch = KillSwitch(on_trigger=self._on_kill_switch)
        self._trade_logger = OFITradeLogger(output_file="ofi_trades.json")
        self._analytics = Analytics()
        
        # WebSocket
        self._ws: Optional[WSManager] = None
        
        # Control
        self._running = False
        self._last_watchdog_ms = 0
        self._last_status_log_ms = 0
        
        # OFI flip debounce counter per symbol
        # OFI flip debounce counter per symbol
        self._flip_counter: Dict[str, int] = {}
        
        # Log rate limiting: (symbol, reason) -> timestamp_ms
        self._last_reject_log_ms: Dict[str, int] = {}
        
        # Binance cross-venue context
        self._binance_ws: Optional[BinanceWSManager] = None
        self._binance_states: Dict[str, CoinState] = {}
        self._binance_enabled = config.as_dict().get("binance", {}).get("enabled", False)
        self._binance_veto_enabled = config.as_dict().get("binance", {}).get("veto_enabled", True)
        self._binance_veto_min_ofi = config.as_dict().get("binance", {}).get("veto_min_ofi", 0.10)
        
        if self._binance_enabled:
            for symbol in symbols:
                self._binance_states[symbol] = CoinState(symbol=symbol)
        
        # Maker mode (low-range reversion)
        self._maker_config = MakerConfig.from_dict(config.as_dict().get("maker_mode", {}))
        self._maker_engine: Optional[MakerEngine] = None
        self._regime_detector: Optional[RegimeDetector] = None
        
        if self._maker_config.enabled:
            # Pass trade logger to maker engine
            self._maker_engine = MakerEngine(
                config=self._maker_config,
                trade_logger=self._trade_logger,
                maker_fee_bps=self.ofi_config.maker_fee_bps, # Pass config fees
                taker_fee_bps=self.ofi_config.taker_fee_bps  # Pass config fees
            )
            self._regime_detector = RegimeDetector(
                vol_filter=self._vol_filter,
                spread_filter=self._spread_filter,
                config=self._maker_config,
            )
            log.info("Maker reversion mode enabled")
        
        # Exchange selection (hyperliquid or bybit)
        exchange_cfg = config.as_dict().get("exchange", {})
        self._exchange_type = exchange_cfg.get("type", "hyperliquid").lower()
        self._exchange_testnet = exchange_cfg.get("testnet", False)
        
        # Bybit WS manager (used when exchange=bybit)
        self._bybit_ws: Optional[BybitWSManager] = None
    
    def _load_ofi_config(self) -> OFIConfig:
        """Load OFI config from Config object."""
        ofi_cfg = self.config.as_dict().get("ofi", {})
        cfg = OFIConfig()
        
        # Override with config values
        cfg.ofi_long_threshold = ofi_cfg.get("ofi_long_threshold", cfg.ofi_long_threshold)
        cfg.ofi_short_threshold = ofi_cfg.get("ofi_short_threshold", cfg.ofi_short_threshold)
        cfg.ofi_window_sec = ofi_cfg.get("ofi_window_sec", cfg.ofi_window_sec)
        cfg.ofi_flip_debounce = ofi_cfg.get("ofi_flip_debounce", cfg.ofi_flip_debounce)
        cfg.ofi_adaptive_threshold = ofi_cfg.get("ofi_adaptive_threshold", cfg.ofi_adaptive_threshold)
        cfg.ofi_vol_clamp_min = ofi_cfg.get("ofi_vol_clamp_min", cfg.ofi_vol_clamp_min)
        cfg.ofi_vol_clamp_max = ofi_cfg.get("ofi_vol_clamp_max", cfg.ofi_vol_clamp_max)
        cfg.min_ofi_abs = ofi_cfg.get("min_ofi_abs", cfg.min_ofi_abs)
        
        # New filters
        cfg.entry_confirm_ticks = ofi_cfg.get("entry_confirm_ticks", cfg.entry_confirm_ticks)
        cfg.entry_confirm_min_hits = ofi_cfg.get("entry_confirm_min_hits", cfg.entry_confirm_min_hits)
        cfg.ofi_area_window_sec = ofi_cfg.get("ofi_area_window_sec", cfg.ofi_area_window_sec)
        cfg.ofi_area_long = ofi_cfg.get("ofi_area_long", cfg.ofi_area_long)
        cfg.ofi_area_short = ofi_cfg.get("ofi_area_short", cfg.ofi_area_short)
        cfg.confirm_integral_mode = ofi_cfg.get("confirm_integral_mode", cfg.confirm_integral_mode).upper()
        
        cfg.drift_ticks_lookback = ofi_cfg.get("drift_ticks_lookback", cfg.drift_ticks_lookback)
        cfg.drift_bypass_ofi_abs = ofi_cfg.get("drift_bypass_ofi_abs", cfg.drift_bypass_ofi_abs)
        cfg.micro_drift_spread_mult = ofi_cfg.get("micro_drift_spread_mult", cfg.micro_drift_spread_mult)
        cfg.trade_imbalance_threshold = ofi_cfg.get("trade_imbalance_threshold", cfg.trade_imbalance_threshold)
        cfg.trade_imbalance_min_trades = ofi_cfg.get("trade_imbalance_min_trades", cfg.trade_imbalance_min_trades)
        
        cfg.stoprun_cooldown_sec = ofi_cfg.get("stoprun_cooldown_sec", cfg.stoprun_cooldown_sec)
        cfg.extreme_ofi_mult = ofi_cfg.get("extreme_ofi_mult", cfg.extreme_ofi_mult)
        
        cfg.edge_buffer_bps = ofi_cfg.get("edge_buffer_bps", cfg.edge_buffer_bps)
        cfg.edge_atr_mult = ofi_cfg.get("edge_atr_mult", cfg.edge_atr_mult)
        
        # Exit gating
        cfg.exit_require_net_nonnegative_for_flip = ofi_cfg.get("exit_require_net_nonnegative_for_flip", cfg.exit_require_net_nonnegative_for_flip)
        cfg.exit_require_net_nonnegative_for_vol_spike = ofi_cfg.get("exit_require_net_nonnegative_for_vol_spike", cfg.exit_require_net_nonnegative_for_vol_spike)
        cfg.exit_require_net_nonnegative_for_spread_spike = ofi_cfg.get("exit_require_net_nonnegative_for_spread_spike", cfg.exit_require_net_nonnegative_for_spread_spike)
        cfg.est_slippage_bps = ofi_cfg.get("est_slippage_bps", cfg.est_slippage_bps)
        
        # Volatility
        cfg.vol_window_ticks = ofi_cfg.get("vol_window_ticks", cfg.vol_window_ticks)
        cfg.volz_cutoff = ofi_cfg.get("volz_cutoff", cfg.volz_cutoff)
        cfg.volz_unblock = ofi_cfg.get("volz_unblock", cfg.volz_unblock)
        cfg.volz_exit_cutoff = ofi_cfg.get("volz_exit_cutoff", cfg.volz_exit_cutoff)
        cfg.vol_spike_min_hold_sec = ofi_cfg.get("vol_spike_min_hold_sec", cfg.vol_spike_min_hold_sec)
        
        # Spread
        cfg.spreadz_cutoff = ofi_cfg.get("spreadz_cutoff", cfg.spreadz_cutoff)
        cfg.spreadz_unblock = ofi_cfg.get("spreadz_unblock", cfg.spreadz_unblock)
        cfg.spreadz_exit_cutoff = ofi_cfg.get("spreadz_exit_cutoff", cfg.spreadz_exit_cutoff)
        cfg.hard_spread_stop_pct = ofi_cfg.get("hard_spread_stop_pct", cfg.hard_spread_stop_pct)
        cfg.cooldown_ms = ofi_cfg.get("cooldown_ms", cfg.cooldown_ms)
        cfg.max_slip_spread_mult = ofi_cfg.get("max_slip_spread_mult", cfg.max_slip_spread_mult)
        cfg.max_slip_atr_mult = ofi_cfg.get("max_slip_atr_mult", cfg.max_slip_atr_mult)
        cfg.tp_pct = ofi_cfg.get("tp_pct", cfg.tp_pct)
        cfg.sl_pct = ofi_cfg.get("sl_pct", cfg.sl_pct)
        cfg.max_hold_sec = ofi_cfg.get("max_hold_sec", cfg.max_hold_sec)
        cfg.taker_fee_bps = ofi_cfg.get("taker_fee_bps", cfg.taker_fee_bps)
        cfg.maker_fee_bps = ofi_cfg.get("maker_fee_bps", cfg.maker_fee_bps) # Load config value
        cfg.position_size_usd = ofi_cfg.get("position_size_usd", cfg.position_size_usd)
        
        mode_str = ofi_cfg.get("mode", "shadow").lower()
        if mode_str == "live":
            cfg.mode = ExecutionMode.LIVE
        elif mode_str == "paper":
            cfg.mode = ExecutionMode.PAPER
        else:
            cfg.mode = ExecutionMode.SHADOW
        
        # Filter Toggles (nested under 'filters' key)
        filters = ofi_cfg.get("filters", {})
        cfg.filter_stoprun_enabled = filters.get("stoprun_enabled", cfg.filter_stoprun_enabled)
        cfg.filter_confirmation_enabled = filters.get("confirmation_enabled", cfg.filter_confirmation_enabled)
        cfg.filter_integral_enabled = filters.get("integral_enabled", cfg.filter_integral_enabled)
        cfg.filter_trade_imbalance_enabled = filters.get("trade_imbalance_enabled", cfg.filter_trade_imbalance_enabled)
        cfg.filter_drift_enabled = filters.get("drift_enabled", cfg.filter_drift_enabled)
        cfg.filter_edge_enabled = filters.get("edge_enabled", cfg.filter_edge_enabled)
        cfg.filter_vol_enabled = filters.get("vol_filter_enabled", cfg.filter_vol_enabled)
        cfg.filter_spread_enabled = filters.get("spread_filter_enabled", cfg.filter_spread_enabled)
        
        return cfg
    
    async def run(self) -> None:
        """Main run loop."""
        log.info(f"OFI Orchestrator starting in {self.mode.name} mode")
        log.info(f"Symbols: {self.symbols}")
        log.info(f"Exchange: {self._exchange_type.upper()}")
        
        tasks = []
        
        # Create appropriate WebSocket manager based on exchange type
        if self._exchange_type == "bybit":
            # Use Bybit WebSocket
            self._bybit_ws = BybitWSManager(
                symbols=self.symbols,
                testnet=self._exchange_testnet,
                on_book_update=self._on_bybit_book_update,
                on_trade=self._on_bybit_trade,
                on_state_change=self._on_bybit_state_change,
            )
            tasks.append(self._bybit_ws.start())
            log.info(f"Bybit WS {'testnet' if self._exchange_testnet else 'mainnet'} mode")
        else:
            # Default: Use Hyperliquid WebSocket
            self._ws = WSManager(
                symbols=self.symbols,
                on_state_change=self._on_ws_state_change,
            )
            
            # Set up handlers
            self._ws.on_book_snapshot = self._on_book_snapshot
            self._ws.on_book_update = self._on_book_update
            self._ws.on_trade = self._on_trade
            
            tasks.append(self._ws.run())
        
        self._running = True
        
        # Add watchdog
        tasks.append(self._watchdog_loop())
        
        # Add Binance WS if enabled (context provider for both exchanges)
        if self._binance_enabled:
            self._binance_ws = BinanceWSManager(symbols=self.symbols)
            self._binance_ws.on_book_update = self._on_binance_book_update
            self._binance_ws.on_trade = self._on_binance_trade
            tasks.append(self._binance_ws.run())
            log.info("Binance cross-venue context enabled")
        
        # Run all tasks
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            log.info("Orchestrator cancelled")
        finally:
            await self.stop()
    
    async def stop(self) -> None:
        """Stop the orchestrator."""
        self._running = False
        if self._ws:
            await self._ws.stop()
        if self._bybit_ws:
            await self._bybit_ws.stop()
        if self._binance_ws:
            await self._binance_ws.close()
        
        # Save analytics
        self._analytics.save()
        log.info("OFI Orchestrator stopped")
    
    # === WebSocket Event Handlers ===
    
    def _on_ws_state_change(self, old: WSState, new: WSState) -> None:
        """Handle WS state changes (Hyperliquid)."""
        if new == WSState.RECONNECTING:
            # Pause trading on all symbols
            for symbol, state in self._states.items():
                self._kill_switch.trigger(
                    symbol, KillReason.WS_DISCONNECT, "WebSocket reconnecting"
                )
                state.reset_buffers()
        
        elif new == WSState.READY:
            log.info("System ready - trading enabled")
    
    def _on_bybit_state_change(self, old: BybitWSState, new: BybitWSState) -> None:
        """Handle WS state changes (Bybit)."""
        if new == BybitWSState.DISCONNECTED:
            # Pause trading on all symbols
            for symbol, state in self._states.items():
                self._kill_switch.trigger(
                    symbol, KillReason.WS_DISCONNECT, "Bybit WebSocket disconnected"
                )
                state.reset_buffers()
        
        elif new == BybitWSState.READY:
            log.info("Bybit system ready - trading enabled")
    
    def _on_bybit_book_update(self, symbol: str, data: Dict) -> None:
        """Handle Bybit orderbook update - lightweight L1 handler."""
        state = self._states.get(symbol)
        if not state:
            return
        
        now_ms = int(time.time() * 1000)
        
        try:
            # BybitWSManager sends direct values: best_bid, bid_size, best_ask, ask_size
            best_bid = data.get("best_bid", 0)
            bid_size = data.get("bid_size", 0)
            best_ask = data.get("best_ask", 0)
            ask_size = data.get("ask_size", 0)
            
            if best_bid <= 0 or best_ask <= 0 or best_ask <= best_bid:
                return
            
            # Get previous values for delta calculation
            prev_tob = state.book.top_of_book
            prev_bid_size = prev_tob.bid_size if prev_tob.bid_size > 0 else bid_size
            prev_ask_size = prev_tob.ask_size if prev_tob.ask_size > 0 else ask_size
            
            # Force book to ready state if uninitialized (Bybit doesn't have snapshot flow)
            if state.book.state == BookState.UNINITIALIZED:
                state.book.force_ready()
            
            # Directly update top-of-book in BookManager
            state.book._tob = TopOfBook(
                best_bid=best_bid,
                best_ask=best_ask,
                bid_size=bid_size,
                ask_size=ask_size,
                timestamp_ms=now_ms,
            )
            state.book._update_count += 1
            
            # Calculate deltas
            delta_bid = bid_size - prev_bid_size
            delta_ask = ask_size - prev_ask_size
            
            # Update feature buffers
            tob = state.book.top_of_book
            state.microprice_buf.push(tob.microprice)
            state.spread_buf.push(tob.spread)
            state.book_bid_deltas.add(now_ms, delta_bid)
            state.book_ask_deltas.add(now_ms, delta_ask)
            
            # Update microATR
            if state.microprice_buf.size >= 2:
                prev_micro = state.microprice_buf.get(-2)
                curr_micro = tob.microprice
                delta_micro = abs(curr_micro - prev_micro)
                state.micro_atr_ema.update(delta_micro)
            
            # Check if kill-switched
            if self._kill_switch.check_cooldown(symbol, now_ms):
                return
            
            # Update OFI history
            ofi_signal = self._ofi_calc.compute(state, now_ms)
            state.ofi_history.append((now_ms, ofi_signal.ofi_smooth))
            state.ofi_smooth = ofi_signal.ofi_smooth
            state.price_history.append((now_ms, tob.microprice))
            
            # Decision logic
            asyncio.create_task(self._maybe_decide(symbol, state, now_ms))
            
        except Exception as e:
            log.debug(f"[Bybit] Book update error: {e}")
    
    def _on_bybit_trade(self, symbol: str, data: Dict) -> None:
        """Handle Bybit trade update."""
        state = self._states.get(symbol)
        if not state:
            return
        
        # Parse and add to trade stream (same format as Hyperliquid after conversion)
        state.trades.on_trade(data)
    
    async def _on_book_snapshot(self, symbol: str, data: Dict) -> None:
        """Handle book snapshot - also triggers decision logic."""
        state = self._states.get(symbol)
        if not state:
            return
        
        now_ms = int(time.time() * 1000)
        
        # Apply snapshot
        success = state.book.apply_snapshot(data)
        if not success:
            self._kill_switch.trigger(symbol, KillReason.BOOK_RESYNC, "Snapshot failed")
            return
        
        # Get book state
        tob = state.book.top_of_book
        delta = state.book.last_delta  # Delta from previous snapshot
        
        # Update feature buffers
        state.microprice_buf.push(tob.microprice)
        state.spread_buf.push(tob.spread)
        
        # Add book deltas to rolling sums (THIS WAS MISSING!)
        state.book_bid_deltas.add(now_ms, delta.delta_bid_size)
        state.book_ask_deltas.add(now_ms, delta.delta_ask_size)
        
        # Update microATR (Time-based sampling: 250ms)
        # This ensures volatility is not diluted by high-frequency zero-change updates
        if state.microprice_buf.size >= 2 and (now_ms - state.last_atr_update_ms >= 250):
            prev_micro = state.microprice_buf.get(-2) # This is technically last tick, but for sampling we should ideally track 'last sampled price'
            # Simpler: just use spread-normalized delta or keep it as is, 
            # but only tick the EMA clock every 250ms.
            # Ideally: delta = abs(curr_micro - last_sampled_price). 
            # But let's stick to simple sampling of current buffering for now to avoid state drift complexity.
            # Better: delta = abs(curr_micro - prev_micro) is minimal move. 
            # Actually, to be scientific, we should use (curr - last_sampled).
            
            # Let's use simple sampling first.
            prev_micro = state.microprice_buf.get(-2)
            curr_micro = tob.microprice
            delta_micro = abs(curr_micro - prev_micro)
            state.micro_atr_ema.update(delta_micro)
            state.last_atr_update_ms = now_ms
        
        # Check if ready and not kill-switched
        if self._kill_switch.check_cooldown(symbol, now_ms):
            return
        
        if not state.is_book_ready:
            return
        
        # Mark WS as ready if all books are ready
        if self._ws and self._ws.state == WSState.WARMING_UP:
            if all(s.is_book_ready for s in self._states.values()):
                self._ws.set_ready()
        
        # Decision logic
        await self._maybe_decide(symbol, state, now_ms)
    
    async def _on_book_update(self, symbol: str, data: Dict) -> None:
        """Handle book delta update."""
        state = self._states.get(symbol)
        if not state:
            return
        
        now_ms = int(time.time() * 1000)
        
        # Apply update
        success = state.book.apply_delta(data)
        if not success:
            self._kill_switch.trigger(symbol, KillReason.BOOK_RESYNC, "Delta failed")
            return
        
        # Get book state
        tob = state.book.top_of_book
        delta = state.book.last_delta
        
        # Update feature buffers
        state.microprice_buf.push(tob.microprice)
        state.spread_buf.push(tob.spread)
        state.book_bid_deltas.add(now_ms, delta.delta_bid_size)
        state.book_ask_deltas.add(now_ms, delta.delta_ask_size)
        
        # Update price history for drift filter (keep ~5s window)
        state.price_history.append((now_ms, tob.microprice))
        while state.price_history and state.price_history[0][0] < now_ms - 5000:
            state.price_history.popleft()
        
        # Update microATR (Time-based sampling: 250ms)
        if state.microprice_buf.size >= 2 and (now_ms - state.last_atr_update_ms >= 250):
            # Scientific: Volatility = StdDev of returns. 
            # Proxy: EMA of |price_t - price_{t-250ms}|
            # Since we just updated buffer, get(-1) is current.
            # We need to find what was price 250ms ago? 
            # RingBuffer doesn't support time-lookup.
            # Approximation: Just update EMA with current tick delta, but only do it 4 times a second.
            # This prevents weight dilution.
            
            prev_micro = state.microprice_buf.get(-2)
            curr_micro = tob.microprice
            delta_micro = abs(curr_micro - prev_micro)
            state.micro_atr_ema.update(delta_micro)
            state.last_atr_update_ms = now_ms
        
        # Check if ready and not kill-switched
        if self._kill_switch.check_cooldown(symbol, now_ms):
            return
        
        if not state.is_book_ready:
            return
        
        # Mark WS as ready if all books are ready
        if self._ws and self._ws.state == WSState.WARMING_UP:
            if all(s.is_book_ready for s in self._states.values()):
                self._ws.set_ready()
        
        # Decision logic
        await self._maybe_decide(symbol, state, now_ms)
    
    async def _on_trade(self, symbol: str, data: Dict) -> None:
        """Handle trade update."""
        state = self._states.get(symbol)
        if not state:
            return
        
        # Get book prices for aggressor resolution
        tob = state.book.top_of_book
        state.trades.on_trade(data, tob.best_bid, tob.best_ask)
    
    # === Binance Event Handlers (Context Only) ===
    
    async def _on_binance_book_update(self, symbol: str, data: Dict) -> None:
        """Handle Binance book ticker update."""
        state = self._binance_states.get(symbol)
        if not state:
            return
        
        # Update Binance book state (simplified - just store BBO)
        best_bid = data.get("best_bid", 0)
        best_ask = data.get("best_ask", 0)
        best_bid_sz = data.get("best_bid_sz", 0)
        best_ask_sz = data.get("best_ask_sz", 0)
        
        if best_bid > 0 and best_ask > 0:
            # Calculate microprice
            microprice = (best_ask * best_bid_sz + best_bid * best_ask_sz) / (best_bid_sz + best_ask_sz) if (best_bid_sz + best_ask_sz) > 0 else (best_bid + best_ask) / 2
            state.microprice_buf.push(microprice)
            state.spread_buf.push(best_ask - best_bid)
            
            # Mark as ready after a few updates
            if not state.book.is_ready and state.microprice_buf.size >= 10:
                state.book._is_ready = True
    
    async def _on_binance_trade(self, symbol: str, data: Dict) -> None:
        """Handle Binance trade update."""
        state = self._binance_states.get(symbol)
        if not state:
            return
        
        # Feed trade to stream manager
        state.trades.on_trade(data)
    
    # === Decision Logic ===
    
    async def _maybe_decide(self, symbol: str, state: CoinState, now_ms: int) -> None:
        """Main decision logic - entry or exit."""
        
        # Check for exit if in position (Taker/General exit)
        if state.has_position:
            await self._check_exit(symbol, state, now_ms)
            return
        
        # === Regime-based routing ===
        # If maker mode enabled, check regime and route accordingly
        if self._maker_engine and self._regime_detector:
            regime_snap = self._regime_detector.detect(state, now_ms)
            regime = regime_snap.regime
            
            # Prepare OFI for maker engine
            ofi_signal = self._ofi_calc.compute(state, now_ms)
            binance_ofi = 0.0
            if self._binance_enabled:
                bin_state = self._binance_states.get(symbol)
                if bin_state and bin_state.trades.is_ready:
                    binance_ofi = bin_state.trades.get_ofi_trade(now_ms)
            
            # 1. Always tick Maker Engine (handles exits/timeouts/updates)
            # Only allow entry if explicitly in RANGE_MAKER
            # User requirement: "Maker orders grace period" is handled inside allow_entry=False logic
            await self._maker_engine.on_tick(
                symbol=symbol,
                state=state,
                ofi_signal=ofi_signal,
                binance_ofi=binance_ofi,
                now_ms=now_ms,
                is_shadow=(self.mode == ExecutionMode.SHADOW),
                allow_entry=(regime == MarketRegime.RANGE_MAKER),
            )
            
            if regime == MarketRegime.RANGE_MAKER:
                return  # Maker active, skip taker
            
            if regime == MarketRegime.GRAY_ZONE:
                return  # Gray zone: Taker passive, Maker passive (exit only)
            
            # If MOMENTUM_TAKER, fall through to Taker entry check
            # (The maker engine already handled its own cleanup/graceful exit above)

        # Default: use taker engine (MOMENTUM_TAKER or no maker config)
        await self._check_entry(symbol, state, now_ms)
    
    def _log_reject(self, symbol: str, reason: str, now_ms: int) -> None:
        """Log rejection with rate limiting (once per 5s per reason category)."""
        category = reason.split(':')[0]
        
        # Log to analytics (always, no rate limit)
        # Reason normalization is now handled inside Analytics class
        self._analytics.log_filter_block(category, reason)
        
        key = f"{symbol}:{category}"
        last = self._last_reject_log_ms.get(key, 0)
        if now_ms - last >= 5000:
            log.info(f"[{symbol}] BLOCKED by {reason}")
            self._last_reject_log_ms[key] = now_ms

    async def _check_entry(self, symbol: str, state: CoinState, now_ms: int) -> None:
        """Check entry conditions."""
        
        # === Gating checks ===
        if not state.can_enter(now_ms):
            return
        
        # Volatility filter
        vol_result = self._vol_filter.check(state)
        if vol_result.status == FilterStatus.BLOCK:
            return
        if vol_result.status == FilterStatus.NOT_READY:
            return
        
        # Spread filter
        spread_result = self._spread_filter.check(state)
        if spread_result.status == FilterStatus.BLOCK:
            return
        if spread_result.status == FilterStatus.NOT_READY:
            return
        
        # === OFI Signal ===
        ofi_signal = self._ofi_calc.compute(state, now_ms)
        
        # Update OFI history for integral/confirmation (keep ~5s window)
        if ofi_signal.is_ready:
            state.ofi_history.append((now_ms, ofi_signal.ofi_smooth))
            while state.ofi_history and state.ofi_history[0][0] < now_ms - 5000:
                state.ofi_history.popleft()
        
        # Periodic status log (every 30 seconds)
        if now_ms - self._last_status_log_ms >= 30000:
            self._last_status_log_ms = now_ms
            tob = state.book.top_of_book
            log.info(
                f"[{symbol}] Status: price={tob.microprice:.2f} ofi={ofi_signal.ofi_smooth:.4f} "
                f"volZ={vol_result.value:.2f} pos={state.position_side or 'none'}"
            )
            # Periodic analytics save
            self._analytics.save()
        if not ofi_signal.is_ready:
            return
        
        # Calculate adaptive thresholds based on volatility
        if self.ofi_config.ofi_adaptive_threshold:
            volz = vol_result.value
            # Clamp volZ to [min, max] for threshold multiplier
            vol_mult = max(self.ofi_config.ofi_vol_clamp_min, 
                          min(self.ofi_config.ofi_vol_clamp_max, volz))
            effective_long_th = self.ofi_config.ofi_long_threshold * vol_mult
            effective_short_th = self.ofi_config.ofi_short_threshold * vol_mult
            
            # Enforce min_ofi_abs floor (no weak signals)
            effective_long_th = max(effective_long_th, self.ofi_config.min_ofi_abs)
            effective_short_th = min(effective_short_th, -self.ofi_config.min_ofi_abs)
        else:
            effective_long_th = self.ofi_config.ofi_long_threshold
            effective_short_th = self.ofi_config.ofi_short_threshold
        
        # === Layer 0: Signal Identification & Stop-Run Guard ===
        current_ofi = ofi_signal.ofi_smooth
        side = None
        
        # Check Stop-Run (Extreme OFI + Spike)
        if self.ofi_config.filter_stoprun_enabled:
            is_extreme = False
            if current_ofi > effective_long_th * self.ofi_config.extreme_ofi_mult:
                is_extreme = True
            elif current_ofi < effective_short_th * self.ofi_config.extreme_ofi_mult:
                is_extreme = True
                
            if is_extreme:
                if vol_result.value > self.ofi_config.volz_cutoff or spread_result.value > self.ofi_config.spreadz_cutoff:
                    log.info(f"[{symbol}] STOP-RUN DETECTED! OFI={current_ofi:.3f} VolZ={vol_result.value:.2f} -> Cooldown {self.ofi_config.stoprun_cooldown_sec}s")
                    state.stoprun_cooldown_until_ms = now_ms + int(self.ofi_config.stoprun_cooldown_sec * 1000)
                    return

        # Check basic thresholds
        if current_ofi >= effective_long_th:
            side = "buy"
        elif current_ofi <= effective_short_th:
            side = "sell"
        else:
            return  # No signal
        
        # === Layer 0.5: Binance Cross-Venue Veto ===
        if self._binance_enabled and self._binance_veto_enabled:
            binance_state = self._binance_states.get(symbol)
            if binance_state and binance_state.trades.is_ready:
                # Calculate Binance OFI
                binance_ofi = binance_state.trades.get_ofi_trade(now_ms)
                
                # Only veto if Binance has meaningful signal
                if abs(binance_ofi) >= self._binance_veto_min_ofi:
                    hyper_sign = 1 if current_ofi > 0 else -1
                    binance_sign = 1 if binance_ofi > 0 else -1
                    
                    if hyper_sign != binance_sign:
                        self._log_reject(symbol, f"BinanceVeto: Hyp={current_ofi:.2f} vs Bin={binance_ofi:.2f} (sign mismatch)", now_ms)
                        return  # Cross-venue disagreement
            
        # === Layer 1: OFI Integral & Confirmation ===
        # These can work as AND (both required) or OR (either sufficient)
        
        confirm_passed = True
        integral_passed = True
        
        # 1A. Confirmation (last N ticks)
        if self.ofi_config.filter_confirmation_enabled:
            confirm_ticks = self.ofi_config.entry_confirm_ticks
            relevant_history = list(state.ofi_history)[-confirm_ticks:]
            hits = 0
            for _, val in relevant_history:
                if side == "buy" and val >= effective_long_th: hits += 1
                if side == "sell" and val <= effective_short_th: hits += 1
                
            if hits < self.ofi_config.entry_confirm_min_hits:
                confirm_passed = False
            
        # 1B. Integral (Area)
        if self.ofi_config.filter_integral_enabled:
            area_cutoff_ms = now_ms - int(self.ofi_config.ofi_area_window_sec * 1000)
            ofi_area = sum(v for t, v in state.ofi_history if t >= area_cutoff_ms)
            
            if side == "buy" and ofi_area < self.ofi_config.ofi_area_long:
                integral_passed = False
            if side == "sell" and ofi_area > self.ofi_config.ofi_area_short:
                integral_passed = False
        
        # Apply AND/OR logic based on config
        if self.ofi_config.confirm_integral_mode == "OR":
            # Either filter passing is sufficient
            if not confirm_passed and not integral_passed:
                self._log_reject(symbol, f"Confirm+Integral(OR): both failed", now_ms)
                return
        else:  # AND mode (default)
            # Both filters must pass
            if not confirm_passed:
                self._log_reject(symbol, f"Confirmation: failed (AND mode)", now_ms)
                return
            if not integral_passed:
                self._log_reject(symbol, f"Integral: failed (AND mode)", now_ms)
                return
            
        # === Layer 2: Microprice Drift (Tick-Based) & Trade Imbalance ===
        tob = state.book.top_of_book
        
        # 2A. Trade Imbalance Confirmation
        if self.ofi_config.filter_trade_imbalance_enabled:
            # Skip if too few trades (noisy in quiet markets)
            trade_count = state.trades.get_trade_count(now_ms)
            
            if trade_count >= self.ofi_config.trade_imbalance_min_trades:
                buy_ratio = state.trades.get_buy_ratio(now_ms)
                
                if side == "buy" and buy_ratio < 0.4:
                     self._log_reject(symbol, f"TradeImbalance: buy_ratio={buy_ratio:.2f} < 0.4 (Bearish flow)", now_ms)
                     return
                if side == "sell" and buy_ratio > 0.6:
                     self._log_reject(symbol, f"TradeImbalance: buy_ratio={buy_ratio:.2f} > 0.6 (Bullish flow)", now_ms)
                     return
            # else: skip imbalance check (too few trades = noisy)

        # 2B. Microprice Drift (Tick-based with Bypass)
        if self.ofi_config.filter_drift_enabled:
            # Check if strong OFI bypasses drift
            bypass_drift = abs(current_ofi) >= self.ofi_config.drift_bypass_ofi_abs
            
            if not bypass_drift:
                lookback = self.ofi_config.drift_ticks_lookback
                if state.microprice_buf.size >= lookback + 1:
                    past_price = state.microprice_buf.get(-(lookback + 1))
                    micro_drift = tob.microprice - past_price
                    
                    required_drift = tob.spread * self.ofi_config.micro_drift_spread_mult
                    
                    if side == "buy" and micro_drift < required_drift:
                        self._log_reject(symbol, f"Drift: val={micro_drift:.2f} < req={required_drift:.2f}", now_ms)
                        return
                    if side == "sell" and micro_drift > -required_drift:
                        self._log_reject(symbol, f"Drift: val={micro_drift:.2f} > req={-required_drift:.2f}", now_ms)
                        return
        
        # === Layer 3: Edge Gate (Expected Move vs Cost) ===
        if self.ofi_config.filter_edge_enabled:
            # Volatility Floor: If ATR is 0, assume minimal volatility (20% of spread)
            micro_atr = max(state.micro_atr_ema.value or 0.0, tob.spread * 0.2)
            
            expected_move_bps = (micro_atr * self.ofi_config.edge_atr_mult / tob.microprice) * 10000 if tob.microprice > 0 else 0
            
            # Costs breakdown
            spread_bps = (tob.spread / tob.microprice) * 10000
            fee_bps = self.ofi_config.taker_fee_bps * 2
            slip_bps = self.ofi_config.est_slippage_bps
            buff_bps = self.ofi_config.edge_buffer_bps
            cost_bps = spread_bps + fee_bps + slip_bps + buff_bps
            
            if expected_move_bps < cost_bps:
                self._log_reject(symbol, f"Edge: exp={expected_move_bps:.1f} < cost={cost_bps:.1f} (spd={spread_bps:.1f} fee={fee_bps:.1f} slip={slip_bps:.1f} buf={buff_bps:.1f})", now_ms)
                return  # Not enough edge (expected move < costs)
        
        # === Execute Entry ===
        await self._execute_entry(symbol, state, side, ofi_signal, now_ms)
    
    async def _check_exit(self, symbol: str, state: CoinState, now_ms: int) -> None:
        """Check exit conditions."""
        
        tob = state.book.top_of_book
        
        # === Hard risk exits ===
        
        # Volatility spike (with gating)
        vol_result = self._vol_filter.check(state)
        if vol_result.value > self.ofi_config.volz_exit_cutoff:  # Use exit-specific cutoff
            # Check min hold time
            if state.entry_time_ms > 0:
                hold_sec = (now_ms - state.entry_time_ms) / 1000.0
                if hold_sec < self.ofi_config.vol_spike_min_hold_sec:
                    pass  # Too early for vol-spike exit, skip
                else:
                    # Check if spike is AGAINST our position (OFI flipped)
                    ofi_signal = self._ofi_calc.compute(state, now_ms)
                    spike_against_us = False
                    if ofi_signal.is_ready:
                        if state.position_side == "long" and ofi_signal.ofi_smooth < 0:
                            spike_against_us = True
                        elif state.position_side == "short" and ofi_signal.ofi_smooth > 0:
                            spike_against_us = True
                    
                    # Only vol-exit if spike is against us OR we have solid profit
                    should_exit = False
                    net_pnl = self._compute_net_pnl_pct(state, tob)
                    
                    if spike_against_us and net_pnl >= 0:
                        should_exit = True  # Exit: spike against us but we're profitable
                    elif net_pnl >= 0.03:  # 0.03% net profit - lock it in
                        should_exit = True  # Exit: good profit, lock it in
                    # else: spike in our favor, ride it!
                    
                    if should_exit:
                        await self._execute_exit(symbol, state, OrderIntent.EXIT_RISK, "vol_spike", now_ms)
                        return
        
        # Spread spike (with gating, unless hard stop)
        spread_result = self._spread_filter.check(state)
        spread_pct = tob.spread / tob.microprice if tob.microprice > 0 else 0
        
        # Hard emergency exit: spread >= hard_spread_stop_pct (always exits)
        if spread_pct >= self.ofi_config.hard_spread_stop_pct:
            await self._execute_exit(symbol, state, OrderIntent.EXIT_RISK, "hard_spread_stop", now_ms)
            return
        
        # Normal spread_spike exit (with gating)
        # Use separate exit cutoff (higher tolerance than entry)
        if spread_result.value > self.ofi_config.spreadz_exit_cutoff:
            should_exit = True
            if self.ofi_config.exit_require_net_nonnegative_for_spread_spike:
                net_pnl = self._compute_net_pnl_pct(state, tob)
                if net_pnl < 0:
                    should_exit = False  # Don't spread-exit at a loss
            
            if should_exit:
                await self._execute_exit(symbol, state, OrderIntent.EXIT_RISK, "spread_spike", now_ms)
                return
        
        # === TP/SL ===
        if state.avg_entry > 0:
            pnl_pct = self._compute_pnl_pct(state, tob.microprice)
            
            if pnl_pct >= self.ofi_config.tp_pct * 100:
                await self._execute_exit(symbol, state, OrderIntent.EXIT_TPSL, "take_profit", now_ms)
                return
            
            if pnl_pct <= -self.ofi_config.sl_pct * 100:
                await self._execute_exit(symbol, state, OrderIntent.EXIT_TPSL, "stop_loss", now_ms)
                return
        
        # === Max hold time ===
        if state.entry_time_ms > 0:
            hold_sec = (now_ms - state.entry_time_ms) / 1000.0
            if hold_sec >= self.ofi_config.max_hold_sec:
                await self._execute_exit(symbol, state, OrderIntent.EXIT_TIME, "max_hold", now_ms)
                return
        
        # === OFI flip (with debounce + profit gate) ===
        ofi_signal = self._ofi_calc.compute(state, now_ms)
        if ofi_signal.is_ready:
            is_flip = False
            if state.position_side == "long" and ofi_signal.ofi_smooth < 0:
                is_flip = True
            elif state.position_side == "short" and ofi_signal.ofi_smooth > 0:
                is_flip = True
            
            if is_flip:
                # Increment debounce counter
                self._flip_counter[symbol] = self._flip_counter.get(symbol, 0) + 1
                
                # Only exit if debounce threshold reached
                if self._flip_counter[symbol] >= self.ofi_config.ofi_flip_debounce:
                    should_exit = True
                    
                    # Check net profit gating (if enabled)
                    if self.ofi_config.exit_require_net_nonnegative_for_flip:
                        tob = state.book.top_of_book
                        net_pnl = self._compute_net_pnl_pct(state, tob)
                        if net_pnl < 0:
                            should_exit = False  # Don't flip exit at a loss
                    
                    if should_exit:
                        await self._execute_exit(symbol, state, OrderIntent.EXIT_OFI_FLIP, "ofi_flip", now_ms)
                        self._flip_counter[symbol] = 0
                        return
            else:
                # Reset counter if OFI is back in our direction
                self._flip_counter[symbol] = 0
    
    def _compute_pnl_pct(self, state: CoinState, current_price: float) -> float:
        """Compute unrealized PnL percentage."""
        if state.avg_entry <= 0 or current_price <= 0:
            return 0.0
        
        if state.position_side == "long":
            return ((current_price - state.avg_entry) / state.avg_entry) * 100
        else:
            return ((state.avg_entry - current_price) / state.avg_entry) * 100
    
    def _estimate_slippage_bps(self, spread: float, micro_atr: float, price: float) -> float:
        """Estimate slippage in basis points."""
        if price <= 0:
            return 0.0
        
        max_slip = max(
            spread * self.ofi_config.max_slip_spread_mult,
            micro_atr * self.ofi_config.max_slip_atr_mult,
        )
        return (max_slip / price) * 10000
    
    def _compute_net_pnl_pct(self, state: CoinState, tob) -> float:
        """
        Compute net PnL percentage including all costs.
        
        Net PnL = Gross PnL - Spread Cost - 2x Taker Fee - Slippage Estimate
        """
        if not tob or tob.microprice <= 0:
            return 0.0
        
        gross_pnl_pct = self._compute_pnl_pct(state, tob.microprice)
        spread_cost_pct = (tob.spread / tob.microprice) * 100
        fee_cost_pct = (self.ofi_config.taker_fee_bps / 100) * 2  # Entry + Exit
        slippage_cost_pct = (self.ofi_config.est_slippage_bps / 100) * 2
        
        return gross_pnl_pct - spread_cost_pct - fee_cost_pct - slippage_cost_pct
    
    # === Execution ===
    
    async def _execute_entry(
        self,
        symbol: str,
        state: CoinState,
        side: str,
        ofi_signal: OFISignal,
        now_ms: int,
    ) -> None:
        """Execute entry order."""
        tob = state.book.top_of_book
        
        # Calculate IOC limit price
        micro_atr = state.micro_atr_ema.value or 0.0
        max_slip = max(
            tob.spread * self.ofi_config.max_slip_spread_mult,
            micro_atr * self.ofi_config.max_slip_atr_mult,
        )
        
        if side == "buy":
            limit_price = tob.best_ask + max_slip
        else:
            limit_price = tob.best_bid - max_slip
        
        # Calculate size
        qty = self.ofi_config.position_size_usd / tob.microprice
        
        # Capture decision snapshot
        snapshot = state.capture_decision_snapshot(ofi_signal.ofi_smooth)
        
        # Shadow mode - log only but simulate position
        if self.mode == ExecutionMode.SHADOW:
            # Get vol_z and spread_z from filters
            vol_result = self._vol_filter.check(state)
            spread_result = self._spread_filter.check(state)
            
            log.info(
                f"[SHADOW] {symbol} ENTRY {side.upper()} "
                f"qty={qty:.4f} limit={limit_price:.2f} ofi={ofi_signal.ofi_smooth:.3f} "
                f"bid={tob.best_bid:.2f} ask={tob.best_ask:.2f} spread={tob.spread:.3f} "
                f"max_slip={max_slip:.4f} atr={micro_atr:.4f}"
            )
            
            # Log to JSON
            self._trade_logger.log_entry(
                symbol=symbol,
                side=side,
                qty=qty,
                limit_price=limit_price,
                ofi_smooth=ofi_signal.ofi_smooth,
                ofi_threshold=self.ofi_config.ofi_long_threshold if side == "buy" else self.ofi_config.ofi_short_threshold,
                best_bid=tob.best_bid,
                best_ask=tob.best_ask,
                spread=tob.spread,
                vol_z=vol_result.value if vol_result.value else 1.0,
                spread_z=spread_result.value if spread_result.value else 1.0,
                micro_atr=micro_atr,
                max_slip=max_slip,
                taker_fee_bps=self.ofi_config.taker_fee_bps,
                mode="shadow",
            )
            
            # Simulate position entry (so we wait for exit, not spam entries)
            # position_side is derived from position_qty automatically
            state.position_qty = qty if side == "buy" else -qty
            state.avg_entry = tob.microprice
            state.entry_time_ms = now_ms
            state.entry_inflight = False
            state.cooldown_until_ms = now_ms + self.ofi_config.cooldown_ms
            return
        
        # Create order
        order = self._order_manager.create_order(
            symbol=symbol,
            side=side,
            qty=qty,
            intent=OrderIntent.ENTRY,
            limit_price=limit_price,
            decision_snapshot=snapshot,
        )
        
        if not order:
            log.warning(f"[{symbol}] Entry order creation failed (duplicate)")
            return
        
        # Set lock
        state.entry_inflight = True
        state.last_decision_ms = now_ms
        
        # Send order (would call exchange here)
        log.info(
            f"[{symbol}] ENTRY {side.upper()} "
            f"qty={qty:.4f} limit={limit_price:.2f} "
            f"cid={order.client_id}"
        )
        self._order_manager.mark_sent(order)
        
        # TODO: Actually send to exchange via execution_manager
        # result = await self._send_ioc_order(symbol, side, qty, limit_price)
    
    async def _execute_exit(
        self,
        symbol: str,
        state: CoinState,
        intent: OrderIntent,
        reason: str,
        now_ms: int,
    ) -> None:
        """Execute exit order."""
        if state.exit_inflight:
            return  # Already exiting
        
        tob = state.book.top_of_book
        qty = abs(state.position_qty)
        
        # Determine exit side (opposite of position)
        side = "sell" if state.position_side == "long" else "buy"
        
        # Calculate IOC limit price for exit
        micro_atr = state.micro_atr_ema.value or 0.0
        max_slip = max(
            tob.spread * self.ofi_config.max_slip_spread_mult,
            micro_atr * self.ofi_config.max_slip_atr_mult,
        )
        
        if side == "buy":
            limit_price = tob.best_ask + max_slip
        else:
            limit_price = tob.best_bid - max_slip
        
        # Shadow mode - realistic PnL calculation
        if self.mode == ExecutionMode.SHADOW:
            # Gross PnL
            gross_pnl_pct = self._compute_pnl_pct(state, tob.microprice)
            
            # Costs (in percentage)
            spread_cost_pct = (tob.spread / tob.microprice) * 100  # Entry + Exit spread
            fee_cost_pct = (self.ofi_config.taker_fee_bps / 100) * 2  # 2x taker fee (entry + exit)
            slippage_est_pct = (self.ofi_config.est_slippage_bps / 100) * 2  # Estimated slippage
            
            # Net PnL
            net_pnl_pct = gross_pnl_pct - spread_cost_pct - fee_cost_pct - slippage_est_pct
            
            log.info(
                f"[SHADOW] {symbol} EXIT {reason} qty={qty:.4f} "
                f"gross={gross_pnl_pct:+.3f}% net={net_pnl_pct:+.3f}% "
                f"(spread={spread_cost_pct:.3f}% fee={fee_cost_pct:.3f}% slip={slippage_est_pct:.3f}%)"
            )
            
            # Log to JSON
            self._trade_logger.log_exit(
                symbol=symbol,
                reason=reason,
                best_bid=tob.best_bid,
                best_ask=tob.best_ask,
                spread=tob.spread,
                gross_pnl_pct=gross_pnl_pct,
                spread_cost_pct=spread_cost_pct,
                fee_cost_pct=fee_cost_pct,
                slippage_est_pct=slippage_est_pct,
                net_pnl_pct=net_pnl_pct,
                entry_time_ms=state.entry_time_ms,
                now_ms=now_ms,
            )
            
            state.close_position()
            state.cooldown_until_ms = now_ms + self.ofi_config.cooldown_ms
            self._flip_counter[symbol] = 0  # Reset debounce on exit
            return
        
        # Create order
        order = self._order_manager.create_order(
            symbol=symbol,
            side=side,
            qty=qty,
            intent=intent,
            limit_price=limit_price,
        )
        
        if not order:
            return
        
        state.exit_inflight = True
        state.active_exit_signal = True
        
        log.info(f"[{symbol}] EXIT {reason} qty={qty:.4f} cid={order.client_id}")
        self._order_manager.mark_sent(order)
        
        # TODO: Send to exchange
    
    # === Order Event Callbacks ===
    
    def _on_order_fill(self, order: Order, fill_qty: float, fill_price: float) -> None:
        """Handle order fill."""
        state = self._states.get(order.symbol)
        if not state:
            return
        
        now_ms = int(time.time() * 1000)
        
        # Update position
        signed_qty = fill_qty if order.side == "buy" else -fill_qty
        state.update_position(signed_qty, fill_price, now_ms)
        
        log.info(f"[{order.symbol}] FILL {order.side} {fill_qty:.4f} @ {fill_price:.4f}")
    
    def _on_order_terminal(self, order: Order) -> None:
        """Handle order terminal state - release locks."""
        state = self._states.get(order.symbol)
        if not state:
            return
        
        now_ms = int(time.time() * 1000)
        
        # Release appropriate lock
        if order.is_entry:
            state.entry_inflight = False
        else:
            state.exit_inflight = False
            state.active_exit_signal = False
        
        # Set cooldown
        state.cooldown_until_ms = now_ms + self.ofi_config.cooldown_ms
        
        # Log slippage
        if order.slippage is not None:
            log.info(f"[{order.symbol}] Order {order.client_id} slippage: {order.slippage:.4f}")
    
    def _on_kill_switch(self, symbol: str, reason: KillReason, details: str) -> None:
        """Handle kill-switch trigger."""
        state = self._states.get(symbol)
        if state:
            state.kill_switch = True
            state.reset_buffers()
        
        # TODO: Cancel orders, flatten if needed
    
    # === Watchdog ===
    
    async def _watchdog_loop(self) -> None:
        """Periodic watchdog for stuck orders and reconciliation."""
        while self._running:
            await asyncio.sleep(0.25)  # 250ms interval
            
            now_ms = int(time.time() * 1000)
            
            # Check for stuck orders
            stuck = self._order_manager.get_stuck_orders(now_ms)
            for order in stuck:
                log.warning(f"Stuck order detected: {order.client_id}")
                self._order_manager.on_timeout(order.client_id)
                
                # Trigger reconcile
                self._kill_switch.trigger(
                    order.symbol,
                    KillReason.ORDER_STUCK,
                    f"Order {order.client_id} timed out"
                )
            
            # Cleanup old orders
            self._order_manager.cleanup_old()
