# hyperbot/exchanges/bybit_exchange.py
"""
Bybit exchange adapter for perpetual futures trading.

Uses pybit SDK for Bybit V5 API.
Testnet URL: https://api-testnet.bybit.com
Mainnet URL: https://api.bybit.com
"""
import os
import time
from typing import Optional, Dict, Any, List
from dataclasses import dataclass

from ..logger import get_logger

log = get_logger(__name__)

# Try to import pybit
try:
    from pybit.unified_trading import HTTP
    PYBIT_AVAILABLE = True
except ImportError:
    PYBIT_AVAILABLE = False
    log.warning("pybit not installed. Install with: pip install pybit")


@dataclass
class BybitPosition:
    """Position data from Bybit."""
    symbol: str
    side: str  # "Buy" or "Sell"
    size: float
    entry_price: float
    unrealized_pnl: float
    leverage: int


@dataclass
class BybitOrder:
    """Order data from Bybit."""
    order_id: str
    symbol: str
    side: str
    order_type: str
    price: float
    qty: float
    status: str
    reduce_only: bool


class BybitExchange:
    """
    Bybit perpetual futures exchange adapter.
    
    Requires:
    - pip install pybit
    - BYBIT_API_KEY environment variable
    - BYBIT_API_SECRET environment variable
    
    Interface matches HyperliquidExchange for drop-in replacement.
    """
    
    def __init__(self, config: dict):
        """
        Args:
            config: Execution config with api_key, api_secret, testnet flag, etc.
        """
        self.config = config
        self._client: Optional[HTTP] = None
        self._connected = False
        
        # Get credentials from config or environment
        self._api_key = config.get("api_key") or os.getenv("BYBIT_API_KEY")
        self._api_secret = config.get("api_secret") or os.getenv("BYBIT_API_SECRET")
        self._testnet = config.get("testnet", False)
        
        # Symbol mapping (Hyperliquid style -> Bybit style)
        # Hyperliquid uses "ETH", Bybit uses "ETHUSDT"
        self._symbol_suffix = "USDT"
    
    def _to_bybit_symbol(self, symbol: str) -> str:
        """Convert symbol to Bybit format."""
        if not symbol.endswith("USDT"):
            return f"{symbol}{self._symbol_suffix}"
        return symbol
    
    def _from_bybit_symbol(self, symbol: str) -> str:
        """Convert Bybit symbol to standard format."""
        if symbol.endswith("USDT"):
            return symbol[:-4]
        return symbol
    
    def connect(self) -> bool:
        """Connect to Bybit API."""
        if not PYBIT_AVAILABLE:
            log.error("pybit SDK not available. Install with: pip install pybit")
            return False
        
        if not self._api_key or not self._api_secret:
            log.error("Bybit API credentials not configured")
            return False
        
        try:
            self._client = HTTP(
                testnet=self._testnet,
                api_key=self._api_key,
                api_secret=self._api_secret,
            )
            
            # Test connection by getting account info
            result = self._client.get_wallet_balance(accountType="UNIFIED")
            
            if result.get("retCode") == 0:
                self._connected = True
                network = "testnet" if self._testnet else "mainnet"
                log.info(f"Connected to Bybit ({network})")
                return True
            else:
                log.error(f"Bybit connection failed: {result.get('retMsg')}")
                return False
                
        except Exception as e:
            log.error(f"Bybit connection error: {e}")
            return False
    
    def disconnect(self) -> None:
        """Disconnect from Bybit."""
        self._client = None
        self._connected = False
        log.info("Disconnected from Bybit")
    
    @property
    def is_connected(self) -> bool:
        return self._connected and self._client is not None
    
    def get_balance(self) -> Optional[float]:
        """Get account balance in USD."""
        if not self.is_connected:
            return None
        
        try:
            result = self._client.get_wallet_balance(accountType="UNIFIED")
            
            if result.get("retCode") == 0:
                # Find USDT balance
                for coin in result["result"]["list"][0]["coin"]:
                    if coin["coin"] == "USDT":
                        return float(coin["walletBalance"])
            return None
            
        except Exception as e:
            log.error(f"Failed to get balance: {e}")
            return None
    
    def get_ticker(self, symbol: str) -> Optional[float]:
        """
        Get current mid-price for symbol.
        
        Args:
            symbol: Coin symbol (e.g. "ETH", "BTC")
            
        Returns:
            Current mid price or None if failed
        """
        if not self.is_connected:
            return None
        
        try:
            bybit_symbol = self._to_bybit_symbol(symbol)
            result = self._client.get_tickers(
                category="linear",
                symbol=bybit_symbol,
            )
            
            if result.get("retCode") == 0 and result["result"]["list"]:
                ticker = result["result"]["list"][0]
                bid = float(ticker["bid1Price"])
                ask = float(ticker["ask1Price"])
                return (bid + ask) / 2
            return None
            
        except Exception as e:
            log.error(f"Failed to get ticker for {symbol}: {e}")
            return None
    
    def get_position(self, symbol: str) -> Optional[BybitPosition]:
        """Get current position for symbol."""
        if not self.is_connected:
            return None
        
        try:
            bybit_symbol = self._to_bybit_symbol(symbol)
            result = self._client.get_positions(
                category="linear",
                symbol=bybit_symbol,
            )
            
            if result.get("retCode") == 0 and result["result"]["list"]:
                pos = result["result"]["list"][0]
                size = float(pos["size"])
                
                if size == 0:
                    return None
                
                return BybitPosition(
                    symbol=symbol,
                    side=pos["side"],
                    size=size,
                    entry_price=float(pos["avgPrice"]),
                    unrealized_pnl=float(pos["unrealisedPnl"]),
                    leverage=int(pos["leverage"]),
                )
            return None
            
        except Exception as e:
            log.error(f"Failed to get position for {symbol}: {e}")
            return None
    
    def get_open_orders(self, symbol: str) -> List[BybitOrder]:
        """Get all open orders for symbol."""
        if not self.is_connected:
            return []
        
        try:
            bybit_symbol = self._to_bybit_symbol(symbol)
            result = self._client.get_open_orders(
                category="linear",
                symbol=bybit_symbol,
            )
            
            orders = []
            if result.get("retCode") == 0:
                for order in result["result"]["list"]:
                    orders.append(BybitOrder(
                        order_id=order["orderId"],
                        symbol=symbol,
                        side=order["side"],
                        order_type=order["orderType"],
                        price=float(order["price"]) if order["price"] else 0.0,
                        qty=float(order["qty"]),
                        status=order["orderStatus"],
                        reduce_only=order["reduceOnly"],
                    ))
            return orders
            
        except Exception as e:
            log.error(f"Failed to get open orders for {symbol}: {e}")
            return []
    
    def market_order(
        self, 
        symbol: str, 
        side: str, 
        size: float,
        reduce_only: bool = False
    ) -> Optional[str]:
        """Place a market order."""
        if not self.is_connected:
            return None
        
        try:
            bybit_symbol = self._to_bybit_symbol(symbol)
            bybit_side = "Buy" if side.lower() == "buy" else "Sell"
            
            result = self._client.place_order(
                category="linear",
                symbol=bybit_symbol,
                side=bybit_side,
                orderType="Market",
                qty=str(size),
                reduceOnly=reduce_only,
            )
            
            if result.get("retCode") == 0:
                order_id = result["result"]["orderId"]
                log.info(f"Market order placed: {bybit_side} {size} {symbol} -> {order_id}")
                return order_id
            else:
                log.error(f"Market order failed: {result.get('retMsg')}")
                return None
                
        except Exception as e:
            log.error(f"Market order error: {e}")
            return None
    
    def limit_order(
        self, 
        symbol: str, 
        side: str, 
        size: float,
        price: float,
        reduce_only: bool = False,
        post_only: bool = False,
    ) -> Optional[str]:
        """Place a limit order."""
        if not self.is_connected:
            return None
        
        try:
            bybit_symbol = self._to_bybit_symbol(symbol)
            bybit_side = "Buy" if side.lower() == "buy" else "Sell"
            
            time_in_force = "PostOnly" if post_only else "GTC"
            
            result = self._client.place_order(
                category="linear",
                symbol=bybit_symbol,
                side=bybit_side,
                orderType="Limit",
                qty=str(size),
                price=str(price),
                reduceOnly=reduce_only,
                timeInForce=time_in_force,
            )
            
            if result.get("retCode") == 0:
                order_id = result["result"]["orderId"]
                log.info(f"Limit order placed: {bybit_side} {size} {symbol} @ {price} -> {order_id}")
                return order_id
            else:
                log.error(f"Limit order failed: {result.get('retMsg')}")
                return None
                
        except Exception as e:
            log.error(f"Limit order error: {e}")
            return None
    
    def ioc_order(
        self,
        symbol: str,
        side: str,
        size: float,
        limit_price: float,
        reduce_only: bool = False,
        client_order_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Place an IOC (Immediate-Or-Cancel) aggressive limit order.
        
        IOC orders fill immediately at limit price or better, or cancel.
        """
        if not self.is_connected:
            return None
        
        try:
            bybit_symbol = self._to_bybit_symbol(symbol)
            bybit_side = "Buy" if side.lower() == "buy" else "Sell"
            
            params = {
                "category": "linear",
                "symbol": bybit_symbol,
                "side": bybit_side,
                "orderType": "Limit",
                "qty": str(size),
                "price": str(limit_price),
                "reduceOnly": reduce_only,
                "timeInForce": "IOC",
            }
            
            if client_order_id:
                params["orderLinkId"] = client_order_id
            
            result = self._client.place_order(**params)
            
            if result.get("retCode") == 0:
                order_id = result["result"]["orderId"]
                log.info(f"IOC order placed: {bybit_side} {size} {symbol} @ {limit_price} -> {order_id}")
                
                # Return order info
                return {
                    "order_id": order_id,
                    "client_order_id": client_order_id,
                    "symbol": symbol,
                    "side": side,
                    "size": size,
                    "price": limit_price,
                    "status": "NEW",
                }
            else:
                log.error(f"IOC order failed: {result.get('retMsg')}")
                return None
                
        except Exception as e:
            log.error(f"IOC order error: {e}")
            return None
    
    def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Cancel an open order."""
        if not self.is_connected:
            return False
        
        try:
            bybit_symbol = self._to_bybit_symbol(symbol)
            result = self._client.cancel_order(
                category="linear",
                symbol=bybit_symbol,
                orderId=order_id,
            )
            
            if result.get("retCode") == 0:
                log.info(f"Order cancelled: {order_id}")
                return True
            else:
                log.error(f"Cancel order failed: {result.get('retMsg')}")
                return False
                
        except Exception as e:
            log.error(f"Cancel order error: {e}")
            return False
    
    def cancel_all_orders(self, symbol: str) -> bool:
        """Cancel all open orders for symbol."""
        if not self.is_connected:
            return False
        
        try:
            bybit_symbol = self._to_bybit_symbol(symbol)
            result = self._client.cancel_all_orders(
                category="linear",
                symbol=bybit_symbol,
            )
            
            if result.get("retCode") == 0:
                log.info(f"All orders cancelled for {symbol}")
                return True
            else:
                log.error(f"Cancel all orders failed: {result.get('retMsg')}")
                return False
                
        except Exception as e:
            log.error(f"Cancel all orders error: {e}")
            return False
    
    def close_position(
        self, 
        symbol: str, 
        size: Optional[float] = None
    ) -> Optional[str]:
        """Close position (full or partial)."""
        if not self.is_connected:
            return None
        
        try:
            pos = self.get_position(symbol)
            if not pos:
                log.warning(f"No position to close for {symbol}")
                return None
            
            close_size = size if size else pos.size
            close_side = "Sell" if pos.side == "Buy" else "Buy"
            
            return self.market_order(
                symbol=symbol,
                side=close_side.lower(),
                size=close_size,
                reduce_only=True,
            )
            
        except Exception as e:
            log.error(f"Close position error: {e}")
            return None
    
    def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Set leverage for symbol."""
        if not self.is_connected:
            return False
        
        try:
            bybit_symbol = self._to_bybit_symbol(symbol)
            result = self._client.set_leverage(
                category="linear",
                symbol=bybit_symbol,
                buyLeverage=str(leverage),
                sellLeverage=str(leverage),
            )
            
            if result.get("retCode") == 0:
                log.info(f"Leverage set to {leverage}x for {symbol}")
                return True
            else:
                # Check if leverage already set (not an error)
                if "110043" in str(result.get("retCode")):
                    return True
                log.error(f"Set leverage failed: {result.get('retMsg')}")
                return False
                
        except Exception as e:
            log.error(f"Set leverage error: {e}")
            return False
    
    def set_sl_tp(
        self, 
        symbol: str, 
        stop_loss: Optional[float] = None, 
        take_profit: Optional[float] = None
    ) -> bool:
        """
        Set Stop Loss and Take Profit for position.
        
        Uses Bybit's trading stop endpoint.
        """
        if not self.is_connected:
            return False
        
        try:
            pos = self.get_position(symbol)
            if not pos:
                log.warning(f"No position to set SL/TP for {symbol}")
                return False
            
            bybit_symbol = self._to_bybit_symbol(symbol)
            
            params = {
                "category": "linear",
                "symbol": bybit_symbol,
                "positionIdx": 0,  # One-way mode
            }
            
            if stop_loss:
                params["stopLoss"] = str(stop_loss)
            if take_profit:
                params["takeProfit"] = str(take_profit)
            
            result = self._client.set_trading_stop(**params)
            
            if result.get("retCode") == 0:
                log.info(f"SL/TP set for {symbol}: SL={stop_loss}, TP={take_profit}")
                return True
            else:
                log.error(f"Set SL/TP failed: {result.get('retMsg')}")
                return False
                
        except Exception as e:
            log.error(f"Set SL/TP error: {e}")
            return False
