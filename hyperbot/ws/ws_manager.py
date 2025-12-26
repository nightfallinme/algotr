# hyperbot/ws/ws_manager.py
"""
WebSocket Manager for Hyperliquid real-time data.

Features:
- Auto-reconnect with exponential backoff
- Circuit breaker pattern
- Trading pause on reconnect
- Unified event dispatch
"""
import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, Callable, List, Awaitable
from enum import Enum, auto

from ..logger import get_logger

log = get_logger(__name__)

# Hyperliquid WebSocket URL
HYPERLIQUID_WS_URL = "wss://api.hyperliquid.xyz/ws"


class WSState(Enum):
    """WebSocket connection states."""
    DISCONNECTED = auto()
    CONNECTING = auto()
    CONNECTED = auto()
    WARMING_UP = auto()
    READY = auto()
    RECONNECTING = auto()
    ERROR = auto()


@dataclass
class WSConfig:
    """WebSocket configuration."""
    url: str = HYPERLIQUID_WS_URL
    reconnect_delay_initial: float = 1.0
    reconnect_delay_max: float = 30.0
    reconnect_delay_multiplier: float = 2.0
    ping_interval: float = 30.0
    ping_timeout: float = 10.0
    max_reconnect_attempts: int = 100


class WSManager:
    """
    Manages WebSocket connection to Hyperliquid.
    
    Usage:
        ws = WSManager(symbols=["ETH", "BTC"])
        ws.on_book_update = handle_book
        ws.on_trade = handle_trade
        await ws.connect()
        await ws.run()
    """
    
    def __init__(
        self,
        symbols: List[str],
        config: Optional[WSConfig] = None,
        on_state_change: Optional[Callable[[WSState, WSState], None]] = None,
    ):
        self.symbols = symbols
        self.config = config or WSConfig()
        self._on_state_change = on_state_change
        
        # State
        self._state = WSState.DISCONNECTED
        self._ws = None
        self._reconnect_attempts = 0
        self._last_message_time: float = 0.0
        
        # Event handlers (set by caller)
        self.on_book_update: Optional[Callable[[str, Dict], Awaitable[None]]] = None
        self.on_book_snapshot: Optional[Callable[[str, Dict], Awaitable[None]]] = None
        self.on_trade: Optional[Callable[[str, Dict], Awaitable[None]]] = None
        self.on_user_fill: Optional[Callable[[Dict], Awaitable[None]]] = None
        
        # Tasks
        self._receive_task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None
        self._running = False
    
    @property
    def state(self) -> WSState:
        return self._state
    
    @property
    def is_connected(self) -> bool:
        return self._state in (WSState.CONNECTED, WSState.WARMING_UP, WSState.READY)
    
    @property
    def is_ready(self) -> bool:
        return self._state == WSState.READY
    
    def _set_state(self, new_state: WSState) -> None:
        """Update state and notify callback."""
        if new_state != self._state:
            old_state = self._state
            self._state = new_state
            log.info(f"WS state: {old_state.name} -> {new_state.name}")
            if self._on_state_change:
                try:
                    self._on_state_change(old_state, new_state)
                except Exception as e:
                    log.error(f"State change callback error: {e}")
    
    async def connect(self) -> bool:
        """
        Establish WebSocket connection.
        
        Returns: True if connected successfully
        """
        try:
            import websockets
        except ImportError:
            log.error("websockets library not installed. Run: pip install websockets")
            return False
        
        self._set_state(WSState.CONNECTING)
        
        try:
            self._ws = await websockets.connect(
                self.config.url,
                ping_interval=self.config.ping_interval,
                ping_timeout=self.config.ping_timeout,
            )
            
            # Subscribe to channels
            await self._subscribe()
            
            self._set_state(WSState.CONNECTED)
            self._reconnect_attempts = 0
            log.info(f"Connected to {self.config.url}")
            return True
            
        except Exception as e:
            log.error(f"Connection failed: {e}")
            self._set_state(WSState.ERROR)
            return False
    
    async def _subscribe(self) -> None:
        """Subscribe to required channels."""
        if not self._ws:
            return
        
        # Subscribe to L2 book for each symbol
        for symbol in self.symbols:
            sub_msg = {
                "method": "subscribe",
                "subscription": {
                    "type": "l2Book",
                    "coin": symbol,
                }
            }
            await self._ws.send(json.dumps(sub_msg))
            log.debug(f"Subscribed to l2Book:{symbol}")
        
        # Subscribe to trades for each symbol
        for symbol in self.symbols:
            sub_msg = {
                "method": "subscribe",
                "subscription": {
                    "type": "trades",
                    "coin": symbol,
                }
            }
            await self._ws.send(json.dumps(sub_msg))
            log.debug(f"Subscribed to trades:{symbol}")
    
    async def run(self) -> None:
        """
        Main run loop with auto-reconnect.
        
        Blocks until stop() is called.
        """
        self._running = True
        
        while self._running:
            try:
                if not self.is_connected:
                    success = await self.connect()
                    if not success:
                        await self._handle_reconnect()
                        continue
                
                # Start receive loop
                await self._receive_loop()
                
            except Exception as e:
                log.error(f"WS run error: {e}")
                await self._handle_reconnect()
    
    async def _receive_loop(self) -> None:
        """Process incoming messages."""
        if not self._ws:
            return
        
        try:
            async for message in self._ws:
                self._last_message_time = time.time()
                await self._handle_message(message)
                
        except Exception as e:
            log.warning(f"Receive loop error: {e}")
            self._set_state(WSState.DISCONNECTED)
    
    async def _handle_message(self, raw: str) -> None:
        """Parse and dispatch message to handlers."""
        try:
            data = json.loads(raw)
            
            channel = data.get("channel")
            msg_data = data.get("data", {})
            
            if channel == "l2Book":
                coin = msg_data.get("coin", "")
                if self.on_book_snapshot and "levels" in msg_data:
                    await self.on_book_snapshot(coin, msg_data)
                elif self.on_book_update:
                    await self.on_book_update(coin, msg_data)
                    
            elif channel == "trades":
                trades = msg_data if isinstance(msg_data, list) else [msg_data]
                for trade in trades:
                    coin = trade.get("coin", "")
                    if self.on_trade:
                        await self.on_trade(coin, trade)
                        
            elif channel == "user":
                # User fills/orders
                if self.on_user_fill:
                    await self.on_user_fill(msg_data)
            
            # Check/set ready state after receiving data
            if self._state == WSState.CONNECTED:
                self._set_state(WSState.WARMING_UP)
                
        except json.JSONDecodeError as e:
            log.debug(f"Invalid JSON: {e}")
        except Exception as e:
            log.error(f"Message handler error: {e}")
    
    async def _handle_reconnect(self) -> None:
        """Handle reconnection with exponential backoff."""
        self._set_state(WSState.RECONNECTING)
        self._reconnect_attempts += 1
        
        if self._reconnect_attempts > self.config.max_reconnect_attempts:
            log.error("Max reconnect attempts exceeded")
            self._set_state(WSState.ERROR)
            self._running = False
            return
        
        delay = min(
            self.config.reconnect_delay_initial * (
                self.config.reconnect_delay_multiplier ** (self._reconnect_attempts - 1)
            ),
            self.config.reconnect_delay_max,
        )
        
        log.info(f"Reconnecting in {delay:.1f}s (attempt {self._reconnect_attempts})")
        await asyncio.sleep(delay)
    
    def set_ready(self) -> None:
        """Mark connection as ready (called after warm-up complete)."""
        if self._state == WSState.WARMING_UP:
            self._set_state(WSState.READY)
    
    async def stop(self) -> None:
        """Stop the manager and close connection."""
        self._running = False
        
        if self._ws:
            await self._ws.close()
            self._ws = None
        
        self._set_state(WSState.DISCONNECTED)
        log.info("WS manager stopped")
    
    def get_stats(self) -> Dict[str, Any]:
        """Get connection statistics."""
        return {
            "state": self._state.name,
            "reconnect_attempts": self._reconnect_attempts,
            "last_message_age_sec": time.time() - self._last_message_time if self._last_message_time else None,
        }
