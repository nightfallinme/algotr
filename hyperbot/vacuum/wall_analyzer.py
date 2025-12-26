# hyperbot/vacuum/wall_analyzer.py
"""
Industrial-Grade Wall Analysis Module v2.

Key v2 Changes:
- Bin-based wall tracking (price cluster instead of exact price)
- Normalized erosion using r=q/D_med
- Eat rate integration (when aggressor data available)

Key Components:
- WallStrength: r × w_dist composite score
- WallState: STABLE, ERODING, BROKEN, FAKE
- Persistence: Presence ratio over time window
- Churn: Volume volatility for spoof detection
- Erosion: EWM slope on normalized r + sharp drop detection
"""
from dataclasses import dataclass, field
from typing import Optional, List, Tuple, Dict
from enum import Enum, auto
from collections import deque
import time
import math


class WallState(Enum):
    """Wall classification states."""
    UNKNOWN = auto()
    STABLE = auto()      # P >= 0.7, E < E_min, C < C_max
    ERODING = auto()     # E >= E_min or sharp drop
    BROKEN = auto()      # r < r_min for T_break duration
    FAKE = auto()        # P < 0.6 or C > C_max (spoof)


@dataclass
class WallCandidate:
    """Single wall candidate with all metrics."""
    price: float
    size: float
    side: str  # "bid" | "ask"
    bin_id: int = 0       # v2: bin identifier for tracking
    
    # Normalized metrics
    r: float = 0.0           # size / D_med (ratio)
    z: float = 0.0           # z-score (robust)
    d_bps: float = 0.0       # distance from mid in bps
    w_dist: float = 0.0      # distance decay weight
    S: float = 0.0           # WallStrength = r × w_dist
    
    # Time series metrics
    persistence: float = 0.0  # P: presence ratio
    churn: float = 0.0        # C: volume volatility
    erosion: float = 0.0      # E: erosion rate (on r, not q)
    drop: float = 0.0         # sharp drop ratio
    eat_rate: float = 0.0     # v2: aggressor eating the wall
    
    # State
    state: WallState = WallState.UNKNOWN
    first_seen_ms: int = 0
    stability_ticks: int = 0


@dataclass
class WallTimeSeries:
    """Time series data for a tracked wall (v2: bin-based)."""
    bin_id: int
    side: str
    # v2: Track both q and r (normalized)
    samples: deque = field(default_factory=lambda: deque(maxlen=100))
    ema_r: float = 0.0  # v2: EMA of r, not q
    ema_alpha: float = 0.3
    
    # Running D_med for normalization
    last_d_med: float = 1.0
    
    def add_sample(self, q: float, r: float, d_med: float, ts_ms: int) -> None:
        """Add volume sample with normalized r."""
        # v2: Store (timestamp, q, r)
        self.samples.append((ts_ms, q, r))
        self.last_d_med = d_med
        
        # Update EMA on r (normalized), not q
        if self.ema_r == 0:
            self.ema_r = r
        else:
            self.ema_r = self.ema_alpha * r + (1 - self.ema_alpha) * self.ema_r
    
    def get_samples_in_window(self, now_ms: int, window_ms: int) -> List[Tuple[int, float, float]]:
        """Get samples within time window. Returns (ts, q, r)."""
        cutoff = now_ms - window_ms
        return [(ts, q, r) for ts, q, r in self.samples if ts >= cutoff]


@dataclass
class WallAnalyzerConfig:
    """Configuration for wall analyzer."""
    # Detection thresholds
    r_min: float = 3.0           # min ratio to be wall candidate
    z_min: float = 4.0           # alternative z-score threshold
    lambda_bps: float = 10.0     # distance decay parameter
    max_distance_bps: float = 50.0  # max distance to consider
    
    # v2: Bin configuration
    bin_size_ticks: int = 5      # Number of price ticks per bin (fallback)
    tick_size: float = 0.01      # Price tick size (0.01 for ETH)
    bin_width_bps: float = 1.0   # v2.1: Target bin width in bps (adaptive)
    
    # Time windows (ms)
    T_persist_ms: int = 5000     # persistence window
    T_erosion_ms: int = 4000     # erosion calculation window
    T_break_ms: int = 2000       # v2.1: broken wall confirmation window
    
    # State thresholds
    P_stable: float = 0.7        # min persistence for stable
    P_fake: float = 0.6          # below this = fake
    C_max: float = 0.8           # max churn for stable
    E_min: float = 0.25          # min erosion rate
    drop_min: float = 0.25       # sharp drop threshold
    
    # v2.1: BROKEN state params
    r_exist_min: float = 1.5     # "still a wall?" threshold (lower than r_min)
    P_broken: float = 0.2        # P_exist below this = BROKEN
    
    # v2.1: Eat rate for ERODING/FAKE distinction
    eat_rate_min: float = 0.1    # min eat rate to confirm ERODING
    eat_rate_low: float = 0.05   # below this + high churn = FAKE
    
    # EMA
    ema_alpha: float = 0.3


