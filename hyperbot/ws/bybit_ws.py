# hyperbot/ws/bybit_ws.py
"""
Bybit WebSocket manager for real-time order book and trade data.

Uses Bybit V5 WebSocket API.
Mainnet: wss://stream.bybit.com/v5/public/linear
Testnet: wss://stream-testnet.bybit.com/v5/public/linear
"""
import asyncio
import json
import time
from typing import Optional, Dict, Any, Callable, List
from enum import Enum, auto
from dataclasses import dataclass

from ..logger import get_logger

log = get_logger(__name__)

# Try to import websockets
try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    log.warning("websockets not installed")


class BybitWSState(Enum):
    """WebSocket connection state."""
    DISCONNECTED = auto()
    CONNECTING = auto()
    CONNECTED = auto()
    READY = auto()


@dataclass
class BybitTick:
    """Parsed orderbook tick from Bybit."""
    symbol: str
    best_bid: float
    best_ask: float
    bid_size: float
    ask_size: float
    timestamp_ms: int


@dataclass
class BybitTrade:
    """Parsed trade from Bybit."""
    symbol: str
    side: str  # "Buy" or "Sell"
    price: float
    size: float
    timestamp_ms: int


class BybitWSManager:
    """
    Bybit WebSocket manager for linear perpetuals.
    
    Subscribes to:
    - orderbook.1.{symbol} - Level 1 order book (best bid/ask)
    - publicTrade.{symbol} - Public trades
    
    Provides same interface as HyperliquidExchange WSManager.
    """
    
    MAINNET_URL = "wss://stream.bybit.com/v5/public/linear"
    TESTNET_URL = "wss://stream-testnet.bybit.com/v5/public/linear"
    
    def __init__(
        self,
        symbols: List[str],
        testnet: bool = False,
        on_book_update: Optional[Callable[[str, Dict], None]] = None,
        on_trade: Optional[Callable[[str, Dict], None]] = None,
        on_state_change: Optional[Callable[[BybitWSState, BybitWSState], None]] = None,
    ):
        self.symbols = symbols
        self.testnet = testnet
        self._on_book_update = on_book_update
        self._on_trade = on_trade
        self._on_state_change = on_state_change
        
        # Connection
        self._ws = None
        self._state = BybitWSState.DISCONNECTED
        self._running = False
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 30.0
        
        # Heartbeat
        self._last_ping_ms = 0
        self._ping_interval_ms = 20000  # 20 seconds
        
        # Symbol mapping
        self._symbol_suffix = "USDT"
    
    def _to_bybit_symbol(self, symbol: str) -> str:
        """Convert to Bybit symbol format."""
        if not symbol.endswith("USDT"):
            return f"{symbol}{self._symbol_suffix}"
        return symbol
    
    def _from_bybit_symbol(self, symbol: str) -> str:
        """Convert from Bybit symbol format."""
        if symbol.endswith("USDT"):
            return symbol[:-4]
        return symbol
    
    @property
    def state(self) -> BybitWSState:
        return self._state
    
    @property
    def is_ready(self) -> bool:
        return self._state == BybitWSState.READY
    
    def _set_state(self, new_state: BybitWSState) -> None:
        """Update state and notify."""
        if new_state != self._state:
            old_state = self._state
            self._state = new_state
            log.info(f"[Bybit] WS state: {old_state.name} -> {new_state.name}")
            if self._on_state_change:
                self._on_state_change(old_state, new_state)
    
    async def start(self) -> None:
        """Start WebSocket connection."""
        if not WEBSOCKETS_AVAILABLE:
            log.error("websockets not available")
            return
        
        self._running = True
        await self._connect_loop()
    
    async def stop(self) -> None:
        """Stop WebSocket connection."""
        self._running = False
        if self._ws:
            await self._ws.close()
        self._set_state(BybitWSState.DISCONNECTED)
        log.info("[Bybit] WebSocket closed")
    
    async def _connect_loop(self) -> None:
        """Connection loop with auto-reconnect."""
        while self._running:
            try:
                await self._connect()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"[Bybit] Connection error: {e}")
                self._set_state(BybitWSState.DISCONNECTED)
                
                if self._running:
                    log.info(f"[Bybit] Reconnecting in {self._reconnect_delay}s...")
                    await asyncio.sleep(self._reconnect_delay)
                    self._reconnect_delay = min(
                        self._reconnect_delay * 2,
                        self._max_reconnect_delay
                    )
    
    async def _connect(self) -> None:
        """Establish WebSocket connection."""
        url = self.TESTNET_URL if self.testnet else self.MAINNET_URL
        
        self._set_state(BybitWSState.CONNECTING)
        log.info(f"[Bybit] Connecting to {url}...")
        
        async with websockets.connect(url) as ws:
            self._ws = ws
            self._set_state(BybitWSState.CONNECTED)
            self._reconnect_delay = 1.0  # Reset delay on successful connect
            
            # Subscribe to channels
            await self._subscribe()
            
            self._set_state(BybitWSState.READY)
            log.info("[Bybit] Connected and subscribed")
            
            # Message loop
            await self._message_loop()
    
    async def _subscribe(self) -> None:
        """Subscribe to orderbook and trade channels."""
        if not self._ws:
            return
        
        # Build subscription list
        args = []
        for symbol in self.symbols:
            bybit_symbol = self._to_bybit_symbol(symbol)
            args.append(f"orderbook.1.{bybit_symbol}")  # Level 1 (best bid/ask)
            args.append(f"publicTrade.{bybit_symbol}")
        
        subscribe_msg = {
            "op": "subscribe",
            "args": args,
        }
        
        await self._ws.send(json.dumps(subscribe_msg))
        log.info(f"[Bybit] Subscribed to: {args}")
    
    async def _message_loop(self) -> None:
        """Process incoming messages."""
        while self._running and self._ws:
            try:
                # Check if we need to ping
                now_ms = int(time.time() * 1000)
                if now_ms - self._last_ping_ms > self._ping_interval_ms:
                    await self._send_ping()
                    self._last_ping_ms = now_ms
                
                # Receive message with timeout
                try:
                    raw = await asyncio.wait_for(self._ws.recv(), timeout=30.0)
                    await self._handle_message(raw)
                except asyncio.TimeoutError:
                    # Send ping on timeout
                    await self._send_ping()
                    self._last_ping_ms = now_ms
                    
            except websockets.ConnectionClosed:
                log.warning("[Bybit] Connection closed")
                break
            except Exception as e:
                log.error(f"[Bybit] Message error: {e}")
    
    async def _send_ping(self) -> None:
        """Send heartbeat ping."""
        if self._ws:
            try:
                ping_msg = {"op": "ping"}
                await self._ws.send(json.dumps(ping_msg))
            except Exception as e:
                log.debug(f"[Bybit] Ping error: {e}")
    
    async def _handle_message(self, raw: str) -> None:
        """Handle incoming WebSocket message."""
        try:
            msg = json.loads(raw)
            
            # Handle pong
            if msg.get("op") == "pong":
                return
            
            # Handle subscription response
            if msg.get("op") == "subscribe":
                if msg.get("success"):
                    log.debug(f"[Bybit] Subscription confirmed")
                else:
                    log.warning(f"[Bybit] Subscription failed: {msg}")
                return
            
            # Handle data messages
            topic = msg.get("topic", "")
            data = msg.get("data")
            
            if not topic or not data:
                return
            
            # Parse topic: "orderbook.1.ETHUSDT" or "publicTrade.ETHUSDT"
            parts = topic.split(".")
            
            if parts[0] == "orderbook" and len(parts) >= 3:
                bybit_symbol = parts[2]
                symbol = self._from_bybit_symbol(bybit_symbol)
                await self._handle_orderbook(symbol, data)
                
            elif parts[0] == "publicTrade" and len(parts) >= 2:
                bybit_symbol = parts[1]
                symbol = self._from_bybit_symbol(bybit_symbol)
                await self._handle_trades(symbol, data)
                
        except json.JSONDecodeError as e:
            log.debug(f"[Bybit] JSON decode error: {e}")
        except Exception as e:
            log.error(f"[Bybit] Message handler error: {e}")
    
    async def _handle_orderbook(self, symbol: str, data: Dict) -> None:
        """Handle orderbook update."""
        try:
            # Bybit orderbook.1 format:
            # {"s": "ETHUSDT", "b": [["2941.50", "10.5"]], "a": [["2941.60", "8.2"]], "u": 123, "ts": ...}
            # Note: b/a contain arrays of [price_str, size_str]
            
            bids = data.get("b", [])
            asks = data.get("a", [])
            
            if not bids or not asks:
                return
            
            # Parse best bid/ask directly (Bybit sends as strings)
            best_bid = float(bids[0][0])
            bid_size = float(bids[0][1])
            best_ask = float(asks[0][0])
            ask_size = float(asks[0][1])
            
            # Simple format for orchestrator - direct values
            book_data = {
                "coin": symbol,
                "best_bid": best_bid,
                "bid_size": bid_size,
                "best_ask": best_ask,
                "ask_size": ask_size,
                "ts": data.get("ts", int(time.time() * 1000)),
            }
            
            if self._on_book_update:
                self._on_book_update(symbol, book_data)
                
        except Exception as e:
            log.error(f"[Bybit] Orderbook parse error: {e}")
    
    async def _handle_trades(self, symbol: str, data: List[Dict]) -> None:
        """Handle trade updates."""
        try:
            # Bybit publicTrade format:
            # [{"T": 1234567890123, "s": "ETHUSDT", "S": "Buy", "v": "1.5", "p": "2941.55", "L": "ZeroMinusTick", "i": "123"}]
            
            for trade in data:
                trade_data = {
                    "coin": symbol,
                    "side": "A" if trade.get("S") == "Buy" else "B",  # Aggressor side
                    "px": trade.get("p"),
                    "sz": trade.get("v"),
                    "time": trade.get("T", int(time.time() * 1000)),
                    "tid": trade.get("i"),
                }
                
                if self._on_trade:
                    self._on_trade(symbol, trade_data)
                    
        except Exception as e:
            log.error(f"[Bybit] Trade parse error: {e}")
