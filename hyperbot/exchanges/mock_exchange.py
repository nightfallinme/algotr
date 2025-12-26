# hyperbot/exchanges/mock_exchange.py
"""
Mock exchange adapter for testing without real orders.

Simulates order execution and position tracking.
Auto-registers with ExchangeRegistry.
"""
from typing import Optional, Dict, Any, List
from datetime import datetime
import random

from .base_exchange import BaseExchange
from .registry import ExchangeRegistry
from ..logger import get_logger, log_trade

log = get_logger(__name__)


@ExchangeRegistry.register("mock")
class MockExchange(BaseExchange):
    """
    Mock exchange for testing.
    
    Simulates:
    - Balance tracking
    - Position management (internal state)
    - Order execution (instant fills)
    - Price updates for PnL calculation
    """
    
    def __init__(self, config: dict):
        """
        Args:
            config: Configuration dict
        """
        self.config = config
        self.initial_balance = config.get("initial_balance", 10000.0)
        
        # Simulated state
        self._balance = self.initial_balance
        self._positions: Dict[str, Dict] = {}
        self._orders: Dict[int, Dict] = {}
        self._order_counter = 0
        self._connected = False
        self._prices: Dict[str, float] = {"BTC": 100000.0, "ETH": 3500.0}
        
        # SL/TP orders (set by ExecutionManager, checked on price update)
        self._pending_sl_tp: Dict[str, Dict] = {}  # symbol -> {stop_loss, take_profit}
    
    def connect(self) -> bool:
        """Connect to mock exchange."""
        self._connected = True
        log.info(f"Mock exchange connected. Balance: ${self._balance:.2f}")
        return True
    
    def disconnect(self) -> None:
        """Disconnect from mock exchange."""
        self._connected = False
        log.info("Mock exchange disconnected")
    
    @property
    def is_connected(self) -> bool:
        return self._connected
    
    def set_price(self, symbol: str, price: float) -> None:
        """
        Set simulated price for symbol.
        Called by ExecutionManager each tick for PnL updates.
        Also checks if SL/TP should be triggered.
        """
        self._prices[symbol] = price
        # Update position unrealized PnL
        if symbol in self._positions:
            pos = self._positions[symbol]
            pos["mark_price"] = price
            pos["unrealized_pnl"] = self._calculate_pnl(pos, price)
            
            # Check SL/TP triggers
            self._check_sl_tp_trigger(symbol, price)
    
    def set_sl_tp(self, symbol: str, stop_loss: float = None, take_profit: float = None) -> None:
        """
        Set stop loss and take profit levels for a symbol.
        These are checked on each price update.
        """
        if stop_loss or take_profit:
            self._pending_sl_tp[symbol] = {
                "stop_loss": stop_loss,
                "take_profit": take_profit
            }
            log.info(f"Mock SL/TP set for {symbol}: SL={stop_loss}, TP={take_profit}")
        else:
            # Clear SL/TP
            self._pending_sl_tp.pop(symbol, None)
    
    def _check_sl_tp_trigger(self, symbol: str, price: float) -> bool:
        """Check if SL or TP should trigger. Returns True if triggered."""
        if symbol not in self._pending_sl_tp:
            return False
        
        sl_tp = self._pending_sl_tp[symbol]
        pos = self._positions.get(symbol)
        
        if pos is None:
            return False
        
        side = pos["side"]
        triggered = False
        trigger_type = None
        
        # Check stop loss
        if sl_tp.get("stop_loss"):
            sl = sl_tp["stop_loss"]
            if side == "long" and price <= sl:
                triggered = True
                trigger_type = "STOP_LOSS"
            elif side == "short" and price >= sl:
                triggered = True
                trigger_type = "STOP_LOSS"
        
        # Check take profit
        if sl_tp.get("take_profit") and not triggered:
            tp = sl_tp["take_profit"]
            if side == "long" and price >= tp:
                triggered = True
                trigger_type = "TAKE_PROFIT"
            elif side == "short" and price <= tp:
                triggered = True
                trigger_type = "TAKE_PROFIT"
        
        if triggered:
            log.info(f"Mock {trigger_type} triggered for {symbol} at {price}")
            # Store trigger info for ExecutionManager to pick up
            pos["sl_tp_triggered"] = trigger_type
        
        return triggered
    
    def get_ticker(self, symbol: str) -> Optional[float]:
        """Get current mid-price for symbol (alias for get_price)."""
        return self.get_price(symbol)

    def get_price(self, symbol: str) -> float:
        """Get simulated price for symbol."""
        return self._prices.get(symbol, 100000.0)
    
    def get_balance(self) -> float:
        """Get simulated balance."""
        return self._balance
    
    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Get simulated position with current market data.
        
        Returns dict with:
        - symbol, side, size, entry_price
        - mark_price, unrealized_pnl (calculated from current price)
        """
        pos = self._positions.get(symbol)
        if pos is None:
            return None
        
        # Ensure mark_price and PnL are updated
        current_price = self._prices.get(symbol, pos["entry_price"])
        return {
            "symbol": symbol,
            "side": pos["side"],
            "size": pos["size"],
            "entry_price": pos["entry_price"],
            "mark_price": current_price,
            "unrealized_pnl": self._calculate_pnl(pos, current_price)
        }
    
    def get_open_orders(self, symbol: str) -> List[Dict[str, Any]]:
        """Get simulated open orders."""
        return [
            order for order in self._orders.values()
            if order.get("symbol") == symbol and order.get("status") == "open"
        ]
    
    def market_order(
        self, 
        symbol: str, 
        side: str, 
        size: float,
        reduce_only: bool = False
    ) -> Dict[str, Any]:
        """Execute simulated market order."""
        if not self.is_connected:
            return {"status": "error", "message": "Not connected"}
        
        price = self.get_price(symbol)
        is_buy = side.lower() == "buy"
        
        log_trade(
            action="MOCK_MARKET",
            symbol=symbol,
            side="LONG" if is_buy else "SHORT",
            size=size,
            price=price
        )
        
        # Update position
        current_pos = self._positions.get(symbol)
        
        if current_pos is None:
            # New position
            self._positions[symbol] = {
                "symbol": symbol,
                "size": size,
                "side": "long" if is_buy else "short",
                "entry_price": price,
                "unrealized_pnl": 0.0
            }
        else:
            # Modify existing position
            current_side = current_pos["side"]
            current_size = current_pos["size"]
            
            if (is_buy and current_side == "long") or (not is_buy and current_side == "short"):
                # Adding to position
                total_size = current_size + size
                avg_price = (current_pos["entry_price"] * current_size + price * size) / total_size
                current_pos["size"] = total_size
                current_pos["entry_price"] = avg_price
            else:
                # Reducing or reversing
                if size >= current_size:
                    # Close and potentially reverse
                    pnl = self._calculate_pnl(current_pos, price)
                    self._balance += pnl
                    
                    if size > current_size:
                        # Reverse
                        self._positions[symbol] = {
                            "symbol": symbol,
                            "size": size - current_size,
                            "side": "long" if is_buy else "short",
                            "entry_price": price,
                            "unrealized_pnl": 0.0
                        }
                    else:
                        # Full close
                        del self._positions[symbol]
                else:
                    # Partial close
                    pnl = self._calculate_pnl(current_pos, price, size)
                    self._balance += pnl
                    current_pos["size"] -= size
        
        self._order_counter += 1
        return {
            "status": "ok",
            "order_id": self._order_counter,
            "filled_price": price,
            "filled_size": size
        }
    
    def limit_order(
        self, 
        symbol: str, 
        side: str, 
        size: float,
        price: float,
        reduce_only: bool = False
    ) -> Dict[str, Any]:
        """Place simulated limit order (immediate fill for simplicity)."""
        # For mock, just fill immediately at requested price
        self.set_price(symbol, price)
        return self.market_order(symbol, side, size, reduce_only)
    
    def cancel_order(self, symbol: str, order_id: int) -> Dict[str, Any]:
        """Cancel simulated order."""
        if order_id in self._orders:
            self._orders[order_id]["status"] = "cancelled"
            return {"status": "ok"}
        return {"status": "error", "message": "Order not found"}
    
    def cancel_all_orders(self, symbol: str) -> Dict[str, Any]:
        """Cancel all simulated orders."""
        cancelled = 0
        for order in self._orders.values():
            if order.get("symbol") == symbol and order.get("status") == "open":
                order["status"] = "cancelled"
                cancelled += 1
        return {"status": "ok", "cancelled": cancelled}
    
    def close_position(
        self, 
        symbol: str, 
        size: Optional[float] = None
    ) -> Dict[str, Any]:
        """Close simulated position."""
        position = self._positions.get(symbol)
        if not position:
            return {"status": "ok", "message": "No position to close", "price": 0}
        
        close_size = size if size else position["size"]
        close_side = "sell" if position["side"] == "long" else "buy"
        
        # Get current price for exit
        exit_price = self.get_price(symbol)
        
        result = self.market_order(symbol, close_side, close_size, reduce_only=True)
        
        # Add exit price to result for analytics
        result["price"] = exit_price
        return result
    
    def set_leverage(self, symbol: str, leverage: int) -> Dict[str, Any]:
        """Set simulated leverage (no-op for mock)."""
        log.info(f"Mock leverage set to {leverage}x for {symbol}")
        return {"status": "ok"}
    
    def _calculate_pnl(
        self, 
        position: Dict, 
        exit_price: float, 
        size: Optional[float] = None
    ) -> float:
        """Calculate PnL for position."""
        pos_size = size if size else position["size"]
        entry = position["entry_price"]
        
        if position["side"] == "long":
            pnl = (exit_price - entry) * pos_size
        else:
            pnl = (entry - exit_price) * pos_size
        
        return pnl
    
    def get_stats(self) -> Dict[str, Any]:
        """Get mock exchange statistics."""
        return {
            "balance": self._balance,
            "initial_balance": self.initial_balance,
            "pnl": self._balance - self.initial_balance,
            "pnl_pct": ((self._balance / self.initial_balance) - 1) * 100,
            "open_positions": len(self._positions),
            "total_orders": self._order_counter
        }
