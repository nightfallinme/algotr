# hyperbot/vacuum_orchestrator.py
"""
Vacuum Scalper Orchestrator - Main event loop for liquidity vacuum scalping.

This is the async WebSocket-driven orchestrator that:
1. Receives real-time book/trade updates
2. Analyzes liquidity structure (walls, vacuums, LIR)
3. Makes entry/exit decisions (Break vs Fade)
4. Executes with maker-first or taker logic

Strategy Modes:
- MOD 1 (Break): Vacuum ahead + LIR confirms → Taker entry
- MOD 2 (Fade): Wall touch + rejection → Maker-first entry

Architecture:
- Market Data Layer: WebSocket-based with snapshot + incremental updates
- Market State Layer: LiquidityMap, WallTracker, LIRCalculator
- Strategy Decision Layer: Fade vs Break classification
- Execution Layer: Maker-first with taker fallback
- Risk Layer: Fixed TP/SL, max hold time, kill-switch
"""
import asyncio
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Optional, Any
import time

from .vacuum import (
    LiquidityMap, VacuumDetector, WallTracker, LIRCalculator,
    VacuumCoinState, VacuumSignal, LIRSignal, LiquidityWall,
    BinanceBookState, Regime, WallState,
)
from .ws.ws_manager import WSManager, WSState
from .ws.book_manager import BookState
from .execution.order_manager import OrderManager, Order, OrderIntent, OrderState
from .execution.kill_switch import KillSwitch
from .execution.reconciler import PositionReconciler
from .ofi.trade_logger import OFITradeLogger
from .config import Config
from .logger import get_logger
from .vacuum.strategies.engine import StrategyEngine
from .vacuum.strategies.base import StrategyContext

log = get_logger(__name__)


# Simple trade logger for vacuum strategy
class VacuumTradeLogger:
    """Lightweight trade logger for vacuum strategy."""
    
    def __init__(self, output_file: str = "vacuum_trades.json"):
        self.output_file = output_file
        self._signals: list = []
        self._trades: list = []
    
    def log_shadow_signal(self, data: dict) -> None:
        """Log shadow mode signal."""
        self._signals.append(data)
        log.debug(f"Shadow signal: {data}")
        self.save()  # Auto-save
    
    def log_trade(self, data: dict) -> None:
        """Log completed trade."""
        self._trades.append(data)
        log.info(f"Trade logged: {data}")
        self.save()  # Auto-save
    
    def save(self) -> None:
        """Save logged data to file with PnL summary at top."""
        import json
        try:
            # Calculate PnL summary from closed trades (signals with exit)
            closed_trades = [s for s in self._signals if "exit_price" in s and "pnl_pct" in s]
            
            total_pnl_pct = sum(t.get("pnl_pct", 0) for t in closed_trades)
            win_count = sum(1 for t in closed_trades if t.get("pnl_pct", 0) > 0)
            loss_count = sum(1 for t in closed_trades if t.get("pnl_pct", 0) <= 0)
            total_trades = len(closed_trades)
            win_rate = (win_count / total_trades * 100) if total_trades > 0 else 0
            
            # Calculate by exit reason
            tp_trades = [t for t in closed_trades if t.get("reason") == "TP"]
            sl_trades = [t for t in closed_trades if t.get("reason") == "SL"]
            time_trades = [t for t in closed_trades if t.get("reason") == "TIME"]
            
            summary = {
                "total_pnl_pct": round(total_pnl_pct * 100, 4),  # As percentage
                "total_pnl_bps": round(total_pnl_pct * 10000, 2),  # As basis points
                "total_trades": total_trades,
                "wins": win_count,
                "losses": loss_count,
                "win_rate_pct": round(win_rate, 1),
                "avg_pnl_bps": round(total_pnl_pct / total_trades * 10000, 2) if total_trades > 0 else 0,
                "tp_count": len(tp_trades),
                "sl_count": len(sl_trades),
                "time_count": len(time_trades),
                "tp_pnl_bps": round(sum(t.get("pnl_pct", 0) for t in tp_trades) * 10000, 2),
                "sl_pnl_bps": round(sum(t.get("pnl_pct", 0) for t in sl_trades) * 10000, 2),
            }
            
            with open(self.output_file, "w") as f:
                json.dump({
                    "summary": summary,
                    "signals": self._signals,
                    "trades": self._trades
                }, f, indent=2)
        except Exception as e:
            log.error(f"Failed to save trade log: {e}")


class ExecutionMode(Enum):
    """Orchestrator execution mode."""
    SHADOW = auto()  # Log signals, no execution
    PAPER = auto()   # Small size execution
    LIVE = auto()    # Full execution


class EntryMode(Enum):
    """Entry type."""
    BREAK = "break"  # MOD 1: Vacuum continuation
    FADE = "fade"    # MOD 2: Wall rejection


