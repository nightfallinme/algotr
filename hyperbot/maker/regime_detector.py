# hyperbot/maker/regime_detector.py
"""
Market regime detection for switching between Taker and Maker strategies.

Implements robust regime detection with:
- Hysteresis (separate entry/exit thresholds)
- Minimum hold time (prevents rapid flip-flops)
- N-of-M voting (debounce via recent history)
- Dual condition (volZ + ATR for confirmation)
"""
from enum import Enum, auto
from dataclasses import dataclass
from typing import Optional, List
from collections import deque

from ..ofi.coin_state import CoinState
from ..ofi.filters import VolatilityFilter, SpreadFilter, FilterResult
from .maker_config import MakerConfig
from ..logger import get_logger

log = get_logger(__name__)


class MarketRegime(Enum):
    """Market regime classification."""
    UNKNOWN = auto()        # Not enough data
    RANGE_MAKER = auto()    # Low-range, use maker strategy
    MOMENTUM_TAKER = auto() # Trending/volatile, use taker strategy
    GRAY_ZONE = auto()      # Uncertain, both strategies conservative


@dataclass
class RegimeSnapshot:
    """Snapshot of regime detection values."""
    regime: MarketRegime
    volz: float
    micro_atr_bps: float
    spreadz: float
    timestamp_ms: int
    in_gray_zone: bool = False


@dataclass
class RegimeConfig:
    """Regime detection configuration with hysteresis."""
    # Hysteresis thresholds (RANGE -> MOMENTUM requires higher, MOMENTUM -> RANGE lower)
    to_momentum_volz: float = 1.70       # volZ must exceed this to switch to MOMENTUM
    to_range_volz: float = 1.10          # volZ must drop below this to switch to RANGE
    
    # Dual condition: ATR confirmation for MOMENTUM
    to_momentum_atr_bps: float = 1.0     # microATR must exceed this for MOMENTUM
    
    # Spread thresholds
    to_momentum_spreadz: float = 2.5     # spreadZ for MOMENTUM
    to_range_spreadz: float = 1.5        # spreadZ for RANGE
    
    # Hold time (prevent rapid switching)
    min_hold_sec: float = 30.0           # Minimum seconds in a regime before switching
    
    # Voting (debounce)
    vote_window: int = 10                # Number of recent samples to consider
    vote_needed: int = 7                 # Votes needed to confirm regime change
    
    # Gray zone handling
    gray_zone_enabled: bool = True       # Enable gray zone between thresholds


