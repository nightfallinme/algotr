from typing import Optional
from hyperbot.vacuum.strategies.base import BaseStrategy, StrategyContext, StrategySignal

class VacuumFillStrategy(BaseStrategy):
    """
    Vacuum Fill Strategy.
    
    Logic:
    - Extremely thin liquidity (Vacuum) detected
    - LIR isn't opposing the move
    - Concept: Price fills vacuum rapidly
    """
    
    def __init__(self, config):
        super().__init__(config)
        self.name = "vacuum_fill"

    def check(self, ctx: StrategyContext) -> Optional[StrategySignal]:
        if not self.enabled or not ctx.has_binance:
            return None
            
        bs = ctx.binance_state
        hs = ctx.hyper_state
        cfg = self.config
        
        vac_thresh = cfg.get("vacuum_threshold", 0.15)
        # LIR Neutral Range [0.9, 1.1]
        lir_range = cfg.get("lir_neutral_range", [0.9, 1.1])
        
        # Fix: Use context LIR signal
        if not ctx.lir:
            return None
        lir = ctx.lir.lir_smooth
        
        # Check UP Vacuum
        if bs.vacuum_up < vac_thresh:
            # LIR should not be bearish (< 0.9)
            if lir > lir_range[0]:
                return StrategySignal(
                    side="buy", 
                    strategy_name=self.name,
                    confidence=0.9,
                    reason=f"Vacuum Fill Up: vac={bs.vacuum_up:.2f} < {vac_thresh}",
                    metadata={"vacuum": bs.vacuum_up}
                )

        # Check DOWN Vacuum
        if bs.vacuum_down < vac_thresh:
            # LIR should not be bullish (> 1.1)
            if lir < lir_range[1]:
                return StrategySignal(
                    side="sell", 
                    strategy_name=self.name,
                    confidence=0.9,
                    reason=f"Vacuum Fill Down: vac={bs.vacuum_down:.2f} < {vac_thresh}",
                    metadata={"vacuum": bs.vacuum_down}
                )
                        
        return None
