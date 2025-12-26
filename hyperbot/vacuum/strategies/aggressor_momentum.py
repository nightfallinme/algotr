from typing import Optional
from hyperbot.vacuum.strategies.base import BaseStrategy, StrategyContext, StrategySignal

class AggressorMomentumStrategy(BaseStrategy):
    """
    Aggressor Momentum Strategy.
    
    Logic:
    - Aggressor bias is extremely strong (one-sided flow).
    - LIR confirms the direction.
    - Signal: Follow the flow (Taker entry / Break-like).
    """
    
    def __init__(self, config):
        super().__init__(config)
        self.name = "aggressor_momentum"

    def check(self, ctx: StrategyContext) -> Optional[StrategySignal]:
        if not self.enabled or not ctx.has_binance:
            return None
            
        bs = ctx.binance_state
        hs = ctx.hyper_state
        cfg = self.config
        
        bias_thresh = cfg.get("bias_threshold", 1.5)
        require_lir = cfg.get("require_lir_confirm", True)
        
        bias = bs.aggressor_bias
        
        # Check Bullish Flow
        if bias > bias_thresh:
            if require_lir:
                # Fix: Use context LIR signal, not calculator instance
                if not ctx.lir or ctx.lir.lir_smooth < 1.0: # LIR contradicts
                    return None
            
            return StrategySignal(
                side="buy",
                strategy_name=self.name,
                confidence=0.85,
                reason=f"Aggressor Mom Buy: bias={bias:.1f} > {bias_thresh}",
                metadata={"bias": bias}
            )

        # Check Bearish Flow
        if bias < -bias_thresh:
            if require_lir:
                # Fix: Use context LIR signal
                if not ctx.lir or ctx.lir.lir_smooth > 1.0: # LIR contradicts
                    return None

            return StrategySignal(
                side="sell",
                strategy_name=self.name,
                confidence=0.85,
                reason=f"Aggressor Mom Sell: bias={bias:.1f} < -{bias_thresh}",
                metadata={"bias": bias}
            )
        
        return None