class RegimeDetector:
    """
    Detects market regime to determine which strategy to use.
    
    Low-Range Regime (RANGE_MAKER):
    - Low volatility (volZ < to_range_volz)
    - Low micro ATR (< threshold bps)
    - Tight spreads (spreadZ < to_range_spreadz)
    
    Momentum Regime (MOMENTUM_TAKER):
    - High volatility (volZ > to_momentum_volz)
    - High micro ATR (> to_momentum_atr_bps)
    - OR wide spreads (spreadZ > to_momentum_spreadz)
    
    Gray Zone (between thresholds):
    - Conservative behavior for both strategies
    """
    
    def __init__(
        self,
        vol_filter: VolatilityFilter,
        spread_filter: SpreadFilter,
        config: MakerConfig,
    ):
        self.vol_filter = vol_filter
        self.spread_filter = spread_filter
        self.maker_config = config
        
        # Extract regime config from maker config or use defaults
        regime_cfg = config.regime_detection if hasattr(config, 'regime_detection') else {}
        self.config = RegimeConfig(
            to_momentum_volz=regime_cfg.get('to_momentum_volz', 1.70),
            to_range_volz=regime_cfg.get('to_range_volz', 1.10),
            to_momentum_atr_bps=regime_cfg.get('to_momentum_atr_bps', 1.0),
            to_momentum_spreadz=regime_cfg.get('to_momentum_spreadz', 2.5),
            to_range_spreadz=regime_cfg.get('to_range_spreadz', 1.5),
            min_hold_sec=regime_cfg.get('min_hold_sec', 30.0),
            vote_window=regime_cfg.get('vote_window', 10),
            vote_needed=regime_cfg.get('vote_needed', 7),
            gray_zone_enabled=regime_cfg.get('gray_zone_enabled', True),
        )
        
        # State
        self._current_regime = MarketRegime.UNKNOWN
        self._last_change_ms: int = 0
        self._min_hold_ms = int(self.config.min_hold_sec * 1000)
        
        # Voting history: deque of (timestamp_ms, regime_vote)
        self._vote_history: deque = deque(maxlen=self.config.vote_window)
        
        # Logging throttle
        self._last_log_ms: int = 0
        self._log_interval_ms = 60000  # Log every 60s
    
    def _classify_tick(self, volz: float, atr_bps: float, spreadz: float) -> MarketRegime:
        """Classify a single tick as RANGE, MOMENTUM, or GRAY_ZONE."""
        
        # Clear MOMENTUM: volZ high AND (ATR high OR spreadZ high)
        is_momentum = (
            volz >= self.config.to_momentum_volz and
            (atr_bps >= self.config.to_momentum_atr_bps or spreadz >= self.config.to_momentum_spreadz)
        )
        
        # Clear RANGE: volZ low AND spreadZ low
        is_range = (
            volz <= self.config.to_range_volz and
            spreadz <= self.config.to_range_spreadz
        )
        
        if is_momentum:
            return MarketRegime.MOMENTUM_TAKER
        elif is_range:
            return MarketRegime.RANGE_MAKER
        else:
            return MarketRegime.GRAY_ZONE
    
    def _count_votes(self, target_regime: MarketRegime) -> int:
        """Count how many votes in history match target regime."""
        return sum(1 for _, vote in self._vote_history if vote == target_regime)
    
    def detect(self, state: CoinState, now_ms: int) -> RegimeSnapshot:
        """
        Detect current market regime based on volatility and spread metrics.
        
        Args:
            state: Current coin state with book and indicators
            now_ms: Current timestamp in milliseconds
            
        Returns:
            RegimeSnapshot with classification and metrics
        """
        # Get filter values
        vol_result = self.vol_filter.check(state)
        spread_result = self.spread_filter.check(state)
        
        # Calculate micro ATR in bps
        tob = state.book.top_of_book
        micro_atr = state.micro_atr_ema.value or 0.0
        micro_atr_bps = (micro_atr / tob.microprice) * 10000 if tob.microprice > 0 else 0.0
        
        # Classify this tick
        tick_vote = self._classify_tick(vol_result.value, micro_atr_bps, spread_result.value)
        
        # Add to voting history
        self._vote_history.append((now_ms, tick_vote))
        
        # Check if hold time has elapsed
        hold_elapsed = (now_ms - self._last_change_ms) >= self._min_hold_ms
        
        # Only consider regime change if hold time elapsed
        new_regime = self._current_regime
        in_gray_zone = False
        
        if hold_elapsed and len(self._vote_history) >= self.config.vote_needed:
            # Count votes for each regime
            momentum_votes = self._count_votes(MarketRegime.MOMENTUM_TAKER)
            range_votes = self._count_votes(MarketRegime.RANGE_MAKER)
            gray_votes = self._count_votes(MarketRegime.GRAY_ZONE)
            
            # Determine new regime based on voting
            if momentum_votes >= self.config.vote_needed:
                new_regime = MarketRegime.MOMENTUM_TAKER
            elif range_votes >= self.config.vote_needed:
                new_regime = MarketRegime.RANGE_MAKER
            elif self.config.gray_zone_enabled and gray_votes >= self.config.vote_needed:
                # Stay in gray zone
                in_gray_zone = True
                # Don't change regime, but mark as gray zone
            # else: not enough consensus, keep current regime
            
            # Apply regime change if different
            if new_regime != self._current_regime and new_regime != MarketRegime.UNKNOWN:
                self._current_regime = new_regime
                self._last_change_ms = now_ms
                log.info(
                    f"[{state.symbol}] Regime change: {new_regime.name} "
                    f"(volZ={vol_result.value:.2f} atr={micro_atr_bps:.3f}bps spreadZ={spread_result.value:.2f}) "
                    f"[votes: M={momentum_votes} R={range_votes} G={gray_votes}]"
                )
        
        # Check if currently in gray zone (even if regime didn't change)
        if tick_vote == MarketRegime.GRAY_ZONE:
            in_gray_zone = True
        
        # Periodic logging
        if now_ms - self._last_log_ms > self._log_interval_ms:
            self._last_log_ms = now_ms
            momentum_votes = self._count_votes(MarketRegime.MOMENTUM_TAKER)
            range_votes = self._count_votes(MarketRegime.RANGE_MAKER)
            log.info(
                f"[{state.symbol}] Regime: {self._current_regime.name} | "
                f"volZ={vol_result.value:.2f} atr={micro_atr_bps:.3f}bps spreadZ={spread_result.value:.2f} | "
                f"votes M={momentum_votes} R={range_votes}"
            )
        
        return RegimeSnapshot(
            regime=self._current_regime,
            volz=vol_result.value,
            micro_atr_bps=micro_atr_bps,
            spreadz=spread_result.value,
            timestamp_ms=now_ms,
            in_gray_zone=in_gray_zone,
        )
    
    @property
    def current_regime(self) -> MarketRegime:
        """Get current regime without recalculating."""
        return self._current_regime
    
    def reset(self) -> None:
        """Reset regime state."""
        self._current_regime = MarketRegime.UNKNOWN
        self._last_change_ms = 0
        self._vote_history.clear()
