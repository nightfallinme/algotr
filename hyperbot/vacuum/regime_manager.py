# hyperbot/vacuum/regime_manager.py
"""
Regime Manager v2 - RANGE vs MOMENTUM classification with hysteresis.

v2 Changes:
- Directional vacuum: vacuum_up for UP break, vacuum_down for DOWN break
- Two-sided wall scoring: consider both walls for range stability
- Better momentum direction: uses eroding wall side + matching vacuum

Implements score-based regime decision with:
- Score_range: Wall strength × persistence × (1 - erosion)
- Score_mom: Erosion + (1 - vacuum_direction)
- Hysteresis: Different thresholds for enter/exit
- Hold time: Minimum duration before regime switch
"""
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum, auto
import time

from .wall_analyzer import WallCandidate, WallState


class Regime(Enum):
    """Market regime classification."""
    UNKNOWN = auto()
    RANGE_MAKER = auto()     # Mean-reversion, fade with maker
    MOMENTUM_BREAK = auto()  # Continuation, break with taker


@dataclass
class RegimeConfig:
    """Regime manager configuration."""
    # Score thresholds
    theta_range_enter: float = 1.2
    theta_range_exit: float = 0.9
    theta_mom_enter: float = 1.0
    theta_mom_exit: float = 0.7
    
    # Hysteresis
    min_regime_hold_sec: float = 5.0
    
    # Vacuum threshold
    vac_thin_threshold: float = 0.25
    
    # v2: Two-sided range scoring weights
    range_max_weight: float = 0.6  # Weight for stronger wall
    range_min_weight: float = 0.4  # Weight for weaker wall


@dataclass
class RegimeState:
    """Current regime state."""
    regime: Regime = Regime.UNKNOWN
    last_change_ms: int = 0
    score_range: float = 0.0
    score_mom: float = 0.0
    confidence: float = 0.0
    
    # v2: Break direction info
    break_direction: str = ""  # "up" or "down"


