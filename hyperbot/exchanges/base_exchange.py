# hyperbot/exchanges/base_exchange.py
"""
Abstract base class for exchange adapters.

All exchange implementations must inherit from this class
and implement all abstract methods.
"""
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List


class BaseExchange(ABC):
    """
    Abstract exchange interface.
    
    Provides a consistent API for different exchanges:
    - Hyperliquid (main execution)
    - Mock (testing)
    - Future: Binance, Bybit, etc.
    """
    
    @abstractmethod
    def connect(self) -> bool:
        """
        Establish connection to exchange.
        
        Returns:
            True if connection successful
        """
        pass
    
    @abstractmethod
    def disconnect(self) -> None:
        """Close connection to exchange."""
        pass
    
    @abstractmethod
    def get_balance(self) -> float:
        """
        Get account balance in USD.
        
        Returns:
            Available balance
        """
        pass
        
    @abstractmethod
    def get_ticker(self, symbol: str) -> Optional[float]:
        """
        Get current mid-price for symbol.
        
        Args:
            symbol: Trading symbol
            
        Returns:
            Current price or None
        """
        pass
    
    @abstractmethod
    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Get current position for symbol.
        
        Args:
            symbol: Trading symbol (e.g., "BTC")
            
        Returns:
            Position dict with keys: size, side, entry_price, unrealized_pnl
            None if no position
        """
        pass
    
    @abstractmethod
    def get_open_orders(self, symbol: str) -> List[Dict[str, Any]]:
        """
        Get all open orders for symbol.
        
        Args:
            symbol: Trading symbol
            
        Returns:
            List of order dicts
        """
        pass
    
    @abstractmethod
    def market_order(
        self, 
        symbol: str, 
        side: str, 
        size: float,
        reduce_only: bool = False
    ) -> Dict[str, Any]:
        """
        Place a market order.
        
        Args:
            symbol: Trading symbol
            side: "buy" or "sell"
            size: Order size
            reduce_only: If True, only reduce existing position
            
        Returns:
            Order result dict with status and details
        """
        pass
    
    @abstractmethod
    def limit_order(
        self, 
        symbol: str, 
        side: str, 
        size: float,
        price: float,
        reduce_only: bool = False
    ) -> Dict[str, Any]:
        """
        Place a limit order.
        
        Args:
            symbol: Trading symbol
            side: "buy" or "sell"
            size: Order size
            price: Limit price
            reduce_only: If True, only reduce existing position
            
        Returns:
            Order result dict
        """
        pass
    
    @abstractmethod
    def cancel_order(self, symbol: str, order_id: int) -> Dict[str, Any]:
        """
        Cancel an open order.
        
        Args:
            symbol: Trading symbol
            order_id: Order ID to cancel
            
        Returns:
            Cancel result dict
        """
        pass
    
    @abstractmethod
    def cancel_all_orders(self, symbol: str) -> Dict[str, Any]:
        """
        Cancel all open orders for symbol.
        
        Args:
            symbol: Trading symbol
            
        Returns:
            Cancel result dict
        """
        pass
    
    @abstractmethod
    def close_position(
        self, 
        symbol: str, 
        size: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Close position (full or partial).
        
        Args:
            symbol: Trading symbol
            size: Size to close (None = full position)
            
        Returns:
            Close result dict
        """
        pass
    
    @abstractmethod
    def set_leverage(self, symbol: str, leverage: int) -> Dict[str, Any]:
        """
        Set leverage for symbol.
        
        Args:
            symbol: Trading symbol
            leverage: Leverage multiplier
            
        Returns:
            Result dict
        """
        pass

    @abstractmethod
    def set_sl_tp(
        self, 
        symbol: str, 
        stop_loss: Optional[float] = None, 
        take_profit: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Set Stop Loss and Take Profit levels/orders.
        
        Args:
            symbol: Trading symbol
            stop_loss: Stop loss price
            take_profit: Take profit price
        
        Returns:
            Result dict
        """
        pass
    
    @abstractmethod
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
        
        This is the preferred order type for OFI scalping:
        - Fills immediately at limit price or better
        - Unfilled portion is cancelled
        - No maker rebates, pure taker
        
        Args:
            symbol: Trading symbol
            side: "buy" or "sell"
            size: Order size
            limit_price: Maximum price (buy) or minimum price (sell)
            reduce_only: If True, only reduce existing position
            client_order_id: Optional client-side order ID for idempotency
            
        Returns:
            Order result dict with: status, order_id, filled_qty, avg_price
        """
        pass
    
    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Check if connected to exchange."""
        pass
