from typing import Optional
from hyperbot.vacuum.strategies.base import BaseStrategy, StrategyContext, StrategySignal

class LIRDivergenceStrategy(BaseStrategy):
    """
    LIR Divergence Strategy (Reversal).
    
    Logic:
    - Price deviates significantly.
    - LIR disagrees with price move (Divergence).
    - Example: Price High (Dev > Thresh) but LIR Bearish (< 0.6).
    """
    
    def __init__(self, config):
        super().__init__(config)
        self.name = "lir_divergence"

    def check(self, ctx: StrategyContext) -> Optional[StrategySignal]:
        if not self.enabled or not ctx.has_binance:
            return None
            
        bs = ctx.binance_state
        hs = ctx.hyper_state
        cfg = self.config
        
        lir_extreme = cfg.get("lir_extreme", 1.5) # e.g. 1.5 (bull), 0.5 (bear) - Wait, logic below
        # Let's say lir_extreme acts as deviation from neutral 1.0
        # If config says 1.5, it means Bull > 1.5, Bear < 0.66
        
        dev_thresh = cfg.get("max_deviation_bps", 1.0)
        
        hm = hs.book.top_of_book.mid
        dev = bs.get_deviation_bps(hm)
        # Fix: Use context LIR signal
        if not ctx.lir:
            return None
        lir = ctx.lir.lir_smooth
        
        # Divergence SELL: Price High (Dev > 0) but LIR Bearish
        if dev > dev_thresh:
            if lir < 0.7: # Bearish LIR
                return StrategySignal(
                    side="sell",
                    strategy_name=self.name,
                    confidence=0.8,
                    reason=f"LIR Div Sell: dev={dev:.1f}bps, LIR={lir:.2f} < 0.7",
                    metadata={"lir": lir, "dev": dev}
                )

        # Divergence BUY: Price Low (Dev < 0) but LIR Bullish
        if dev < -dev_thresh:
            if lir > 1.3: # Bullish LIR
                return StrategySignal(
                    side="buy",
                    strategy_name=self.name,
                    confidence=0.8,
                    reason=f"LIR Div Buy: dev={dev:.1f}bps, LIR={lir:.2f} > 1.3",
                    metadata={"lir": lir, "dev": dev}
                )
                        
        return None
