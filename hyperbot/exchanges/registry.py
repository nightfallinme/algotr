# hyperbot/exchanges/registry.py
"""
Exchange Registry - Auto-discovery pattern for exchange adapters.

Each exchange registers itself on import using the @register decorator.
Config determines which exchange is instantiated at runtime.

Usage:
    # In exchange file:
    @ExchangeRegistry.register("hyperliquid")
    class HyperliquidExchange(BaseExchange):
        ...
    
    # In ExecutionManager:
    exchange = ExchangeRegistry.get("hyperliquid", config)
"""
from typing import Dict, Type, List, Optional, Callable
from .base_exchange import BaseExchange
from ..logger import get_logger

log = get_logger(__name__)


class ExchangeRegistry:
    """
    Auto-registration registry for exchange adapters.
    
    Exchanges register themselves using the @register decorator.
    This allows adding new exchanges without modifying core code.
    """
    
    _exchanges: Dict[str, Type[BaseExchange]] = {}
    
    @classmethod
    def register(cls, name: str) -> Callable:
        """
        Decorator to register an exchange class.
        
        Args:
            name: Unique identifier for the exchange (e.g., "hyperliquid", "mock")
            
        Returns:
            Decorator function
            
        Example:
            @ExchangeRegistry.register("mock")
            class MockExchange(BaseExchange):
                ...
        """
        def decorator(exchange_class: Type[BaseExchange]) -> Type[BaseExchange]:
            if name in cls._exchanges:
                log.warning(f"Exchange '{name}' already registered, overwriting")
            cls._exchanges[name] = exchange_class
            log.debug(f"Registered exchange: {name}")
            return exchange_class
        return decorator
    
    @classmethod
    def get(cls, name: str, config: dict) -> BaseExchange:
        """
        Get an instance of a registered exchange.
        
        Args:
            name: Exchange identifier
            config: Configuration dict for the exchange
            
        Returns:
            Instantiated exchange adapter
            
        Raises:
            ValueError: If exchange not found
        """
        if name not in cls._exchanges:
            available = ", ".join(cls._exchanges.keys()) or "none"
            raise ValueError(
                f"Unknown exchange: '{name}'. Available: {available}"
            )
        
        exchange_class = cls._exchanges[name]
        log.info(f"Creating exchange: {name}")
        return exchange_class(config)
    
    @classmethod
    def available(cls) -> List[str]:
        """List all registered exchange names."""
        return list(cls._exchanges.keys())
    
    @classmethod
    def is_registered(cls, name: str) -> bool:
        """Check if an exchange is registered."""
        return name in cls._exchanges
    
    @classmethod
    def clear(cls) -> None:
        """Clear all registrations (for testing)."""
        cls._exchanges.clear()
