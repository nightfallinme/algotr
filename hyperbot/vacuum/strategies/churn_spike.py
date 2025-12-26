from typing import Optional
from hyperbot.vacuum.strategies.base import BaseStrategy, StrategyContext, StrategySignal
from hyperbot.vacuum.wall_analyzer import WallState

class ChurnSpikeStrategy(BaseStrategy):
    """
    Churn Spike Strategy (Fake Wall Exploitation).
    
    Logic:
    - Wall exists but has high CHURN (orders constantly replaced/cancelled).
    - Aggressor flow is attacking it.
    - Expect wall to be fake/spoofed and price to break through.
    """
    
    def __init__(self, config):
        super().__init__(config)
        self.name = "churn_spike"

    def check(self, ctx: StrategyContext) -> Optional[StrategySignal]:
        if not self.enabled or not ctx.has_binance:
            return None
            
        bs = ctx.binance_state
        cfg = self.config
        
        churn_mult = cfg.get("churn_mult", 2.0) # Relative to avg churn? Or absolute value?
        # WallAnalyzer normalizes churn. 1.0 = high?
        # Let's assume absolute threshold for normalized churn
        churn_thresh = 0.5 * churn_mult 
        
        # Check Bid Wall (Break Down)
        wall = bs.bid_wall
        if wall:
            # If churn is high and Aggressor Selling
            if wall.churn > churn_thresh and bs.aggressor_bias < -0.5:
                return StrategySignal(
                    side="sell",
                    strategy_name=self.name,
                    confidence=0.85,
                    reason=f"Churn Spike Bid: churn={wall.churn:.2f} > {churn_thresh}, agg={bs.aggressor_bias:.1f}",
                    metadata={"wall_price": wall.price, "churn": wall.churn}
                )

        # Check Ask Wall (Break Up)
        wall = bs.ask_wall
        if wall:
            if wall.churn > churn_thresh and bs.aggressor_bias > 0.5:
                return StrategySignal(
                    side="buy",
                    strategy_name=self.name,
                    confidence=0.85,
                    reason=f"Churn Spike Ask: churn={wall.churn:.2f} > {churn_thresh}, agg={bs.aggressor_bias:.1f}",
                    metadata={"wall_price": wall.price, "churn": wall.churn}
                )
                        
        return None
