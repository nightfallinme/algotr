# hyperbot/__init__.py
"""
HyperBot OFI Scalper

Architecture:
- OFI Orchestrator: Real-time WebSocket-driven decision loop
- Book/Trade Managers: L2 orderbook and trade stream handling
- OFI Calculator: Order flow imbalance signal generation
- Filters: Volatility, Spread, Net Edge checks
- Order Manager: State machine with safety controls
- Exchange Adapters: Hyperliquid, Mock

Modes:
- Shadow: Signal generation only, no execution
- Paper: Small size real execution
- Live: Full execution
"""

__version__ = "2.0.0"
__author__ = "HyperBot Team"

# Core
from .config import Config, get_config, reload_config
from .logger import get_logger, setup_logging

# Exchanges
from .exchanges import ExchangeRegistry

# OFI components
from .ofi_orchestrator import OFIOrchestrator, OFIConfig, ExecutionMode

__all__ = [
    # Config
    "Config",
    "get_config",
    "reload_config",
    # Logger
    "get_logger",
    "setup_logging",
    # Exchanges
    "ExchangeRegistry",
    # OFI
    "OFIOrchestrator",
    "OFIConfig",
    "ExecutionMode",
]
