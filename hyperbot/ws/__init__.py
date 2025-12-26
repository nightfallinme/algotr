# hyperbot/ws/__init__.py
"""
WebSocket infrastructure for real-time OFI scalping.
"""
from .data_structures import RingBuffer, TimeWindowSum
from .book_manager import BookManager, BookState
from .trade_stream import TradeStreamManager
from .ws_manager import WSManager, WSState
from .binance_ws import BinanceWSManager, BinanceWSState

__all__ = [
    "RingBuffer",
    "TimeWindowSum", 
    "BookManager",
    "BookState",
    "TradeStreamManager",
    "WSManager",
    "WSState",
    "BinanceWSManager",
    "BinanceWSState",
]