@dataclass
class VacuumConfig:
    """Vacuum scalping configuration."""
    # Fees (Hyperliquid Tier 0)
    maker_fee_bps: float = 1.5
    taker_fee_bps: float = 4.5
    
    # Wall detection
    wall_min_size_mult: float = 3.0
    wall_max_distance_bps: float = 20.0
    wall_stability_ticks: int = 2
    depth_levels: int = 20
    
    # Vacuum detection
    min_vacuum_bps: float = 1.5
    vacuum_thin_mult: float = 0.3
    max_vacuum_bps: float = 15.0
    
    # LIR
    lir_depth_bps: float = 10.0
    lir_ema_span: int = 10
    lir_long_threshold: float = 1.5
    lir_short_threshold: float = 0.66
    
    # Wall touch & rejection
    touch_threshold_bps: float = 1.0
    rejection_threshold_bps: float = 0.5
    min_rejection_ticks: int = 2
    max_touch_duration_ms: int = 10000
    
    # Entry    # Filters
    spread_cutoff: float = 1.2
    atr_min_bps: float = 0.8
    warm_up_ticks: int = 50
    
    # Risk
    risk_per_trade_usd: float = 15.0
    max_positions: int = 1
    cooldown_ticks: int = 20
    stop_loss_bps: float = 5.0
    take_profit_bps: float = 8.0
    max_hold_sec: int = 60
    
    # v2.1: Mode-specific Dynamic Risk Params (Tier 0: 9bps round-trip)
    fade_tp_mult: float = 3.5
    fade_tp_min: float = 15.0    # Net ~6bps after 9bps fees
    fade_sl_mult: float = 5.0
    fade_sl_min: float = 20.0
    fade_max_hold_sec: int = 600
    
    break_tp_mult: float = 7.0
    break_tp_min: float = 30.0   # Net ~21bps after fees
    break_sl_mult: float = 4.0
    break_sl_min: float = 15.0
    break_max_hold_sec: int = 600
    
    # v2.1: Strategies
    strategies: Dict[str, Any] = field(default_factory=dict)
    
    # Execution
    cooldown_ms: int = 200
    maker_timeout_sec: float = 2.0
    ack_timeout_ms: int = 2000
    
    # Sizing
    position_size_usd: float = 100.0
    
    # Binance Leader
    binance_enabled: bool = True
    binance_deviation_threshold_bps: float = 3.0  # Min deviation for fade
    binance_wall_buffer_bps: float = 2.0


