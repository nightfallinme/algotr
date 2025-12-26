from typing import Optional
import logging
from hyperbot.vacuum.strategies.base import BaseStrategy, StrategyContext, StrategySignal
from hyperbot.vacuum.wall_analyzer import WallState
from hyperbot.vacuum.regime_manager import Regime

log = logging.getLogger(__name__)

class FadeStrategy(BaseStrategy):
    """
    FADE (Mean Reversion) Strategy.
    
    Logic:
    - Regime: RANGE_MAKER
    - Deviation: Price deviates significantly from Binance Mid
    - Constraint: Stable wall exists on the side we rely on (support for long, resistance for short)
    - Veto: Aggressor flow must not be strongly against us
    """
    
    def __init__(self, config: dict):
        super().__init__(config)
        self.name = "fade"
        # Fix: Default lower for testing if config missing
        self.base_deviation = config.get("deviation_bps", 0.5)
        self.dynamic_spread_mult = config.get("dynamic_spread_mult", 1.0)
        self.use_binance_mid = config.get("use_binance_mid", True)
        
    def check(self, ctx: StrategyContext) -> Optional[StrategySignal]:
        if not self.enabled or not ctx.has_binance:
            return None
            
        bs = ctx.binance_state
        hs = ctx.hyper_state
        
        # 1. Regime Check
        if bs.regime != Regime.RANGE_MAKER:
            return None
            
        # 2. Threshold Calculation
        # Use instance variables initialized with correct defaults
        mult = self.dynamic_spread_mult
        base_thresh = self.base_deviation
        
        # Use simple ToB spread for dynamic calculation
        tob_spread = bs.local_book.spread_bps
        threshold = max(base_thresh, tob_spread * mult)
        
        # 3. Calculate Deviation (Fix 6: From Binance Mid)
        # dev > 0 means Hyper > Binance (Short opportunity if Ask Wall exists)
        # dev < 0 means Hyper < Binance (Long opportunity if Bid Wall exists)
        hyper_mid = hs.book.top_of_book.mid
        dev = bs.get_deviation_bps(hyper_mid)
        
        # 4. Check Signals
        
        # DEBUG TRACE
        # DEBUG TRACE
        # if abs(dev) > 0.3:
        #      log.info(f"[FADE TRACE] Dev={dev:.2f}bps Thresh={threshold:.2f} Regime={bs.regime.name}")
        
        # FADE SHORT: Hyper Price High -> Sell -> Need Stable Ask Wall (Resistance)
        
        # FADE SHORT: Hyper Price High -> Sell -> Need Stable Ask Wall (Resistance)
        if dev >= threshold:
            # Need STABLE ask wall on Binance to reject price
            if bs.ask_wall and bs.ask_wall.state == WallState.STABLE:
                # Check Aggressor Veto (Fix 3)
                if not bs.aggressor_vetoes_fade("short"):
                    return StrategySignal(
                        side="sell",
                        strategy_name=self.name,
                        confidence=1.0,
                        reason=f"Fade Short: dev={dev:.1f}bps > {threshold:.1f}, ask_wall=STABLE",
                        metadata={"wall_price": bs.ask_wall.price, "dev": dev}
                    )
                else:
                    bs.log_veto_reason("short", dev, threshold)
            else:
                # Log why wall failed
                if bs.ask_wall:
                    # log.info(f"[DEBUG] Fade Short REJECT: Dev={dev:.2f}bps. Wall State={bs.ask_wall.state.name} (Need STABLE). WallPrice={bs.ask_wall.price}")
                    pass
                else:
                    # log.info(f"[DEBUG] Fade Short REJECT: Dev={dev:.2f}bps. No Ask Wall.")
                    pass
        
        # FADE LONG: Hyper Price Low -> Buy -> Need Stable Bid Wall (Support)
        if dev <= -threshold:
            # Need STABLE bid wall on Binance to bounce price
            if bs.bid_wall and bs.bid_wall.state == WallState.STABLE:
                # Check Aggressor Veto (Fix 3)
                if not bs.aggressor_vetoes_fade("long"):
                    return StrategySignal(
                        side="buy",
                        strategy_name=self.name,
                        confidence=1.0,
                        reason=f"Fade Long: dev={dev:.1f}bps < -{threshold:.1f}, bid_wall=STABLE",
                        metadata={"wall_price": bs.bid_wall.price, "dev": dev}
                    )
                else:
                    bs.log_veto_reason("long", dev, threshold)
            else:
                if bs.bid_wall:
                    # log.info(f"[DEBUG] Fade Long REJECT: Dev={dev:.2f}bps. Wall State={bs.bid_wall.state.name} (Need STABLE). WallPrice={bs.bid_wall.price}")
                    pass
                else:
                    # log.info(f"[DEBUG] Fade Long REJECT: Dev={dev:.2f}bps. No Bid Wall.")
                    pass
                    
        return None
