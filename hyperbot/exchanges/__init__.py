# hyperbot/exchanges/__init__.py
"""
Exchange adapters with factory pattern for easy switching.

Supported exchanges:
- Hyperliquid (default)
- Bybit

Usage:
    from hyperbot.exchanges import create_exchange
    
    exchange = create_exchange("bybit", config)
    exchange.connect()
"""
from typing import Optional, Dict, Any
from enum import Enum

from ..logger import get_logger

log = get_logger(__name__)


class ExchangeType(Enum):
    """Supported exchange types."""
    HYPERLIQUID = "hyperliquid"
    BYBIT = "bybit"


def create_exchange(exchange_type: str, config: Dict[str, Any]):
    """
    Factory function to create exchange adapter.
    
    Args:
        exchange_type: Exchange name ("hyperliquid" or "bybit")
        config: Execution config dict
        
    Returns:
        Exchange adapter instance
        
    Raises:
        ValueError: If exchange type not supported
    """
    exchange_type = exchange_type.lower()
    
    if exchange_type == "hyperliquid":
        from .hyperliquid_exchange import HyperliquidExchange
        return HyperliquidExchange(config)
    
    elif exchange_type == "bybit":
        from .bybit_exchange import BybitExchange
        return BybitExchange(config)
    
    else:
        raise ValueError(f"Unsupported exchange: {exchange_type}. Use 'hyperliquid' or 'bybit'")


def get_ws_manager_class(exchange_type: str):
    """
    Get WebSocket manager class for exchange.
    
    Args:
        exchange_type: Exchange name
        
    Returns:
        WebSocket manager class
    """
    exchange_type = exchange_type.lower()
    
    if exchange_type == "hyperliquid":
        from ..ws.ws_manager import WSManager
        return WSManager
    
    elif exchange_type == "bybit":
        from ..ws.bybit_ws import BybitWSManager
        return BybitWSManager
    
    else:
        raise ValueError(f"Unsupported exchange: {exchange_type}")


# Convenience imports
try:
    from .hyperliquid_exchange import HyperliquidExchange
except ImportError:
    HyperliquidExchange = None

try:
    from .bybit_exchange import BybitExchange
except ImportError:
    BybitExchange = None


__all__ = [
    "ExchangeType",
    "create_exchange",
    "get_ws_manager_class",
    "HyperliquidExchange",
    "BybitExchange",
    "ExchangeRegistry",
]

# Backwards compatibility - import ExchangeRegistry
try:
    from .registry import ExchangeRegistry
except ImportError:
    ExchangeRegistry = None