class VacuumOrchestrator:
    """
    WebSocket-driven Vacuum Scalping orchestrator.
    
    Usage:
        orch = VacuumOrchestrator(config, symbols=["ETH"])
        await orch.run()
    """
    
    def __init__(
        self,
        config: Config,
        symbols: List[str],
        execution_mode: ExecutionMode = ExecutionMode.SHADOW,
    ):
        self._config_raw = config
        self._symbols = symbols
        self._execution_mode = execution_mode
        
        # Load vacuum config
        self._cfg = self._load_vacuum_config()
        
        # Per-symbol state
        self._states: Dict[str, VacuumCoinState] = {}
        for sym in symbols:
            self._states[sym] = self._create_coin_state(sym)
        
        # Components
        self._ws: Optional[WSManager] = None
        self._binance_ws = None  # BinanceWSManager
        self._order_manager = OrderManager(
            bot_id="vacuum",
            ack_timeout_ms=self._cfg.ack_timeout_ms,
            on_fill=self._on_fill,
            on_terminal=self._on_order_terminal,
        )
        self._kill_switch = KillSwitch()
        self._trade_logger = VacuumTradeLogger()
        
        # Strategy Engine
        self._strategy_engine = StrategyEngine(self._cfg)
        
        # Binance leader state (per-symbol)
        # New industrial BinanceBookState auto-configures WallAnalyzer and RegimeManager
        self._binance_states: Dict[str, BinanceBookState] = {}
        if self._cfg.binance_enabled:
            for sym in symbols:
                self._binance_states[sym] = BinanceBookState(symbol=sym)
        
        # Control
        self._running = False
        self._last_status_log_ms = 0
    
    def _load_vacuum_config(self) -> VacuumConfig:
        """Load vacuum config from Config object."""
        cfg_dict = self._config_raw.as_dict()
        v = cfg_dict.get("vacuum", {})
        
        # Default strategies (Robust fallback)
        default_strats = {
            "fade": {"enabled": True, "deviation_bps": 1.5, "dynamic_spread_mult": 1.2},
            "break_strat": {"enabled": True, "vacuum_threshold": 0.4, "lir_bull_threshold": 1.3, "lir_bear_threshold": 0.7},
            "absorption": {"enabled": True, "touch_zone_bps": 1.0, "min_bounce_ticks": 2},
            "vacuum_fill": {"enabled": True, "vacuum_threshold": 0.25, "lir_neutral_range": [0.9, 1.1]},
            "churn_spike": {"enabled": True, "churn_mult": 2.5, "cooldown_sec": 60},
            "lir_divergence": {"enabled": True, "lir_extreme": 1.8},
            "fresh_wall": {"enabled": True, "min_strength": 5.0},
            "aggressor_momentum": {"enabled": True, "bias_threshold": 2.0}
        }
        
        # Load and merge
        loaded_strats = v.get("strategies", {})
        # Deep merge to preserve defaults if key exists but is empty
        for k, default_val in default_strats.items():
            if k in loaded_strats:
                # Update default with loaded values
                default_val.update(loaded_strats[k])
            else:
                # Use default
                loaded_strats[k] = default_val
        
        print(f"[DEBUG] Final merged strategies: {list(loaded_strats.keys())}")
        print(f"[DEBUG] Absorption status: {loaded_strats.get('absorption', {}).get('enabled')}")

        return VacuumConfig(
            # Fees
            maker_fee_bps=v.get("maker_fee_bps", 0.0),
            taker_fee_bps=v.get("taker_fee_bps", 2.5),
            # Wall detection
            wall_min_size_mult=v.get("wall_min_size_mult", 3.0),
            wall_max_distance_bps=v.get("wall_max_distance_bps", 20.0),
            wall_stability_ticks=v.get("wall_stability_ticks", 2),
            depth_levels=v.get("depth_levels", 20),
            # Vacuum
            min_vacuum_bps=v.get("min_vacuum_bps", 1.5),
            vacuum_thin_mult=v.get("vacuum_thin_mult", 0.3),
            max_vacuum_bps=v.get("max_vacuum_bps", 15.0),
            # LIR
            lir_depth_bps=v.get("lir_depth_bps", 10.0),
            lir_ema_span=v.get("lir_ema_span", 10),
            lir_long_threshold=v.get("lir_long_threshold", 1.5),
            lir_short_threshold=v.get("lir_short_threshold", 0.66),
            # Touch & rejection
            touch_threshold_bps=v.get("touch_threshold_bps", 1.0),
            rejection_threshold_bps=v.get("rejection_threshold_bps", 0.5),
            min_rejection_ticks=v.get("min_rejection_ticks", 2),
            max_touch_duration_ms=v.get("max_touch_duration_ms", 10000),
            # Filters
            spread_cutoff=v.get("spread_cutoff", 2.0),
            atr_min_bps=v.get("atr_min_bps", 0.4),
            warm_up_ticks=v.get("warm_up_ticks", 50),
            # Risk
            risk_per_trade_usd=v.get("risk_per_trade_usd", 15.0),
            max_positions=v.get("max_positions", 1),
            cooldown_ticks=v.get("cooldown_ticks", 20),
            stop_loss_bps=v.get("stop_loss_bps", 10.0),
            take_profit_bps=v.get("take_profit_bps", 15.0),
            max_hold_sec=v.get("max_hold_sec", 300),
            
            # v2.1: Mode-specific Risk Params
            # v2.1: Mode-specific Dynamic Risk Params (Scientific ATR-based)
            fade_tp_mult=v.get("fade_tp_mult", 2.0),     # Target 2x Volatility
            fade_tp_min=v.get("fade_tp_min", 6.0),       # Floor 6bps
            fade_sl_mult=v.get("fade_sl_mult", 3.0),     # Stop 3x Volatility
            fade_sl_min=v.get("fade_sl_min", 13.0),      # Floor 13bps
            fade_max_hold_sec=v.get("fade_max_hold_sec", 300),
            
            break_tp_mult=v.get("break_tp_mult", 5.0),   # Target 5x Volatility (Momentum runs)
            break_tp_min=v.get("break_tp_min", 15.0),    # Floor 15bps
            break_sl_mult=v.get("break_sl_mult", 2.0),   # Stop 2x Volatility
            break_sl_min=v.get("break_sl_min", 8.0),     # Floor 8bps
            break_max_hold_sec=v.get("break_max_hold_sec", 300),
            
            # v2.1: Strategies
            strategies=loaded_strats,
            
            # Execution
            
            # Execution
            cooldown_ms=v.get("cooldown_ms", 200),
            maker_timeout_sec=v.get("maker_timeout_sec", 2.0),
            ack_timeout_ms=v.get("ack_timeout_ms", 2000),
            # Sizing
            position_size_usd=v.get("position_size_usd", 100.0),
            # Binance Leader
            binance_enabled=v.get("binance_enabled", True),
            binance_deviation_threshold_bps=v.get("binance_deviation_threshold_bps", 3.0),
            binance_wall_buffer_bps=v.get("binance_wall_buffer_bps", 2.0),
        )
    
    def _create_coin_state(self, symbol: str) -> VacuumCoinState:
        """Create coin state with configured components."""
        cfg = self._cfg
        
        liquidity_map = LiquidityMap(
            wall_min_size_mult=cfg.wall_min_size_mult,
            wall_max_distance_bps=cfg.wall_max_distance_bps,
            wall_stability_ticks=cfg.wall_stability_ticks,
            depth_levels=cfg.depth_levels,
        )
        
        vacuum_detector = VacuumDetector(
            min_vacuum_bps=cfg.min_vacuum_bps,
            vacuum_thin_mult=cfg.vacuum_thin_mult,
            max_vacuum_bps=cfg.max_vacuum_bps,
        )
        
        wall_tracker = WallTracker(
            touch_threshold_bps=cfg.touch_threshold_bps,
            rejection_threshold_bps=cfg.rejection_threshold_bps,
            max_touch_duration_ms=cfg.max_touch_duration_ms,
        )
        
        lir_calculator = LIRCalculator(
            depth_bps=cfg.lir_depth_bps,
            ema_span=cfg.lir_ema_span,
            long_threshold=cfg.lir_long_threshold,
            short_threshold=cfg.lir_short_threshold,
        )
        
        return VacuumCoinState(
            symbol=symbol,
            liquidity_map=liquidity_map,
            vacuum_detector=vacuum_detector,
            wall_tracker=wall_tracker,
            lir_calculator=lir_calculator,
        )
    
    async def run(self) -> None:
        """Main run loop."""
        self._running = True
        log.info("VacuumOrchestrator starting")
        log.info(f"Mode: {self._execution_mode.name}")
        log.info(f"Symbols: {self._symbols}")
        log.info(f"Binance Leader: {'ENABLED' if self._cfg.binance_enabled else 'DISABLED'}")
        
        # Create Hyperliquid WS manager
        from .ws.ws_manager import WSConfig
        
        cfg_dict = self._config_raw.as_dict()
        ws_url = cfg_dict.get("data", {}).get("ws_url", "wss://api.hyperliquid.xyz/ws")
        
        ws_config = WSConfig(url=ws_url)
        self._ws = WSManager(
            symbols=self._symbols,
            config=ws_config,
            on_state_change=self._on_ws_state_change,
        )
        
        # Set Hyperliquid event handlers
        self._ws.on_book_snapshot = self._on_book_snapshot_async
        self._ws.on_book_update = self._on_book_update_async
        self._ws.on_trade = self._on_trade_async
        
        # Create Binance WS manager if enabled
        binance_task = None
        if self._cfg.binance_enabled:
            from .ws.binance_ws import BinanceWSManager, BinanceWSConfig
            
            binance_config = BinanceWSConfig(
                depth_levels=20,
                depth_update_speed="500ms",
            )
            self._binance_ws = BinanceWSManager(
                symbols=self._symbols,
                config=binance_config,
            )
            # Set Binance depth callback
            self._binance_ws.on_depth = self._on_binance_depth_async
            log.info("Binance WS manager created")
        
        try:
            # Start WS run loops in background tasks
            ws_task = asyncio.create_task(self._ws.run())
            
            if self._binance_ws:
                binance_task = asyncio.create_task(self._binance_ws.run())
            
            # Run periodic checks alongside WS
            while self._running:
                await asyncio.sleep(1)
                self._periodic_check()
            
            # Cancel WS tasks when stopping
            ws_task.cancel()
            try:
                await ws_task
            except asyncio.CancelledError:
                pass
            
            if binance_task:
                binance_task.cancel()
                try:
                    await binance_task
                except asyncio.CancelledError:
                    pass
                
        except asyncio.CancelledError:
            log.info("Orchestrator cancelled")
        finally:
            await self.stop()
    
    async def stop(self) -> None:
        """Stop the orchestrator."""
        self._running = False
        
        if self._ws:
            await self._ws.stop()
        
        if self._binance_ws:
            await self._binance_ws.close()
        
        # Save analytics
        self._trade_logger.save()
        
        log.info("VacuumOrchestrator stopped")
    
    # ============ WS Callbacks ============
    
    def _on_ws_state_change(self, old: WSState, new: WSState) -> None:
        """Handle WS state changes."""
        log.info(f"WS state: {old.name} → {new.name}")
        
        if new == WSState.DISCONNECTED:
            # Kill all positions on disconnect
            for state in self._states.values():
                state.kill_switch = True
    
    def _on_book_snapshot(self, symbol: str, data: Dict) -> None:
        """Handle book snapshot."""
        state = self._states.get(symbol)
        if not state:
            return
        
        # Apply snapshot to book manager
        if state.book.apply_snapshot(data):
            first_snapshot = state.tick_count == 0
            if first_snapshot:
                state.reset_buffers()
                log.info(f"[{symbol}] Book snapshot initialized")
            
            state.tick_count += 1
            
            # Update signals (LIR, vacuum, walls) from new book data
            state.update_signals()
        
        # Process
        self._maybe_decide(symbol, state)
    
    def _on_book_update(self, symbol: str, data: Dict) -> None:
        """Handle book delta update."""
        state = self._states.get(symbol)
        if not state:
            return
        
        # Apply delta
        if not state.book.apply_delta(data):
            return
        
        # Increment tick counter HERE (on every delta)
        state.tick_count += 1
        
        # Update signals
        state.update_signals()
        
        # Process
        self._maybe_decide(symbol, state)
    
    def _on_trade(self, symbol: str, data: Dict) -> None:
        """Handle trade update."""
        state = self._states.get(symbol)
        if not state:
            return
        
        # Forward to trade stream
        state.trades.on_trade(data)
    
    # Async wrapper methods for WSManager callbacks
    async def _on_book_snapshot_async(self, symbol: str, data: Dict) -> None:
        """Async wrapper for book snapshot handler."""
        self._on_book_snapshot(symbol, data)
    
    async def _on_book_update_async(self, symbol: str, data: Dict) -> None:
        """Async wrapper for book update handler."""
        self._on_book_update(symbol, data)
    
    async def _on_trade_async(self, symbol: str, data: Dict) -> None:
        """Async wrapper for trade handler."""
        self._on_trade(symbol, data)
    
    # ============ Binance Callbacks ============
    
    async def _on_binance_depth_async(self, symbol: str, data: Dict) -> None:
        """
        Handle Binance L2 depth DELTA update.
        
        Updates BinanceBookState (the LEADER) for structural signals.
        """
        binance_state = self._binance_states.get(symbol)
        if not binance_state:
            return
        
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        timestamp_ms = data.get("timestamp_ms")
        
        # Apply delta to local book
        binance_state.apply_depth_delta(bids, asks, timestamp_ms)
        
        # Log status periodically (every 40 ticks = ~20 seconds at 500ms)
        if binance_state.tick_count > 0 and binance_state.tick_count % 40 == 0:
            # Get Hyper mid for deviation display
            hyper_state = self._states.get(symbol)
            hyper_mid = hyper_state.book.top_of_book.mid if hyper_state else 0.0
            log.info(f"[{symbol}] Binance | {binance_state.get_status_line(hyper_mid)}")
    
    # ============ Decision Logic ============
    
    def _maybe_decide(self, symbol: str, state: VacuumCoinState) -> None:
        """Main decision logic - entry or exit."""
        now_ms = int(time.time() * 1000)
        
        # Check exit first if in position
        if state.has_position:
            self._check_exit(symbol, state, now_ms)
            return
        
        # Check entry
        self._check_entry(symbol, state, now_ms)
    
    def _check_entry(self, symbol: str, state: VacuumCoinState, now_ms: int) -> None:
        """Check entry conditions using StrategyEngine (v2.1)."""
        
        # Basic readiness
        if not state.can_enter(now_ms):
            # log.debug(f"[{symbol}] Cannot enter (cooldown/active)")
            return
        
        if state.tick_count < self._cfg.warm_up_ticks:
            # log.debug(f"[{symbol}] Warming up: {state.tick_count}/{self._cfg.warm_up_ticks}")
            return
        
        tob = state.book.top_of_book
        if not tob.is_valid:
            return
        
        # Entry filters
        avg_spread = state.get_avg_spread()
        if avg_spread > 0 and tob.spread > avg_spread * self._cfg.spread_cutoff:
            # log.debug(f"[{symbol}] Spread filter: {tob.spread:.1f}bps > {avg_spread * self._cfg.spread_cutoff:.1f}bps")
            return
        
        # Dead market filter
        micro_atr = state.get_micro_atr()
        if micro_atr > 0:
            atr_bps = micro_atr / tob.mid * 10000
            if atr_bps < self._cfg.atr_min_bps:
                # log.debug(f"[{symbol}] Low Volatility: ATR={atr_bps:.2f}bps < {self._cfg.atr_min_bps:.2f}")
                return
        
        # Get Hyperliquid signals
        vacuum = state.last_vacuum
        lir = state.last_lir
        
        if not lir or not lir.is_ready:
            return
            
        # Strategy Execution
        binance_state = self._binance_states.get(symbol)
        
        # Fix 8: Global Staleness Guard (if Binance enabled)
        if self._cfg.binance_enabled:
            if not binance_state or not binance_state.is_ready():
                log.debug(f"[{symbol}] Binance not ready")
                return
                
            if not binance_state.is_fresh(now_ms, max_stale_ms=2000): # Relaxed to 2000ms for testing
                log.debug(f"[{symbol}] Binance stale: last_upd={now_ms - binance_state.last_update_ms}ms > 400ms")
                # return # DISABLED FOR DEBUGGING
                pass
        
        # Create Strategy Context
        ctx = StrategyContext(
            symbol=symbol,
            hyper_state=state,
            binance_state=binance_state,
            config=self._cfg,
            now_ms=now_ms,
            lir=lir,
        )
        
        # Run all strategies
        signal = self._strategy_engine.check_all(ctx)
        
        if signal:
            # Map strategy to EntryMode (Explicit mapping)
            FADE_STRATEGIES = {"fade", "absorption", "fresh_wall", "lir_divergence"}
            BREAK_STRATEGIES = {"break_strat", "vacuum_fill", "churn_spike", "aggressor_momentum"}
            
            if signal.strategy_name in FADE_STRATEGIES:
                mode_enum = EntryMode.FADE
            elif signal.strategy_name in BREAK_STRATEGIES:
                mode_enum = EntryMode.BREAK
            else:
                # Default to FADE (safer, tighter targets)
                mode_enum = EntryMode.FADE
            
            log.info(f"[{symbol}] Signal: {signal.strategy_name} {signal.side} | {signal.reason}")
            
            self._execute_entry(
                symbol, state, signal.side.upper(), mode_enum,
                vacuum, lir, now_ms,
                signal_metadata=signal.metadata
            )
        
        else:
            # === HYPERLIQUID-ONLY FALLBACK ===
            
            # MOD 2: FADE (Wall Rejection)
            if state.wall_tracker.is_rejection("ask", self._cfg.min_rejection_ticks):
                if lir.lir_smooth < 1.0:
                    self._execute_entry(
                        symbol, state, "SELL", EntryMode.FADE,
                        vacuum, lir, now_ms,
                    )
                    return
            
            if state.wall_tracker.is_rejection("bid", self._cfg.min_rejection_ticks):
                if lir.lir_smooth > 1.0:
                    self._execute_entry(
                        symbol, state, "BUY", EntryMode.FADE,
                        vacuum, lir, now_ms,
                    )
                    return
            
            # MOD 1: BREAK (Vacuum Continuation)
            if vacuum and vacuum.is_valid:
                if vacuum.side == "ask" and lir.lir_smooth > self._cfg.lir_long_threshold:
                    self._execute_entry(
                        symbol, state, "BUY", EntryMode.BREAK,
                        vacuum, lir, now_ms,
                    )
                    return
                    
                if vacuum.side == "bid" and lir.lir_smooth < self._cfg.lir_short_threshold:
                    self._execute_entry(
                        symbol, state, "SELL", EntryMode.BREAK,
                        vacuum, lir, now_ms,
                    )
                    return
    
    def _check_exit(self, symbol: str, state: VacuumCoinState, now_ms: int) -> None:
        """Check exit conditions."""
        
        if state.exit_inflight:
            return
        
        tob = state.book.top_of_book
        if not tob.is_valid:
            return
        
        current_price = tob.microprice
        pnl_pct = self._compute_pnl_pct(state, current_price)
        
        # Determine Dynamic TP/SL (Scientific ATR-based)
        atr_bps = max(state.atr_bps, 0.4) # Min 0.4bps floor to prevent zero
        
        if state.entry_mode == EntryMode.BREAK.value:
            tp_bps = max(self._cfg.break_tp_min, atr_bps * self._cfg.break_tp_mult)
            sl_bps = max(self._cfg.break_sl_min, atr_bps * self._cfg.break_sl_mult)
            max_hold_limit = self._cfg.break_max_hold_sec
            
        elif state.entry_mode == EntryMode.FADE.value:
            tp_bps = max(self._cfg.fade_tp_min, atr_bps * self._cfg.fade_tp_mult)
            sl_bps = max(self._cfg.fade_sl_min, atr_bps * self._cfg.fade_sl_mult)
            max_hold_limit = self._cfg.fade_max_hold_sec
            
        else:
            # Fallback
            tp_bps = max(self._cfg.take_profit_bps, atr_bps * 2.0)
            sl_bps = max(self._cfg.stop_loss_bps, atr_bps * 3.0)
            max_hold_limit = self._cfg.max_hold_sec
            
        tp_pct = tp_bps / 10000
        sl_pct = sl_bps / 10000
        
        exit_reason = None
        
        # Take Profit with trailing extension
        if pnl_pct >= tp_pct:
            # Check if signal still favors current direction
            should_extend = False
            max_extensions = 3  # Max number of TP extensions
            
            if state.tp_trail_count < max_extensions:
                # Check cooldown
                if now_ms - state.last_tp_extend_ms > state.tp_extension_cooldown_ms:
                    # Check if LIR still favors our direction
                    lir = state.last_lir
                    if lir and lir.is_ready:
                        if state.position_side == "long" and lir.lir_smooth > 1.2:
                            should_extend = True
                        elif state.position_side == "short" and lir.lir_smooth < 0.8:
                            should_extend = True
            
            if should_extend:
                # Extend TP by moving entry price up (virtual trail)
                state.tp_trail_count += 1
                state.last_tp_extend_ms = now_ms
                # Effectively move our "entry" to lock in profits
                old_entry = state.avg_entry
                trail_pct = tp_pct * 0.5  # Lock in 50% of TP as new floor
                if state.position_side == "long":
                    state.avg_entry = old_entry * (1 + trail_pct)
                else:
                    state.avg_entry = old_entry * (1 - trail_pct)
                log.info(f"[{symbol}] TP EXTEND #{state.tp_trail_count} | LIR={lir.lir_smooth:.2f} | Entry trail {old_entry:.2f} → {state.avg_entry:.2f}")
            else:
                log.info(f"[{symbol}] TP TRIGGER | PnL={pnl_pct*10000:.2f}bps >= Target={tp_bps}bps (Mode={state.entry_mode}, Extensions={state.tp_trail_count})")
                exit_reason = "TP"
        
        # Stop Loss
        elif pnl_pct <= -sl_pct:
            log.info(f"[{symbol}] SL TRIGGER | PnL={pnl_pct*10000:.2f}bps <= Target={sl_bps}bps (Mode={state.entry_mode})")
            exit_reason = "SL"
        
        # Max hold time - only exit if PnL is positive (let SL handle losers)
        elif state.entry_time_ms > 0:
            hold_sec = (now_ms - state.entry_time_ms) / 1000
            if hold_sec >= max_hold_limit and pnl_pct > 0:
                exit_reason = "TIME"
        
        # Structural invalidation: entry wall broken
        if state.entry_mode == EntryMode.FADE.value and state.entry_wall_price > 0:
            if state.position_side == "long":
                # Faded from bid wall rejection - wall should hold
                if tob.best_bid < state.entry_wall_price * 0.995:
                    exit_reason = "WALL_BROKEN"
            elif state.position_side == "short":
                # Faded from ask wall rejection - wall should hold
                if tob.best_ask > state.entry_wall_price * 1.005:
                    exit_reason = "WALL_BROKEN"
        
        if exit_reason:
            self._execute_exit(symbol, state, exit_reason, now_ms)
    
    def _compute_pnl_pct(self, state: VacuumCoinState, current_price: float) -> float:
        """Compute unrealized PnL percentage."""
        if not state.has_position or state.avg_entry <= 0:
            return 0.0
        
        if state.position_qty > 0:  # Long
            return (current_price - state.avg_entry) / state.avg_entry
        else:  # Short
            return (state.avg_entry - current_price) / state.avg_entry
    
    # ============ Execution ============
    
    def _execute_entry(
        self,
        symbol: str,
        state: VacuumCoinState,
        side: str,
        mode: EntryMode,
        vacuum: Optional[VacuumSignal],
        lir: LIRSignal,
        now_ms: int,
        signal_metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Execute entry order."""
        
        tob = state.book.top_of_book
        price = tob.best_ask if side == "BUY" else tob.best_bid
        
        # Calculate quantity
        qty = self._cfg.position_size_usd / price if price > 0 else 0
        if qty <= 0:
            return
        
        # Capture snapshot
        snapshot = state.capture_decision_snapshot(mode.value, vacuum, lir)
        
        # Log signal
        log.info(
            f"[{symbol}] ENTRY SIGNAL | "
            f"mode={mode.value} side={side} "
            f"LIR={lir.lir_smooth:.2f} "
            f"vacuum={'Y' if vacuum and vacuum.is_valid else 'N'} "
            f"price={price:.2f}"
        )
        
        # Shadow mode - log only
        if self._execution_mode == ExecutionMode.SHADOW:
            self._trade_logger.log_shadow_signal({
                "symbol": symbol,
                "side": side,
                "mode": mode.value,
                "price": price,
                "lir": lir.lir_smooth,
                "vacuum_bps": vacuum.vacuum_bps if vacuum else 0,
                "timestamp_ms": now_ms,  
            })
            
            # MOCK STATE UPDATE (Crucial for testing exit logic)
            # state.has_position is a property, determined by position_qty
            state.position_qty = qty if side == "BUY" else -qty
            state.avg_entry = price
            state.entry_time_ms = now_ms
            state.entry_mode = mode.value  # Fix: Capture entry mode for exit logic
            # state.position_side is also a property derived from qty
            state.entry_wall_price = signal_metadata.get("wall_price", 0.0) if signal_metadata else 0.0
            
            log.info(f"[{symbol}] SHADOW FILL | {side} {qty:.4f} @ {price:.2f}")
            return
        
        # Create order
        order = self._order_manager.create_order(
            symbol=symbol,
            side=side,
            qty=qty,
            intent=OrderIntent.ENTRY,
            limit_price=None,  # Market/IOC
            decision_snapshot=snapshot,
        )
        
        if order is None:
            log.warning(f"[{symbol}] Failed to create entry order")
            return
        
        # Set locks and track wall for fade mode
        state.entry_inflight = True
        if mode == EntryMode.FADE:
            if side == "SELL" and state.last_ask_wall:
                state.entry_wall_price = state.last_ask_wall.price
            elif side == "BUY" and state.last_bid_wall:
                state.entry_wall_price = state.last_bid_wall.price
        
        # Execute (would call exchange here)
        log.info(f"[{symbol}] ORDER SENT | {order.client_id} {side} {qty:.4f}")
        
        # For now, simulate immediate fill in paper/live
        self._simulate_fill(order, price, qty)
    
    def _execute_exit(
        self,
        symbol: str,
        state: VacuumCoinState,
        reason: str,
        now_ms: int,
    ) -> None:
        """Execute exit order."""
        
        if not state.has_position:
            return
        
        tob = state.book.top_of_book
        side = "SELL" if state.position_qty > 0 else "BUY"
        qty = abs(state.position_qty)
        price = tob.best_bid if side == "SELL" else tob.best_ask
        
        # Calculate PnL and Fees
        # Fee assumption: Taker on both ends (worst case) or check config
        fee_rate = self._cfg.taker_fee_bps / 10000.0
        
        entry_val = state.avg_entry * qty
        exit_val = price * qty
        
        entry_fee = entry_val * fee_rate
        exit_fee = exit_val * fee_rate
        
        gross_pnl = 0.0
        if state.position_side == "long":
            gross_pnl = exit_val - entry_val
        else:
            gross_pnl = entry_val - exit_val
            
        net_pnl = gross_pnl - entry_fee - exit_fee
        
        log.info(
            f"[{symbol}] EXIT SIGNAL | "
            f"reason={reason} side={side} qty={qty:.4f} "
            f"entry={state.avg_entry:.2f} current={price:.2f} "
            f"NetPnL={net_pnl:.4f} ({net_pnl/entry_val*100:.2f}%)"
        )
        
        # Shadow mode
        if self._execution_mode == ExecutionMode.SHADOW:
            pnl_pct = self._compute_pnl_pct(state, price)
            self._trade_logger.log_shadow_signal({
                "symbol": symbol,
                "side": side,
                "reason": reason,
                "entry_price": state.avg_entry,
                "exit_price": price,
                "pnl_pct": pnl_pct,
                "mode": state.entry_mode,
                "timestamp_ms": now_ms,
            })
            # Clear position in shadow
            state.close_position()
            return
        
        # Create exit order
        snapshot = state.capture_decision_snapshot(
            state.entry_mode,
            state.last_vacuum,
            state.last_lir,
        )
        
        order = self._order_manager.create_order(
            symbol=symbol,
            side=side,
            qty=qty,
            intent=OrderIntent.EXIT_TPSL if reason in ("TP", "SL") else OrderIntent.EXIT_RISK,
            limit_price=None,
            decision_snapshot=snapshot,
        )
        
        if order is None:
            log.warning(f"[{symbol}] Failed to create exit order")
            return
        
        state.exit_inflight = True
        
        log.info(f"[{symbol}] EXIT SENT | {order.client_id} {side} {qty:.4f}")
        
        # Simulate fill
        self._simulate_fill(order, price, qty)
    
    def _simulate_fill(self, order: Order, price: float, qty: float) -> None:
        """Simulate order fill (for paper/testing)."""
        # Mark as filled
        self._order_manager.on_fill(
            client_id=order.client_id,
            fill_qty=qty,
            fill_price=price,
            fee=price * qty * self._cfg.taker_fee_bps / 10000,
        )
    
    # ============ Order Callbacks ============
    
    def _on_fill(self, order: Order, fill_qty: float, fill_price: float) -> None:
        """Handle order fill."""
        symbol = order.symbol
        state = self._states.get(symbol)
        if not state:
            return
        
        now_ms = int(time.time() * 1000)
        
        if order.is_entry:
            signed_qty = fill_qty if order.side == "BUY" else -fill_qty
            state.update_position(signed_qty, fill_price, now_ms, order.intent.value)
            log.info(f"[{symbol}] FILLED ENTRY | pos={state.position_qty:.4f} @ {fill_price:.2f}")
        else:
            # Exit fill
            pnl_pct = self._compute_pnl_pct(state, fill_price)
            log.info(f"[{symbol}] FILLED EXIT | pnl={pnl_pct*100:.3f}%")
            
            # Log trade
            self._trade_logger.log_trade({
                "symbol": symbol,
                "side": order.side,
                "entry": state.avg_entry,
                "exit": fill_price,
                "qty": fill_qty,
                "pnl_pct": pnl_pct,
                "mode": state.entry_mode,
                "reason": order.intent.value,
            })
            
            state.close_position()
            state.cooldown_until_ms = now_ms + self._cfg.cooldown_ms
    
    def _on_order_terminal(self, order: Order) -> None:
        """Handle order reaching terminal state."""
        symbol = order.symbol
        state = self._states.get(symbol)
        if not state:
            return
        
        # Release locks
        if order.is_entry:
            state.entry_inflight = False
        else:
            state.exit_inflight = False
    
    # ============ Periodic ============
    
    def _periodic_check(self) -> None:
        """Periodic status logging and checks."""
        now_ms = int(time.time() * 1000)
        
        # Log status every 30 seconds
        if now_ms - self._last_status_log_ms > 30000:
            self._last_status_log_ms = now_ms
            
            for sym, state in self._states.items():
                tob = state.book.top_of_book
                lir = state.last_lir
                
                lir_val = lir.lir_smooth if lir else 0
                
                log.info(
                    f"[{sym}] STATUS | "
                    f"ticks={state.tick_count} "
                    f"ready={'Y' if state.is_system_ready else 'N'} "
                    f"mid={tob.mid:.2f} "
                    f"LIR={lir_val:.2f} "
                    f"pos={state.position_qty:.4f}"
                )
