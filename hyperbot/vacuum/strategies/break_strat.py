from typing import Optional
from hyperbot.vacuum.strategies.base import BaseStrategy, StrategyContext, StrategySignal
from hyperbot.vacuum.regime_manager import Regime

class BreakStrategy(BaseStrategy):
    """
    BREAK (Momentum) Strategy.
    
    Logic:
    - Regime: MOMENTUM_BREAK
    - Vacuum: Thin liquidity in break direction
    - LIR: Confirms direction (Bullish for UP, Bearish for DOWN)
    - Direction: Determined deterministically by RegimeManager (Fix 5)
    """
    
    def __init__(self, config):
        super().__init__(config)
        self.name = "break_strat"

    def check(self, ctx: StrategyContext) -> Optional[StrategySignal]:
        if not self.enabled or not ctx.has_binance:
            return None
            
        bs = ctx.binance_state
        hs = ctx.hyper_state
        cfg = self.config
        
        # 1. Regime Check
        if bs.regime != Regime.MOMENTUM_BREAK:
            return None
            
        # 2. Thresholds
        vacuum_thresh = cfg.get("vacuum_threshold", 0.25)
        lir_bull = cfg.get("lir_bull_threshold", 1.2)
        lir_bear = cfg.get("lir_bear_threshold", 0.8)
        
        # 3. LIR check
        # 3. LIR check
        if not ctx.lir: return None
        lir = ctx.lir.lir_smooth
        
        # 4. Determine Direction (Fix 5: Deterministic)
        direction = bs.break_direction # "up" or "down"
        
        if direction == "up":
            # Breaking UP (Ask wall eroding)
            if bs.vacuum_up < vacuum_thresh:
                # Require LIR bullish
                if lir > lir_bull:
                    return StrategySignal(
                        side="buy", # Break Up -> Buy
                        strategy_name=self.name,
                        confidence=1.0,
                        reason=f"Break Up: vac_up={bs.vacuum_up:.2f} < {vacuum_thresh}, LIR={lir:.2f} > {lir_bull}",
                        metadata={"vacuum": bs.vacuum_up, "lir": lir}
                    )
                    
        elif direction == "down":
            # Breaking DOWN (Bid wall eroding)
            if bs.vacuum_down < vacuum_thresh:
                if lir < lir_bear:
                    return StrategySignal(
                        side="sell", # Break Down -> Sell
                        strategy_name=self.name,
                        confidence=1.0,
                        reason=f"Break Down: vac_down={bs.vacuum_down:.2f} < {vacuum_thresh}, LIR={lir:.2f} < {lir_bear}",
                        metadata={"vacuum": bs.vacuum_down, "lir": lir}
                    )
                    
        return None