class WallAnalyzer:
    """
    Industrial-grade wall analyzer v2.
    
    Key v2 improvements:
    - Bin-based wall tracking (no identity drift)
    - Erosion on normalized r = q/D_med
    - Support for eat_rate filtering
    
    Usage:
        analyzer = WallAnalyzer(config)
        bid_wall, ask_wall = analyzer.analyze(bids, asks, mid, now_ms)
    """
    
    def __init__(self, config: Optional[WallAnalyzerConfig] = None):
        self.config = config or WallAnalyzerConfig()
        
        # v2: Tracked walls: (side, bin_id) -> WallTimeSeries
        self._wall_series: Dict[Tuple[str, int], WallTimeSeries] = {}
        
        # Current candidates
        self._bid_candidates: List[WallCandidate] = []
        self._ask_candidates: List[WallCandidate] = []
        
        # Current D_med for each side (for normalized erosion)
        self._bid_d_med: float = 1.0
        self._ask_d_med: float = 1.0
        
        # Strongest walls
        self._best_bid_wall: Optional[WallCandidate] = None
        self._best_ask_wall: Optional[WallCandidate] = None
        
        # Stats
        self._last_update_ms: int = 0
        self._tick_count: int = 0
    
    def _price_to_bin(self, price: float) -> int:
        """
        Convert price to bin ID (v2.1: Adaptive BPS-based).
        
        Calculates bin size based on bin_width_bps (e.g., 1.0 bps).
        Prevents jitter across different price scales.
        """
        cfg = self.config
        
        # Adaptive: bin_width = price * bps / 10000
        # If bin_width_bps is set, use it. Otherwise fallback to fixed ticks.
        if cfg.bin_width_bps > 0:
            target_width = price * cfg.bin_width_bps / 10000
            # Round to nearest multiple of tick_size
            ticks = max(1, round(target_width / cfg.tick_size))
            bin_size = ticks * cfg.tick_size
        else:
            bin_size = cfg.bin_size_ticks * cfg.tick_size
            
        return round(price / bin_size)
    
    def analyze(
        self,
        bids: List[Tuple[float, float]],
        asks: List[Tuple[float, float]],
        mid: float,
        now_ms: Optional[int] = None,
    ) -> Tuple[Optional[WallCandidate], Optional[WallCandidate]]:
        """
        Analyze orderbook for walls.
        
        Returns: (best_bid_wall, best_ask_wall)
        """
        now_ms = now_ms or int(time.time() * 1000)
        self._last_update_ms = now_ms
        self._tick_count += 1
        
        if not bids or not asks or mid <= 0:
            return None, None
        
        # Get robust stats and save for normalized erosion
        self._bid_d_med, bid_iqr = self._robust_stats([s for _, s in bids])
        self._ask_d_med, ask_iqr = self._robust_stats([s for _, s in asks])
        
        # Find candidates with bin IDs
        self._bid_candidates = self._find_candidates(
            bids, mid, self._bid_d_med, bid_iqr, "bid", now_ms
        )
        self._ask_candidates = self._find_candidates(
            asks, mid, self._ask_d_med, ask_iqr, "ask", now_ms
        )
        
        # Update time series and compute metrics
        for c in self._bid_candidates:
            self._update_wall_metrics(c, self._bid_d_med, now_ms)
        for c in self._ask_candidates:
            self._update_wall_metrics(c, self._ask_d_med, now_ms)
        
        # Get strongest
        self._best_bid_wall = self._get_strongest(self._bid_candidates)
        self._best_ask_wall = self._get_strongest(self._ask_candidates)
        
        # Cleanup old series
        self._cleanup_old_series(now_ms)
        
        return self._best_bid_wall, self._best_ask_wall
    
    def _robust_stats(self, volumes: List[float]) -> Tuple[float, float]:
        """Calculate robust statistics (median, IQR)."""
        if not volumes:
            return 1.0, 0.1
        
        sorted_v = sorted(volumes)
        n = len(sorted_v)
        
        # Median
        if n % 2 == 0:
            median = (sorted_v[n//2 - 1] + sorted_v[n//2]) / 2
        else:
            median = sorted_v[n//2]
        
        # IQR
        q1_idx = n // 4
        q3_idx = 3 * n // 4
        q1 = sorted_v[q1_idx] if q1_idx < n else sorted_v[0]
        q3 = sorted_v[q3_idx] if q3_idx < n else sorted_v[-1]
        iqr = max(q3 - q1, 0.01)
        
        return max(median, 0.01), iqr
    
    def _find_candidates(
        self,
        levels: List[Tuple[float, float]],
        mid: float,
        d_med: float,
        d_iqr: float,
        side: str,
        now_ms: int,
    ) -> List[WallCandidate]:
        """Find wall candidates with WallStrength scores and bin IDs."""
        cfg = self.config
        candidates = []
        
        for price, size in levels:
            if size <= 0:
                continue
            
            # Distance in bps
            d_bps = abs(price - mid) / mid * 10000
            if d_bps > cfg.max_distance_bps:
                continue
            
            # Ratio and z-score
            r = size / max(d_med, 0.01)
            z = (size - d_med) / max(d_iqr, 0.01)
            
            # Check threshold
            if r < cfg.r_min and z < cfg.z_min:
                continue
            
            # Distance decay weight
            w_dist = math.exp(-d_bps / cfg.lambda_bps)
            
            # WallStrength
            S = r * w_dist
            
            # v2: Assign bin ID
            bin_id = self._price_to_bin(price)
            
            # Create candidate
            c = WallCandidate(
                price=price,
                size=size,
                side=side,
                bin_id=bin_id,
                r=r,
                z=z,
                d_bps=d_bps,
                w_dist=w_dist,
                S=S,
                first_seen_ms=now_ms,
            )
            candidates.append(c)
        
        # Sort by S descending
        candidates.sort(key=lambda x: x.S, reverse=True)
        
        # Return top 3
        return candidates[:3]
    
    def _update_wall_metrics(self, c: WallCandidate, d_med: float, now_ms: int) -> None:
        """Update time series metrics for a wall candidate (v2: bin-based)."""
        cfg = self.config
        
        # v2: Key by bin_id, not exact price
        key = (c.side, c.bin_id)
        
        # Get or create series
        if key not in self._wall_series:
            self._wall_series[key] = WallTimeSeries(
                bin_id=c.bin_id,
                side=c.side,
                ema_alpha=cfg.ema_alpha,
            )
        
        series = self._wall_series[key]
        
        # v2: Add sample with normalized r
        series.add_sample(c.size, c.r, d_med, now_ms)
        
        # Get samples for windows
        persist_samples = series.get_samples_in_window(now_ms, cfg.T_persist_ms)
        erosion_samples = series.get_samples_in_window(now_ms, cfg.T_erosion_ms)
        
        if len(persist_samples) < 3:
            c.state = WallState.UNKNOWN
            return
        
        # 1) Persistence: ratio of samples where r >= r_min
        c.persistence = self._calculate_persistence(persist_samples, cfg.r_min)
        
        # 2) Churn: Σ|dr| / Σr (v2: use r, not q)
        c.churn = self._calculate_churn_normalized(persist_samples)
        
        # 3) Erosion: EWM slope on r (v2: normalized)
        c.erosion, c.drop = self._calculate_erosion_normalized(erosion_samples, series.ema_r)
        
        # 4) Determine state (v2.1: pass series for BROKEN check)
        c.state = self._classify_state(c, series, now_ms)
    
    def _calculate_persistence(
        self, samples: List[Tuple[int, float, float]], r_min: float
    ) -> float:
        """Calculate presence ratio using actual r values (v2)."""
        if not samples:
            return 0.0
        
        # v2: Check actual r >= r_min
        present = sum(1 for _, _, r in samples if r >= r_min)
        return present / len(samples)
    
    def _calculate_churn_normalized(self, samples: List[Tuple[int, float, float]]) -> float:
        """Calculate volume volatility on normalized r (v2)."""
        if len(samples) < 2:
            return 0.0
        
        # v2: Use r (normalized) instead of raw q
        r_values = [r for _, _, r in samples]
        total_change = sum(abs(r_values[i] - r_values[i-1]) for i in range(1, len(r_values)))
        total_r = sum(r_values) + 0.01
        
        return total_change / total_r
    
    def _calculate_erosion_normalized(
        self, samples: List[Tuple[int, float, float]], current_ema_r: float
    ) -> Tuple[float, float]:
        """
        Calculate erosion rate on normalized r (v2).
        
        This prevents "whole book thinned" false positives.
        """
        if len(samples) < 3:
            return 0.0, 0.0
        
        # Linear regression slope on r (not q)
        n = len(samples)
        t0 = samples[0][0]
        
        # Normalize time to seconds from start
        times = [(ts - t0) / 1000.0 for ts, _, _ in samples]
        r_values = [r for _, _, r in samples]
        
        t_mean = sum(times) / n
        r_mean = sum(r_values) / n
        
        numerator = sum((t - t_mean) * (r - r_mean) for t, r in zip(times, r_values))
        denominator = sum((t - t_mean) ** 2 for t in times) + 0.01
        
        slope = numerator / denominator  # r per second
        
        # Erosion rate: negative slope normalized by mean r
        erosion = -slope / max(r_mean, 0.01)
        
        # Sharp drop: compare last two r values
        if len(r_values) >= 2:
            prev_r = r_values[-2]
            curr_r = r_values[-1]
            drop = (curr_r - prev_r) / max(prev_r, 0.01)
        else:
            drop = 0.0
        
        return max(0, erosion), drop
    
    def _classify_state(
        self, c: WallCandidate, series: Optional[WallTimeSeries], now_ms: int
    ) -> WallState:
        """
        Classify wall state based on metrics (v2.1).
        
        Priority: BROKEN > FAKE > ERODING > STABLE > UNKNOWN
        """
        cfg = self.config
        
        # v2.1: BROKEN check - wall existence collapsed
        if series is not None:
            break_samples = series.get_samples_in_window(now_ms, cfg.T_break_ms)
            if len(break_samples) >= 3:
                # P_exist: ratio of samples where r >= r_exist_min
                P_exist = sum(1 for _, _, r in break_samples if r >= cfg.r_exist_min) / len(break_samples)
                if P_exist < cfg.P_broken:
                    return WallState.BROKEN
        
        # FAKE: low persistence OR (high churn + low eat rate)
        if c.persistence < cfg.P_fake:
            return WallState.FAKE
        if c.churn > cfg.C_max and c.eat_rate < cfg.eat_rate_low:
            return WallState.FAKE
        
        # ERODING: (high erosion OR sharp drop) AND eat_rate confirms real eating
        # If eat_rate available and high, more confident it's real erosion
        erosion_signal = c.erosion >= cfg.E_min or c.drop <= -cfg.drop_min
        if erosion_signal:
            # If eat_rate is available and high, definitely ERODING
            # If eat_rate unavailable (0), still use erosion signal
            if c.eat_rate >= cfg.eat_rate_min or c.eat_rate == 0:
                return WallState.ERODING
        
        # STABLE: high persistence, low erosion, low churn
        if c.persistence >= cfg.P_stable and c.erosion < cfg.E_min and c.churn < cfg.C_max:
            return WallState.STABLE
        
        return WallState.UNKNOWN
    
    def _get_strongest(self, candidates: List[WallCandidate]) -> Optional[WallCandidate]:
        """Get strongest wall that's not FAKE."""
        for c in candidates:
            if c.state != WallState.FAKE:
                return c
        return candidates[0] if candidates else None
    
    def _cleanup_old_series(self, now_ms: int, max_age_ms: int = 30000) -> None:
        """Remove stale wall series."""
        cutoff = now_ms - max_age_ms
        keys_to_remove = []
        
        for key, series in self._wall_series.items():
            if not series.samples or series.samples[-1][0] < cutoff:
                keys_to_remove.append(key)
        
        for key in keys_to_remove:
            del self._wall_series[key]
    
    # === v2: Eat rate support ===
    
    def update_eat_rate(self, side: str, aggressor_volume: float) -> None:
        """
        Update eat rate for a wall based on aggressor flow.
        
        Call this when Binance aggTrade comes in targeting this wall's side.
        """
        wall = self._best_bid_wall if side == "bid" else self._best_ask_wall
        if wall and wall.size > 0:
            wall.eat_rate = aggressor_volume / wall.size
    
    # === Public accessors ===
    
    def get_strongest_wall(self, side: str) -> Optional[WallCandidate]:
        """Get strongest wall for side."""
        return self._best_bid_wall if side == "bid" else self._best_ask_wall
    
    def get_wall_state(self, side: str) -> WallState:
        """Get state of strongest wall."""
        wall = self.get_strongest_wall(side)
        return wall.state if wall else WallState.UNKNOWN
    
    @property
    def tick_count(self) -> int:
        return self._tick_count