class RegimeManager:
    """
    Score-based regime manager v2 with hysteresis.
    
    v2 improvements:
    - Directional vacuum for momentum scoring
    - Two-sided wall scoring for range stability
    - Clear break direction tracking
    
    Usage:
        mgr = RegimeManager(config)
        regime = mgr.update(bid_wall, ask_wall, vacuum_up, vacuum_down, now_ms)
    """
    
    def __init__(self, config: Optional[RegimeConfig] = None):
        self.config = config or RegimeConfig()
        self._state = RegimeState()
        self._history: list = []
    
    def update(
        self,
        bid_wall: Optional[WallCandidate],
        ask_wall: Optional[WallCandidate],
        vacuum_up: float,    # v2: 0-1, lower = more vacuum for UP break
        vacuum_down: float,  # v2: 0-1, lower = more vacuum for DOWN break
        now_ms: Optional[int] = None,
    ) -> Regime:
        """
        Update regime based on wall and vacuum state (v2: directional).
        
        Args:
            bid_wall: Strongest bid wall (or None)
            ask_wall: Strongest ask wall (or None)
            vacuum_up: Vacuum fill ratio for UP direction (lower = more vacuum)
            vacuum_down: Vacuum fill ratio for DOWN direction
            now_ms: Current timestamp
        
        Returns:
            Current regime
        """
        now_ms = now_ms or int(time.time() * 1000)
        cfg = self.config
        
        # v2: Calculate scores for both sides
        score_range, eroding_side, break_dir = self._calc_scores_v2(
            bid_wall, ask_wall, vacuum_up, vacuum_down
        )
        
        # Calculate momentum score based on eroding side
        if eroding_side == "ask":
            # Ask wall eroding -> UP break potential
            score_mom = self._calc_momentum_score_v2(ask_wall, vacuum_up)
            break_direction = "up"
        elif eroding_side == "bid":
            # Bid wall eroding -> DOWN break potential  
            score_mom = self._calc_momentum_score_v2(bid_wall, vacuum_down)
            break_direction = "down"
        else:
            score_mom = 0.0
            break_direction = ""
        
        self._state.score_range = score_range
        self._state.score_mom = score_mom
        self._state.break_direction = break_direction
        
        # Check hysteresis hold time
        hold_elapsed_sec = (now_ms - self._state.last_change_ms) / 1000.0
        can_switch = hold_elapsed_sec >= cfg.min_regime_hold_sec
        
        current = self._state.regime
        new_regime = current
        
        if can_switch:
            new_regime = self._decide_regime(
                current, score_range, score_mom, bid_wall, ask_wall
            )
        
        # Update state if changed
        if new_regime != current:
            self._state.regime = new_regime
            self._state.last_change_ms = now_ms
            self._history.append((now_ms, new_regime))
        
        # Confidence
        if new_regime == Regime.RANGE_MAKER:
            self._state.confidence = min(1.0, score_range / cfg.theta_range_enter)
        elif new_regime == Regime.MOMENTUM_BREAK:
            self._state.confidence = min(1.0, score_mom / cfg.theta_mom_enter)
        else:
            self._state.confidence = 0.0
        
        return self._state.regime
    
    def _calc_scores_v2(
        self,
        bid_wall: Optional[WallCandidate],
        ask_wall: Optional[WallCandidate],
        vacuum_up: float,
        vacuum_down: float,
    ) -> tuple[float, str, str]:
        """
        v2.1: Calculate range score + find deterministic break direction.
        
        Returns: (score_range, eroding_side, break_direction)
        """
        cfg = self.config
        
        # Calculate individual wall scores
        bid_score = self._calc_wall_range_score(bid_wall) if bid_wall else 0.0
        ask_score = self._calc_wall_range_score(ask_wall) if ask_wall else 0.0
        
        # v2: Two-sided range scoring
        max_score = max(bid_score, ask_score)
        min_score = min(bid_score, ask_score)
        score_range = cfg.range_max_weight * max_score + cfg.range_min_weight * min_score
        
        # v2.1: Deterministic break direction
        # Calculate momentum potential for both directions
        # mom = E + (1 - vac)
        
        E_ask = 0.0
        if ask_wall and ask_wall.state in {WallState.ERODING, WallState.BROKEN}:
            E_ask = min(1.0, ask_wall.erosion)
            # Bonus for BROKEN
            if ask_wall.state == WallState.BROKEN:
                E_ask = max(E_ask, 0.5)
        
        E_bid = 0.0
        if bid_wall and bid_wall.state in {WallState.ERODING, WallState.BROKEN}:
            E_bid = min(1.0, bid_wall.erosion)
            if bid_wall.state == WallState.BROKEN:
                E_bid = max(E_bid, 0.5)
        
        mom_up = E_ask + (1 - vacuum_up)
        mom_down = E_bid + (1 - vacuum_down)
        
        break_direction = ""
        eroding_side = ""
        
        # Select stronger direction if it meets threshold
        # (Threshold check happens in decide_regime, here we just find max)
        if mom_up > mom_down:
            if mom_up > 0.5: # Minimum filtering
                break_direction = "up"
                eroding_side = "ask"
        else:
            if mom_down > 0.5:
                break_direction = "down"
                eroding_side = "bid"
        
        return score_range, eroding_side, break_direction
    
    def _calc_wall_range_score(self, wall: WallCandidate) -> float:
        """Calculate range score for a single wall."""
        if wall.state == WallState.FAKE:
            return 0.0
        
        S = wall.S
        P = wall.persistence
        E = min(1.0, wall.erosion)
        
        return S * P * (1 - E)
    
    def _calc_momentum_score_v2(
        self, wall: Optional[WallCandidate], vacuum_dir: float
    ) -> float:
        """
        v2: Calculate momentum score with directional vacuum.
        
        Score_mom = E + (1 - vacuum_dir)
        Higher = stronger break setup
        """
        if wall is None:
            return 1 - vacuum_dir  # No wall means vacuum ahead
        
        E = min(1.0, wall.erosion)
        vac = 1 - vacuum_dir  # Invert: 0 vacuum_fill = 1.0 score
        
        return E + vac
    
    def _decide_regime(
        self,
        current: Regime,
        score_range: float,
        score_mom: float,
        bid_wall: Optional[WallCandidate],
        ask_wall: Optional[WallCandidate],
    ) -> Regime:
        """Decide regime with hysteresis."""
        cfg = self.config
        
        # Check if at least one wall is stable
        has_stable = (
            (bid_wall and bid_wall.state == WallState.STABLE) or
            (ask_wall and ask_wall.state == WallState.STABLE)
        )
        
        # Check if any wall is eroding
        has_eroding = (
            (bid_wall and bid_wall.state in {WallState.ERODING, WallState.BROKEN}) or
            (ask_wall and ask_wall.state in {WallState.ERODING, WallState.BROKEN})
        )
        
        # RANGE entry/exit
        if current != Regime.RANGE_MAKER:
            # Entry: need higher threshold + stable wall
            if score_range > cfg.theta_range_enter and has_stable:
                return Regime.RANGE_MAKER
        else:
            # Exit: lower threshold
            if score_range < cfg.theta_range_exit:
                if score_mom > cfg.theta_mom_enter:
                    return Regime.MOMENTUM_BREAK
        
        # MOMENTUM entry/exit
        if current != Regime.MOMENTUM_BREAK:
            # Entry: need eroding wall + vacuum + score
            if score_mom > cfg.theta_mom_enter and has_eroding:
                return Regime.MOMENTUM_BREAK
        else:
            # Exit: lower threshold
            if score_mom < cfg.theta_mom_exit:
                if score_range > cfg.theta_range_enter:
                    return Regime.RANGE_MAKER
        
        # Default: stay in current or UNKNOWN
        if current == Regime.UNKNOWN:
            if score_range > cfg.theta_range_enter:
                return Regime.RANGE_MAKER
            if score_mom > cfg.theta_mom_enter:
                return Regime.MOMENTUM_BREAK
        
        return current
    
    @property
    def regime(self) -> Regime:
        return self._state.regime
    
    @property
    def confidence(self) -> float:
        return self._state.confidence
    
    @property
    def break_direction(self) -> str:
        """v2: Get expected break direction (up/down)."""
        return self._state.break_direction
    
    @property
    def state(self) -> RegimeState:
        return self._state
    
    def is_range(self) -> bool:
        return self._state.regime == Regime.RANGE_MAKER
    
    def is_momentum(self) -> bool:
        return self._state.regime == Regime.MOMENTUM_BREAK
