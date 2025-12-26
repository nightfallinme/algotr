# hyperbot/ws/binance_ws.py
"""
Binance Futures WebSocket for structural leader signals.

This is a READ-ONLY adapter for Binance Futures data.
Used as STRUCTURAL LEADER for Vacuum Scalping strategy.

Purpose:
- Liquidity Wall Detection: Stable walls for fade/break decisions
- Vacuum Geometry: Thin liquidity zones
- Global Regime: Range vs impulse classification

Streams:
- bookTicker: Best Bid/Ask updates
- aggTrade: Aggregated trades
- depth@20: L2 orderbook (20 levels) for wall detection
"""
import asyncio
import json
import time
from dataclasses import dataclass
from typing import Optional, Dict, Any, Callable, List, Awaitable
from enum import Enum, auto

from ..logger import get_logger

log = get_logger(__name__)

# Binance Futures WebSocket URL
BINANCE_FUTURES_WS_URL = "wss://fstream.binance.com/ws"


class BinanceWSState(Enum):
    """WebSocket connection states."""
    DISCONNECTED = auto()
    CONNECTING = auto()
    CONNECTED = auto()
    READY = auto()
    ERROR = auto()


@dataclass
class BinanceWSConfig:
    """Binance WebSocket configuration."""
    base_url: str = BINANCE_FUTURES_WS_URL
    reconnect_delay_initial: float = 1.0
    reconnect_delay_max: float = 30.0
    reconnect_delay_multiplier: float = 2.0
    ping_interval: float = 30.0
    depth_levels: int = 20  # L2 orderbook depth (5, 10, or 20)
    depth_update_speed: str = "500ms"  # "100ms" or "500ms"


