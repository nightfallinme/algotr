# hyperbot/maker/maker_config.py
"""
Maker mode configuration with type-safe dataclasses.
"""
from dataclasses import dataclass, field
from typing import Dict, Any


@dataclass
class MakerRegimeConfig:
    """Regime detection thresholds for activating maker mode."""
    volz_max: float = 0.9           # Activate only when volZ < this
    micro_atr_bps_max: float = 6.0  # Activate only when ATR < this bps
    spreadz_max: float = 1.2        # Activate only when spreadZ < this


@dataclass
class RegimeDetectionConfig:
    """Advanced regime detection with hysteresis and voting."""
    # Hysteresis thresholds (prevent flip-flops)
    to_momentum_volz: float = 1.70      # volZ must exceed this to switch to MOMENTUM
    to_range_volz: float = 1.10         # volZ must drop below this to switch to RANGE
    
    # Dual condition: ATR confirmation for MOMENTUM
    to_momentum_atr_bps: float = 1.0    # microATR must exceed this for MOMENTUM
    
    # Spread thresholds
    to_momentum_spreadz: float = 2.5    # spreadZ for MOMENTUM
    to_range_spreadz: float = 1.5       # spreadZ for RANGE
    
    # Hold time (prevent rapid switching)
    min_hold_sec: float = 30.0          # Minimum seconds in a regime before switching
    
    # Voting (debounce)
    vote_window: int = 10               # Number of recent samples to consider
    vote_needed: int = 7                # Votes needed to confirm regime change
    
    # Gray zone
    gray_zone_enabled: bool = True      # Enable gray zone between thresholds
    ofi_spike_abs_gray: float = 0.45    # Higher spike threshold in gray zone


@dataclass
class MakerConfig:
    """
    Maker-First Micro-Reversion configuration.
    
    This strategy FADES OFI spikes in low-range markets,
    expecting mean reversion to mid-price.
    """
    # Master toggle
    enabled: bool = False
    
    # Regime detection (legacy thresholds)
    regime: MakerRegimeConfig = field(default_factory=MakerRegimeConfig)
    
    # Advanced regime detection (hysteresis, voting)
    regime_detection: Dict[str, Any] = field(default_factory=dict)
    
    # OFI Spike Detection (for FADE triggers)
    ofi_spike_abs: float = 0.35         # |OFI| > this = spike (HIGHER than taker!)
    ofi_spike_confirm_ticks: int = 1    # Just 1 tick (no persistence needed)
    
    # Binance Veto (INVERTED for fade strategy)
    # Block fade if Binance CONFIRMS Hyper direction (real impulse)
    binance_confirm_blocks: bool = True
    binance_min_ofi: float = 0.15
    
    # Entry (Maker Limit Placement)
    entry_offset_bps: float = 0.5       # How far from BBO (tight)
    
    # Order Management
    order_timeout_sec: float = 3.0      # Cancel if not filled
    cooldown_after_cancel_ms: int = 500
    max_replace_count: int = 1          # Max times to re-place order
    
    # Exit Targets
    tp_mid_offset_bps: float = 3.0      # Minimum TP from mid
    tp_max_bps: float = 6.0             # Maximum TP
    sl_bps: float = 4.0                 # Tight stop loss
    max_hold_sec: float = 30.0          # Max hold time
    
    # Prefer Maker Exit
    prefer_maker_exit: bool = True
    maker_exit_timeout_sec: float = 2.0  # Switch to taker if not filled
    
    # Safety
    max_position_usd: float = 100.0     # Max position size
    max_daily_trades: int = 100         # Circuit breaker
    max_daily_loss_bps: float = 50.0    # Daily loss limit
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MakerConfig":
        """Load MakerConfig from dictionary (YAML section)."""
        cfg = cls()
        
        if not data:
            return cfg
        
        # Master toggle
        cfg.enabled = data.get("enabled", cfg.enabled)
        
        # Regime detection (legacy)
        regime_data = data.get("regime", {})
        cfg.regime = MakerRegimeConfig(
            volz_max=regime_data.get("volz_max", cfg.regime.volz_max),
            micro_atr_bps_max=regime_data.get("micro_atr_bps_max", cfg.regime.micro_atr_bps_max),
            spreadz_max=regime_data.get("spreadz_max", cfg.regime.spreadz_max),
        )
        
        # Advanced regime detection (hysteresis, voting, hold time)
        cfg.regime_detection = data.get("regime_detection", {
            "to_momentum_volz": 1.70,
            "to_range_volz": 1.10,
            "to_momentum_atr_bps": 1.0,
            "to_momentum_spreadz": 2.5,
            "to_range_spreadz": 1.5,
            "min_hold_sec": 30.0,
            "vote_window": 10,
            "vote_needed": 7,
            "gray_zone_enabled": True,
            "ofi_spike_abs_gray": 0.45,
        })
        
        # OFI Spike Detection
        cfg.ofi_spike_abs = data.get("ofi_spike_abs", cfg.ofi_spike_abs)
        cfg.ofi_spike_confirm_ticks = data.get("ofi_spike_confirm_ticks", cfg.ofi_spike_confirm_ticks)
        
        # Binance Veto
        cfg.binance_confirm_blocks = data.get("binance_confirm_blocks", cfg.binance_confirm_blocks)
        cfg.binance_min_ofi = data.get("binance_min_ofi", cfg.binance_min_ofi)
        
        # Entry
        cfg.entry_offset_bps = data.get("entry_offset_bps", cfg.entry_offset_bps)
        
        # Order Management
        cfg.order_timeout_sec = data.get("order_timeout_sec", cfg.order_timeout_sec)
        cfg.cooldown_after_cancel_ms = data.get("cooldown_after_cancel_ms", cfg.cooldown_after_cancel_ms)
        cfg.max_replace_count = data.get("max_replace_count", cfg.max_replace_count)
        
        # Exit Targets
        cfg.tp_mid_offset_bps = data.get("tp_mid_offset_bps", cfg.tp_mid_offset_bps)
        cfg.tp_max_bps = data.get("tp_max_bps", cfg.tp_max_bps)
        cfg.sl_bps = data.get("sl_bps", cfg.sl_bps)
        cfg.max_hold_sec = data.get("max_hold_sec", cfg.max_hold_sec)
        
        # Maker Exit
        cfg.prefer_maker_exit = data.get("prefer_maker_exit", cfg.prefer_maker_exit)
        cfg.maker_exit_timeout_sec = data.get("maker_exit_timeout_sec", cfg.maker_exit_timeout_sec)
        
        # Safety
        cfg.max_position_usd = data.get("max_position_usd", cfg.max_position_usd)
        cfg.max_daily_trades = data.get("max_daily_trades", cfg.max_daily_trades)
        cfg.max_daily_loss_bps = data.get("max_daily_loss_bps", cfg.max_daily_loss_bps)
        
        return cfg

