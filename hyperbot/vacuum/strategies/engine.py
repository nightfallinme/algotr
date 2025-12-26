from typing import List, Optional, Any
from hyperbot.vacuum.strategies.base import BaseStrategy, StrategyContext, StrategySignal

# Import strategies
from hyperbot.vacuum.strategies.fade import FadeStrategy
from hyperbot.vacuum.strategies.break_strat import BreakStrategy
from hyperbot.vacuum.strategies.absorption import AbsorptionStrategy
from hyperbot.vacuum.strategies.vacuum_fill import VacuumFillStrategy
from hyperbot.vacuum.strategies.churn_spike import ChurnSpikeStrategy
from hyperbot.vacuum.strategies.lir_divergence import LIRDivergenceStrategy
from hyperbot.vacuum.strategies.fresh_wall import FreshWallStrategy
from hyperbot.vacuum.strategies.aggressor_momentum import AggressorMomentumStrategy

class StrategyEngine:
    """
    Orchestrates all enabled strategies.
    Runs strategies in priority order.
    """
    
    def __init__(self, config: Any):
        self.config = config # VacuumConfig
        self.strategies: List[BaseStrategy] = []
        
        # Config structure: config.strategies provided as dict/Box
        strat_config = config.strategies
        
        # Priority 1: Core Strategies (Fade / Break)
        fade_cfg = strat_config.get("fade", {"enabled": True})
        if fade_cfg.get("enabled"):
            self.strategies.append(FadeStrategy(fade_cfg))
            
        break_cfg = strat_config.get("break_strat", {"enabled": True})
        if break_cfg.get("enabled"):
            self.strategies.append(BreakStrategy(break_cfg))
            
        # Priority 2: Advanced/Specialized
        absorption_cfg = strat_config.get("absorption", {"enabled": False})
        if absorption_cfg.get("enabled"):
            self.strategies.append(AbsorptionStrategy(absorption_cfg))
            
        vacuum_fill_cfg = strat_config.get("vacuum_fill", {"enabled": False})
        if vacuum_fill_cfg.get("enabled"):
            self.strategies.append(VacuumFillStrategy(vacuum_fill_cfg))
            
        churn_spike_cfg = strat_config.get("churn_spike", {"enabled": False})
        if churn_spike_cfg.get("enabled"):
            self.strategies.append(ChurnSpikeStrategy(churn_spike_cfg))
            
        lir_div_cfg = strat_config.get("lir_divergence", {"enabled": False})
        if lir_div_cfg.get("enabled"):
            self.strategies.append(LIRDivergenceStrategy(lir_div_cfg))
            
        fresh_wall_cfg = strat_config.get("fresh_wall", {"enabled": False})
        if fresh_wall_cfg.get("enabled"):
            self.strategies.append(FreshWallStrategy(fresh_wall_cfg))
            
        agg_mom_cfg = strat_config.get("aggressor_momentum", {"enabled": False})
        if agg_mom_cfg.get("enabled"):
            self.strategies.append(AggressorMomentumStrategy(agg_mom_cfg))
            
        print(f"[DEBUG] StrategyEngine initialized with {len(self.strategies)} strategies: {[s.name for s in self.strategies]}")
            
    def check_all(self, ctx: StrategyContext) -> Optional[StrategySignal]:
        """Run all enabled strategies and return the first valid signal."""
        if not self.strategies:
            print("[DEBUG] check_all: No strategies loaded")
            return None
            
        return self._check_strategies(ctx)

    def _check_strategies(self, ctx: StrategyContext) -> Optional[StrategySignal]:
        for strat in self.strategies:
            # Each strategy returns Signal or None
            signal = strat.check(ctx)
            if signal:
                return signal
                
        return None