class BinanceWSManager:
    """
    Read-only WebSocket connection to Binance Futures.
    
    Used purely for signal generation (OFI fusion).
    Does NOT handle any order execution.
    
    Usage:
        ws = BinanceWSManager(symbols=["ETH"])
        ws.on_book_update = handle_book
        ws.on_trade = handle_trade
        await ws.connect()
        await ws.run()
    """
    
    # Symbol mapping: internal -> Binance format
    SYMBOL_MAP = {
        "ETH": "ethusdt",
        "BTC": "btcusdt",
        "SOL": "solusdt",
        "ARB": "arbusdt",
        "DOGE": "dogeusdt",
        "AVAX": "avaxusdt",
        "LINK": "linkusdt",
        "MATIC": "maticusdt",
    }
    
    def __init__(
        self,
        symbols: List[str],
        config: Optional[BinanceWSConfig] = None,
    ):
        self.symbols = symbols
        self.config = config or BinanceWSConfig()
        
        # State
        self._state = BinanceWSState.DISCONNECTED
        self._ws = None
        self._reconnect_attempts = 0
        self._last_message_time: float = 0.0
        
        # Event handlers (set by caller)
        self.on_book_update: Optional[Callable[[str, Dict], Awaitable[None]]] = None
        self.on_trade: Optional[Callable[[str, Dict], Awaitable[None]]] = None
        self.on_depth: Optional[Callable[[str, Dict], Awaitable[None]]] = None  # L2 depth
        
        # Tasks
        self._receive_task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None
        self._running = False
    
    @property
    def state(self) -> BinanceWSState:
        return self._state
    
    @property
    def is_connected(self) -> bool:
        return self._state in (BinanceWSState.CONNECTED, BinanceWSState.READY)
    
    @property
    def is_ready(self) -> bool:
        return self._state == BinanceWSState.READY
    
    def _set_state(self, new_state: BinanceWSState) -> None:
        if new_state != self._state:
            old_state = self._state
            self._state = new_state
            log.info(f"[Binance] WS state: {old_state.name} -> {new_state.name}")
    
    def _get_binance_symbol(self, internal_symbol: str) -> str:
        """Map internal symbol to Binance format."""
        return self.SYMBOL_MAP.get(internal_symbol, f"{internal_symbol.lower()}usdt")
    
    def _get_internal_symbol(self, binance_symbol: str) -> Optional[str]:
        """Map Binance symbol back to internal format."""
        binance_lower = binance_symbol.lower()
        for internal, binance in self.SYMBOL_MAP.items():
            if binance == binance_lower:
                return internal
        # Fallback: strip 'usdt' suffix
        if binance_lower.endswith("usdt"):
            return binance_lower[:-4].upper()
        return None
    
    def _build_stream_url(self) -> str:
        """Build combined stream URL for all symbols."""
        streams = []
        for symbol in self.symbols:
            binance_sym = self._get_binance_symbol(symbol)
            streams.append(f"{binance_sym}@bookTicker")
            streams.append(f"{binance_sym}@aggTrade")
            # Add L2 depth stream (depth@20@500ms format)
            depth_stream = f"{binance_sym}@depth{self.config.depth_levels}@{self.config.depth_update_speed}"
            streams.append(depth_stream)
        
        stream_path = "/".join(streams)
        return f"{self.config.base_url}/{stream_path}"
    
    async def connect(self) -> bool:
        """Connect to Binance Futures WebSocket."""
        try:
            import websockets
        except ImportError:
            log.error("[Binance] websockets library not installed")
            return False
        
        self._set_state(BinanceWSState.CONNECTING)
        
        url = self._build_stream_url()
        log.info(f"[Binance] Connecting to {url[:80]}...")
        
        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(
                    url,
                    ping_interval=self.config.ping_interval,
                    ping_timeout=10.0,
                    close_timeout=5.0,
                ),
                timeout=15.0,
            )
            self._set_state(BinanceWSState.CONNECTED)
            self._reconnect_attempts = 0
            log.info(f"[Binance] Connected successfully")
            return True
            
        except Exception as e:
            log.error(f"[Binance] Connection failed: {e}")
            self._set_state(BinanceWSState.ERROR)
            return False
    
    async def run(self) -> None:
        """Run the WebSocket message loop."""
        self._running = True
        
        while self._running:
            if not self.is_connected:
                success = await self.connect()
                if not success:
                    delay = min(
                        self.config.reconnect_delay_initial * (self.config.reconnect_delay_multiplier ** self._reconnect_attempts),
                        self.config.reconnect_delay_max,
                    )
                    self._reconnect_attempts += 1
                    log.info(f"[Binance] Reconnecting in {delay:.1f}s...")
                    await asyncio.sleep(delay)
                    continue
            
            try:
                await self._receive_loop()
            except Exception as e:
                log.warning(f"[Binance] Receive loop error: {e}")
                self._set_state(BinanceWSState.DISCONNECTED)
    
    async def _receive_loop(self) -> None:
        """Process incoming WebSocket messages."""
        # Mark as ready immediately (Binance doesn't need warmup like Hyperliquid)
        self._set_state(BinanceWSState.READY)
        
        while self._running and self._ws:
            try:
                raw_msg = await asyncio.wait_for(self._ws.recv(), timeout=60.0)
                self._last_message_time = time.time()
                
                try:
                    data = json.loads(raw_msg)
                    await self._dispatch_message(data)
                except json.JSONDecodeError:
                    log.debug(f"[Binance] Invalid JSON: {raw_msg[:100]}")
                    
            except asyncio.TimeoutError:
                log.debug("[Binance] No message in 60s, checking connection...")
                continue
            except Exception as e:
                log.warning(f"[Binance] Receive error: {e}")
                break
        
        self._set_state(BinanceWSState.DISCONNECTED)
    
    async def _dispatch_message(self, data: Dict[str, Any]) -> None:
        """Route message to appropriate handler."""
        event_type = data.get("e")
        
        if event_type == "bookTicker":
            await self._handle_book_ticker(data)
        elif event_type == "aggTrade":
            await self._handle_agg_trade(data)
        elif event_type == "depthUpdate":
            await self._handle_depth_update(data)
        # Ignore other event types
    
    async def _handle_book_ticker(self, data: Dict[str, Any]) -> None:
        """
        Handle bookTicker message.
        
        Binance format:
        {
            "e": "bookTicker",
            "u": 123456789,
            "s": "ETHUSDT",
            "b": "2950.00",  # best bid price
            "B": "10.5",     # best bid qty
            "a": "2950.10",  # best ask price
            "A": "8.2",      # best ask qty
            "T": 1672531200000,
            "E": 1672531200001
        }
        """
        if not self.on_book_update:
            return
        
        binance_symbol = data.get("s", "")
        internal_symbol = self._get_internal_symbol(binance_symbol)
        if not internal_symbol or internal_symbol not in self.symbols:
            return
        
        # Normalize to common format
        normalized = {
            "type": "book_ticker",
            "best_bid": float(data.get("b", 0)),
            "best_bid_sz": float(data.get("B", 0)),
            "best_ask": float(data.get("a", 0)),
            "best_ask_sz": float(data.get("A", 0)),
            "timestamp_ms": int(data.get("T", time.time() * 1000)),
        }
        
        await self.on_book_update(internal_symbol, normalized)
    
    async def _handle_agg_trade(self, data: Dict[str, Any]) -> None:
        """
        Handle aggTrade message.
        
        Binance format:
        {
            "e": "aggTrade",
            "E": 1672531200001,
            "s": "ETHUSDT",
            "a": 123456,
            "p": "2950.05",  # price
            "q": "0.5",      # quantity
            "f": 100,
            "l": 105,
            "T": 1672531200000,
            "m": false       # is buyer maker? (true = sell aggressor)
        }
        """
        if not self.on_trade:
            return
        
        binance_symbol = data.get("s", "")
        internal_symbol = self._get_internal_symbol(binance_symbol)
        if not internal_symbol or internal_symbol not in self.symbols:
            return
        
        # Normalize to common format (same as Hyperliquid's trade format)
        # side: "A" = Ask lifted (buy aggressor), "B" = Bid hit (sell aggressor)
        is_buyer_maker = data.get("m", False)
        side = "B" if is_buyer_maker else "A"
        
        normalized = {
            "coin": internal_symbol,
            "side": side,
            "px": data.get("p", "0"),
            "sz": data.get("q", "0"),
            "time": int(data.get("T", time.time() * 1000)),
        }
        
        await self.on_trade(internal_symbol, normalized)
    
    async def _handle_depth_update(self, data: Dict[str, Any]) -> None:
        """
        Handle depthUpdate message (L2 orderbook).
        
        Binance format:
        {
            "e": "depthUpdate",
            "E": 1672531200001,
            "T": 1672531200000,
            "s": "ETHUSDT",
            "U": 123456789,
            "u": 123456790,
            "pu": 123456788,
            "b": [["2950.00", "10.5"], ["2949.90", "0"], ...],  # bids (size=0 means remove)
            "a": [["2950.10", "8.2"], ["2950.20", "0"], ...]   # asks
        }
        
        NOTE: size=0 means REMOVE the level - must be passed to BinanceLocalBook!
        """
        if not self.on_depth:
            return
        
        binance_symbol = data.get("s", "")
        internal_symbol = self._get_internal_symbol(binance_symbol)
        if not internal_symbol or internal_symbol not in self.symbols:
            return
        
        # Parse bids and asks to (price, size) tuples
        # IMPORTANT: Include size=0 levels - they signal level removal!
        bids = []
        for level in data.get("b", []):
            if len(level) >= 2:
                price = float(level[0])
                size = float(level[1])
                bids.append((price, size))  # Include all, even size=0
        
        asks = []
        for level in data.get("a", []):
            if len(level) >= 2:
                price = float(level[0])
                size = float(level[1])
                asks.append((price, size))  # Include all, even size=0
        
        # Sort: bids descending by price, asks ascending
        bids.sort(key=lambda x: x[0], reverse=True)
        asks.sort(key=lambda x: x[0])
        
        normalized = {
            "type": "depth",
            "bids": bids,
            "asks": asks,
            "timestamp_ms": int(data.get("T", time.time() * 1000)),
        }
        
        await self.on_depth(internal_symbol, normalized)
    
    async def close(self) -> None:
        """Close WebSocket connection."""
        self._running = False
        
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        
        self._set_state(BinanceWSState.DISCONNECTED)
        log.info("[Binance] WebSocket closed")
