from typing import Optional
from hyperbot.vacuum.strategies.base import BaseStrategy, StrategyContext, StrategySignal
from hyperbot.vacuum.wall_analyzer import WallState

class FreshWallStrategy(BaseStrategy):
    """
    Fresh Wall Strategy.
    
    Logic:
    - New wall detected (State: NEW or GROWING).
    - Wall size is huge (r >= min_strength).
    - Persistence is low (it's fresh).
    - Signal: Fade entry (Buy at Bid, Sell at Ask) expecting the new wall to hold.
    """
    
    def __init__(self, config):
        super().__init__(config)
        self.name = "fresh_wall"

    def check(self, ctx: StrategyContext) -> Optional[StrategySignal]:
        if not self.enabled or not ctx.has_binance:
            return None
            
        bs = ctx.binance_state
        cfg = self.config
        
        min_strength = cfg.get("min_strength", 3.0) 
        max_persistence = cfg.get("max_persistence", 0.3)
        
        # Check Bid Wall (Fresh Support -> Buy)
        # Check Bid Wall (Fresh Support -> Buy)
        wall = bs.bid_wall
        if wall:
            # Fix: WallState enum doesn't have NEW/GROWING. Use timestamp.
            age_sec = (ctx.now_ms - wall.first_seen_ms) / 1000.0
            is_fresh = age_sec < 10.0 # Consider fresh if < 10s old
            
            if is_fresh and wall.r >= min_strength and wall.persistence <= max_persistence:
                return StrategySignal(
                    side="buy",
                    strategy_name=self.name,
                    confidence=0.9,
                    reason=f"Fresh Wall Bid: r={wall.r:.1f} > {min_strength}, age={age_sec:.1f}s",
                    metadata={"wall_price": wall.price, "age": age_sec}
                )

        # Check Ask Wall (Fresh Resistance -> Sell)
        wall = bs.ask_wall
        if wall:
            # Fix: Use timestamp
            age_sec = (ctx.now_ms - wall.first_seen_ms) / 1000.0
            is_fresh = age_sec < 10.0
            
            if is_fresh and wall.r >= min_strength and wall.persistence <= max_persistence:
                return StrategySignal(
                    side="sell",
                    strategy_name=self.name,
                    confidence=0.9,
                    reason=f"Fresh Wall Ask: r={wall.r:.1f} > {min_strength}, age={age_sec:.1f}s",
                    metadata={"wall_price": wall.price, "age": age_sec}
                )
        
        return None
