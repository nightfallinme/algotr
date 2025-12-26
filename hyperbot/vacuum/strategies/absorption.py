from typing import Optional
from hyperbot.vacuum.strategies.base import BaseStrategy, StrategyContext, StrategySignal
from hyperbot.vacuum.wall_analyzer import WallState

class AbsorptionStrategy(BaseStrategy):
    """
    Wall Absorption Strategy (Bounce Confirmation).
    
    Logic:
    - Wall is STABLE
    - Price touches wall (deviation ~ 0)
    - Aggressor flow is attacking the wall (against it)
    - Wall is NOT eroding (absorbing the flow)
    - Signal: Counter-trend bounce entry
    """
    
    def __init__(self, config):
        super().__init__(config)
        self.name = "absorption"

    def check(self, ctx: StrategyContext) -> Optional[StrategySignal]:
        if not self.enabled or not ctx.has_binance:
            return None
            
        bs = ctx.binance_state
        hs = ctx.hyper_state
        cfg = self.config
        
        touch_zone = cfg.get("touch_zone_bps", 0.5)
        
        # Hyper mid
        hm = hs.book.top_of_book.mid
        if hm <= 0: return None
        
        # CHECK BID ABSORPTION (Long)
        wall = bs.bid_wall
        if wall and wall.state == WallState.STABLE:
            # 1. Check Touch
            # Deviation: (Hyper - Wall) / Wall * 10000
            # For bid, Hyper > Wall. Touch if Hyper is close to Wall.
            # Dev should be small positive (e.g. 0 to 2 bps)
            dist_bps = (hm - wall.price) / wall.price * 10000
            
            if 0 <= dist_bps <= touch_zone:
                # 2. Check Aggressor (Must be SELLING into bid)
                # Bias should be negative (net selling)
                if bs.aggressor_bias < -0.5:
                    # 3. Check Erosion (Must be absorbing, low erosion)
                    if wall.erosion < 0.1:
                        return StrategySignal(
                            side="buy",
                            strategy_name=self.name,
                            confidence=0.8,
                            reason=f"Absorption Bid: dist={dist_bps:.1f}bps, agg={bs.aggressor_bias:.1f}, ero={wall.erosion:.2f}",
                            metadata={"wall_price": wall.price}
                        )

        # CHECK ASK ABSORPTION (Short)
        wall = bs.ask_wall
        if wall and wall.state == WallState.STABLE:
            # 1. Check Touch
            # For ask, Hyper < Wall. Dist is negative.
            # Use abs distance
            dist_bps = (wall.price - hm) / wall.price * 10000
            
            if 0 <= dist_bps <= touch_zone:
                # 2. Check Aggressor (Must be BUYING into ask)
                # Bias should be positive
                if bs.aggressor_bias > 0.5:
                    # 3. Check Erosion
                    if wall.erosion < 0.1:
                        return StrategySignal(
                            side="sell",
                            strategy_name=self.name,
                            confidence=0.8,
                            reason=f"Absorption Ask: dist={dist_bps:.1f}bps, agg={bs.aggressor_bias:.1f}, ero={wall.erosion:.2f}",
                            metadata={"wall_price": wall.price}
                        )
                        
        return None
