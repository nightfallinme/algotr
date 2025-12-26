# hyperbot/exchanges/hyperliquid_exchange.py
"""
Hyperliquid exchange adapter for perpetual futures trading.

Uses the official hyperliquid-python-sdk.
Mainnet URL: https://api.hyperliquid.xyz
Auto-registers with ExchangeRegistry.
"""
from typing import Optional, Dict, Any, List

import eth_account
from eth_account.signers.local import LocalAccount

from .base_exchange import BaseExchange
from .registry import ExchangeRegistry
from ..logger import get_logger, log_trade

log = get_logger(__name__)

# Import Hyperliquid SDK
try:
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
    from hyperliquid.utils.constants import MAINNET_API_URL
    HYPERLIQUID_AVAILABLE = True
except ImportError:
    HYPERLIQUID_AVAILABLE = False
    MAINNET_API_URL = "https://api.hyperliquid.xyz"
    log.warning("hyperliquid-python-sdk not installed. Install with: pip install hyperliquid-python-sdk")


@ExchangeRegistry.register("hyperliquid")
class HyperliquidExchange(BaseExchange):
    """
    Hyperliquid perpetual futures exchange adapter.
    
    Requires:
    - pip install hyperliquid-python-sdk
    - HL_SECRET_KEY environment variable
    - HL_ACCOUNT_ADDRESS environment variable
    """
    
    def __init__(self, config: dict):
        """
        Args:
            config: Execution config with secret_key, account_address, etc.
        """
        self.config = config
        self.secret_key = config.get("secret_key")
        self.account_address = config.get("account_address")
        self.default_leverage = config.get("leverage", 5)
        self.slippage = config.get("slippage_pct", 0.05)
        
        self._exchange: Optional[Exchange] = None
        self._info: Optional[Info] = None
        self._account: Optional[LocalAccount] = None
        self._connected = False
        
        # Rate limiter for order execution
        from ..execution.rate_limiter import RateLimiter, RateLimitStatus
        self._rate_limiter = RateLimiter()
        self._RateLimitStatus = RateLimitStatus
    
    def connect(self) -> bool:
        """Connect to Hyperliquid mainnet."""
        if not HYPERLIQUID_AVAILABLE:
            log.error("Hyperliquid SDK not available")
            return False
        
        if not self.secret_key:
            log.error("HL_SECRET_KEY not set")
            return False
        
        if not self.account_address:
            log.error("HL_ACCOUNT_ADDRESS not set")
            return False
        
        try:
            # Create account from private key
            self._account = eth_account.Account.from_key(self.secret_key)
            
            # Initialize Info client (read-only)
            self._info = Info(MAINNET_API_URL, skip_ws=True)
            
            # Initialize Exchange client (trading)
            self._exchange = Exchange(
                self._account,
                MAINNET_API_URL,
                account_address=self.account_address
            )
            
            # Verify connection by fetching user state
            user_state = self._info.user_state(self.account_address)
            balance = float(user_state["marginSummary"]["accountValue"])
            
            log.info(f"Connected to Hyperliquid. Balance: ${balance:.2f}")
            self._connected = True
            return True
            
        except Exception as e:
            log.error(f"Failed to connect to Hyperliquid: {e}")
            self._connected = False
            return False
    
    def disconnect(self) -> None:
        """Disconnect from Hyperliquid."""
        self._exchange = None
        self._info = None
        self._account = None
        self._connected = False
        log.info("Disconnected from Hyperliquid")
    
    @property
    def is_connected(self) -> bool:
        return self._connected and self._exchange is not None
    
    def get_balance(self) -> float:
        """Get account balance in USD."""
        if not self.is_connected:
            return 0.0
        
        try:
            user_state = self._info.user_state(self.account_address)
            return float(user_state["marginSummary"]["accountValue"])
        except Exception as e:
            log.error(f"Failed to get balance: {e}")
            return 0.0
            
    def get_ticker(self, symbol: str) -> Optional[float]:
        """
        Get current mid-price for symbol from Hyperliquid.
        
        Args:
            symbol: Coin symbol (e.g. "ETH", "BTC")
            
        Returns:
            Current mid price or None if failed
        """
        if not self.is_connected:
            return None
            
        try:
            # all_mids returns dict: {"ETH": "1234.5", "BTC": "20000.0"}
            all_mids = self._info.all_mids()
            price_str = all_mids.get(symbol)
            
            if price_str:
                return float(price_str)
            return None
            
        except Exception as e:
            log.error(f"Failed to get ticker for {symbol}: {e}")
            return None
    
    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get current position for symbol."""
        if not self.is_connected:
            return None
        
        try:
            user_state = self._info.user_state(self.account_address)
            
            for pos in user_state.get("assetPositions", []):
                position = pos.get("position", {})
                if position.get("coin") == symbol:
                    size = float(position.get("szi", 0))
                    if size == 0:
                        return None
                    
                    return {
                        "symbol": symbol,
                        "size": abs(size),
                        "side": "long" if size > 0 else "short",
                        "entry_price": float(position.get("entryPx", 0)),
                        "unrealized_pnl": float(position.get("unrealizedPnl", 0)),
                        "liquidation_price": float(position.get("liquidationPx", 0)),
                        "leverage": float(position.get("leverage", {}).get("value", 1)),
                        "raw": position
                    }
            
            return None
            
        except Exception as e:
            log.error(f"Failed to get position: {e}")
            return None
    
    def get_open_orders(self, symbol: str) -> List[Dict[str, Any]]:
        """Get all open orders for symbol."""
        if not self.is_connected:
            return []
        
        try:
            open_orders = self._info.open_orders(self.account_address)
            return [
                order for order in open_orders 
                if order.get("coin") == symbol
            ]
        except Exception as e:
            log.error(f"Failed to get open orders: {e}")
            return []
    
    def market_order(
        self, 
        symbol: str, 
        side: str, 
        size: float,
        reduce_only: bool = False
    ) -> Dict[str, Any]:
        """Place a market order."""
        if not self.is_connected:
            return {"status": "error", "message": "Not connected"}
        
        try:
            is_buy = side.lower() == "buy"
            
            log_trade(
                action="MARKET_ORDER",
                symbol=symbol,
                side="LONG" if is_buy else "SHORT",
                size=size,
                price=0,  # Market order
                reduce_only=reduce_only
            )
            
            result = self._exchange.market_open(
                symbol, 
                is_buy, 
                size,
                slippage=self.slippage
            )
            
            log.info(f"Market order result: {result}")
            return result
            
        except Exception as e:
            log.error(f"Market order failed: {e}")
            return {"status": "error", "message": str(e)}
    
    def limit_order(
        self, 
        symbol: str, 
        side: str, 
        size: float,
        price: float,
        reduce_only: bool = False
    ) -> Dict[str, Any]:
        """Place a limit order."""
        if not self.is_connected:
            return {"status": "error", "message": "Not connected"}
        
        try:
            is_buy = side.lower() == "buy"
            
            log_trade(
                action="LIMIT_ORDER",
                symbol=symbol,
                side="LONG" if is_buy else "SHORT",
                size=size,
                price=price,
                reduce_only=reduce_only
            )
            
            result = self._exchange.order(
                symbol,
                is_buy,
                size,
                price,
                order_type={"limit": {"tif": "Gtc"}},
                reduce_only=reduce_only
            )
            
            log.info(f"Limit order result: {result}")
            return result
            
        except Exception as e:
            log.error(f"Limit order failed: {e}")
            return {"status": "error", "message": str(e)}
    
    def ioc_order(
        self,
        symbol: str,
        side: str,
        size: float,
        limit_price: float,
        reduce_only: bool = False,
        client_order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Place an IOC (Immediate-Or-Cancel) aggressive limit order.
        
        IOC orders fill immediately at limit price or better, or cancel.
        Preferred for OFI scalping to control slippage.
        """
        if not self.is_connected:
            return {"status": "error", "message": "Not connected"}
        
        # Rate limit check
        rate_status = self._rate_limiter.check()
        if rate_status == self._RateLimitStatus.HARD_LIMIT:
            log.warning("Rate limit: at hard limit, rejecting order")
            return {"status": "error", "message": "rate_limited", "client_order_id": client_order_id}
        if rate_status == self._RateLimitStatus.BACKOFF:
            wait_time = self._rate_limiter.get_wait_time()
            log.warning(f"Rate limit: in backoff, wait {wait_time:.1f}s")
            return {"status": "error", "message": "rate_limited_backoff", "client_order_id": client_order_id}
        if rate_status == self._RateLimitStatus.SOFT_LIMIT:
            log.info("Rate limit: approaching limit, order allowed")
        
        try:
            is_buy = side.lower() == "buy"
            
            log_trade(
                action="IOC_ORDER",
                symbol=symbol,
                side="LONG" if is_buy else "SHORT",
                size=size,
                price=limit_price,
                reduce_only=reduce_only
            )
            
            # Hyperliquid IOC order type
            order_type = {"limit": {"tif": "Ioc"}}
            
            # Record order before sending
            self._rate_limiter.record_order()
            
            result = self._exchange.order(
                symbol,
                is_buy,
                size,
                limit_price,
                order_type=order_type,
                reduce_only=reduce_only
            )
            
            log.info(f"IOC order result: {result}")
            
            # Parse result for standardized response
            response = result.get("response", {})
            data = response.get("data", {})
            statuses = data.get("statuses", [])
            
            if statuses and isinstance(statuses[0], dict):
                status_info = statuses[0]
                if "filled" in status_info:
                    filled = status_info["filled"]
                    self._rate_limiter.on_success()  # Reset backoff on success
                    return {
                        "status": "ok",
                        "order_id": filled.get("oid"),
                        "filled_qty": float(filled.get("totalSz", 0)),
                        "avg_price": float(filled.get("avgPx", 0)),
                        "client_order_id": client_order_id,
                    }
                elif "error" in status_info:
                    error_msg = status_info["error"]
                    # Check for rate limit error
                    if "rate" in error_msg.lower() or "limit" in error_msg.lower():
                        self._rate_limiter.on_rate_limit_error()
                    return {
                        "status": "error",
                        "message": error_msg,
                        "client_order_id": client_order_id,
                    }
            
            self._rate_limiter.on_success()
            return result
            
        except Exception as e:
            error_str = str(e).lower()
            # Check for rate limit in exception
            if "rate" in error_str or "429" in error_str:
                self._rate_limiter.on_rate_limit_error()
            log.error(f"IOC order failed: {e}")
            return {"status": "error", "message": str(e), "client_order_id": client_order_id}
    
    def cancel_order(self, symbol: str, order_id: int) -> Dict[str, Any]:
        """Cancel an open order."""
        if not self.is_connected:
            return {"status": "error", "message": "Not connected"}
        
        try:
            result = self._exchange.cancel(symbol, order_id)
            log.info(f"Cancel order result: {result}")
            return result
        except Exception as e:
            log.error(f"Cancel order failed: {e}")
            return {"status": "error", "message": str(e)}
    
    def cancel_all_orders(self, symbol: str) -> Dict[str, Any]:
        """Cancel all open orders for symbol."""
        if not self.is_connected:
            return {"status": "error", "message": "Not connected"}
        
        try:
            open_orders = self.get_open_orders(symbol)
            results = []
            
            for order in open_orders:
                oid = order.get("oid")
                if oid:
                    result = self._exchange.cancel(symbol, oid)
                    results.append(result)
            
            return {"status": "ok", "cancelled": len(results)}
            
        except Exception as e:
            log.error(f"Cancel all orders failed: {e}")
            return {"status": "error", "message": str(e)}
    
    def close_position(
        self, 
        symbol: str, 
        size: Optional[float] = None
    ) -> Dict[str, Any]:
        """Close position (full or partial)."""
        if not self.is_connected:
            return {"status": "error", "message": "Not connected"}
        
        try:
            log_trade(
                action="CLOSE_POSITION",
                symbol=symbol,
                side="",
                size=size or 0,
                price=0
            )
            
            result = self._exchange.market_close(
                symbol, 
                sz=size,
                slippage=self.slippage
            )
            
            log.info(f"Close position result: {result}")
            return result
            
        except Exception as e:
            log.error(f"Close position failed: {e}")
            return {"status": "error", "message": str(e)}
    
    def set_leverage(self, symbol: str, leverage: int) -> Dict[str, Any]:
        """Set leverage for symbol."""
        if not self.is_connected:
            return {"status": "error", "message": "Not connected"}
        
        try:
            result = self._exchange.update_leverage(
                leverage, 
                symbol, 
                is_cross=True
            )
            log.info(f"Set leverage to {leverage}x: {result}")
            return result
        except Exception as e:
            log.error(f"Set leverage failed: {e}")
            return {"status": "error", "message": str(e)}

    def set_sl_tp(
        self, 
        symbol: str, 
        stop_loss: Optional[float] = None, 
        take_profit: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Set Stop Loss and Take Profit trigger orders.
        
        Logic:
        1. Get current position size/side.
        2. Cancel existing open orders for symbol.
        3. Place new SL/TP trigger orders (reduce-only).
        """
        if not self.is_connected:
            return {"status": "error", "message": "Not connected"}
            
        try:
            # 1. Get position info
            pos = self.get_position(symbol)
            if not pos:
                log.warning(f"Set SL/TP skipped: No position found for {symbol}")
                return {"status": "skipped", "reason": "no_position"}
            
            size = pos["size"]
            side = pos["side"]
            is_buy_close = (side == "short") # If Short, we Buy to close
            
            # 2. Cancel existing orders
            self.cancel_all_orders(symbol)
            
            results = {"sl": None, "tp": None}
            
            # 3. Place SL (Stop Market)
            if stop_loss:
                # Round price to valid precision (assuming 0.1 for now or SDK handles it)
                # SDK 'order' automatically handles float precision mostly, but safer to be explicit if needed
                
                # Order Type: Stop Market
                order_type = {
                    "trigger": {
                        "isMarket": True, 
                        "triggerPx": stop_loss, 
                        "tpsl": "sl"
                    }
                }
                
                res_sl = self._exchange.order(
                    symbol,
                    is_buy_close,
                    size,
                    stop_loss, 
                    order_type=order_type,
                    reduce_only=True
                )
                results["sl"] = res_sl
                log.info(f"Set SL for {symbol} at {stop_loss}: {res_sl}")
            
            # 4. Place TP (Take Profit Market)
            if take_profit:
                order_type = {
                    "trigger": {
                        "isMarket": True, 
                        "triggerPx": take_profit, 
                        "tpsl": "tp"
                    }
                }
                
                res_tp = self._exchange.order(
                    symbol,
                    is_buy_close,
                    size,
                    take_profit,
                    order_type=order_type,
                    reduce_only=True
                )
                results["tp"] = res_tp
                log.info(f"Set TP for {symbol} at {take_profit}: {res_tp}")
            
            return {"status": "ok", "results": results}
            
        except Exception as e:
            log.error(f"Set SL/TP failed: {e}")
            return {"status": "error", "message": str(e)}
