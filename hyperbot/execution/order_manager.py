# hyperbot/execution/order_manager.py
"""
Order Manager with state machine and idempotency.

Features:
- Deterministic clientOrderId generation
- Order state machine with terminal-only lock release
- Inflight tracking and watchdog support
- Fill handling
"""
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, Callable, List
from enum import Enum, auto
from datetime import datetime
import time
import hashlib

from ..ofi.coin_state import DecisionSnapshot
from ..logger import get_logger

log = get_logger(__name__)


class OrderState(Enum):
    """Order lifecycle states."""
    NEW = auto()        # Created locally
    SENT = auto()       # Sent to exchange
    ACK = auto()        # Acknowledged by exchange
    PARTIAL = auto()    # Partially filled
    FILLED = auto()     # Fully filled (terminal)
    CANCELED = auto()   # Canceled (terminal)
    REJECTED = auto()   # Rejected by exchange (terminal)
    EXPIRED = auto()    # Expired/timeout (terminal)


class OrderIntent(Enum):
    """Order intent type."""
    ENTRY = "entry"
    EXIT_TPSL = "exit_tpsl"
    EXIT_RISK = "exit_risk"
    EXIT_OFI_FLIP = "exit_ofi_flip"
    EXIT_TIME = "exit_time"
    CLOSE = "close"


# Terminal states - locks should only be released here
TERMINAL_STATES = {OrderState.FILLED, OrderState.CANCELED, OrderState.REJECTED, OrderState.EXPIRED}


@dataclass
class Order:
    """Order tracking object."""
    client_id: str
    exchange_id: Optional[str] = None
    
    symbol: str = ""
    intent: OrderIntent = OrderIntent.ENTRY
    side: str = ""  # "buy" or "sell"
    requested_qty: float = 0.0
    limit_price: Optional[float] = None
    
    # Fill tracking
    filled_qty: float = 0.0
    avg_fill_price: float = 0.0
    fees: float = 0.0
    
    # State
    state: OrderState = OrderState.NEW
    created_ms: int = 0
    sent_ms: int = 0
    ack_ms: int = 0
    last_update_ms: int = 0
    
    # Decision snapshot for slippage tracking
    decision_snapshot: Optional[DecisionSnapshot] = None
    
    # Error info
    reject_reason: str = ""
    
    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES
    
    @property
    def is_entry(self) -> bool:
        return self.intent == OrderIntent.ENTRY
    
    @property
    def is_exit(self) -> bool:
        return self.intent != OrderIntent.ENTRY
    
    @property
    def ack_latency_ms(self) -> Optional[int]:
        """Latency from send to ack."""
        if self.sent_ms and self.ack_ms:
            return self.ack_ms - self.sent_ms
        return None
    
    @property
    def slippage(self) -> Optional[float]:
        """Slippage vs decision price (signed)."""
        if not self.decision_snapshot or self.avg_fill_price <= 0:
            return None
        
        decision_price = self.decision_snapshot.microprice
        if decision_price <= 0:
            return None
        
        if self.side == "buy":
            # For buys, positive slippage = paid more than expected
            return self.avg_fill_price - decision_price
        else:
            # For sells, positive slippage = received less than expected
            return decision_price - self.avg_fill_price


class OrderManager:
    """
    Manages order lifecycle with safety guarantees.
    
    Key principles:
    - Deterministic clientOrderId prevents duplicate sends
    - Locks only release on terminal states
    - Watchdog detects stuck orders
    """
    
    def __init__(
        self,
        bot_id: str = "ofi",
        ack_timeout_ms: int = 2000,
        on_fill: Optional[Callable[['Order', float, float], None]] = None,
        on_terminal: Optional[Callable[['Order'], None]] = None,
    ):
        self.bot_id = bot_id
        self.ack_timeout_ms = ack_timeout_ms
        self._on_fill = on_fill
        self._on_terminal = on_terminal
        
        # Order registry
        self._orders: Dict[str, Order] = {}  # client_id -> Order
        self._exchange_id_map: Dict[str, str] = {}  # exchange_id -> client_id
        
        # Idempotency tracking
        self._sent_ids: set = set()  # Already sent client_ids
        
        # Counter for unique IDs
        self._order_counter: int = 0
    
    def generate_client_id(self, symbol: str, intent: str, timestamp_ms: int) -> str:
        """
        Generate deterministic clientOrderId.
        
        Format: {bot_id}:{symbol}:{intent}:{ts}:{counter}
        """
        self._order_counter += 1
        raw = f"{self.bot_id}:{symbol}:{intent}:{timestamp_ms}:{self._order_counter}"
        # Hash to limit length if needed
        short_hash = hashlib.md5(raw.encode()).hexdigest()[:8]
        return f"{self.bot_id}_{symbol}_{short_hash}"
    
    def create_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        intent: OrderIntent,
        limit_price: Optional[float] = None,
        decision_snapshot: Optional[DecisionSnapshot] = None,
    ) -> Optional[Order]:
        """
        Create a new order.
        
        Returns None if order with same signature already exists (idempotency).
        """
        now_ms = int(time.time() * 1000)
        client_id = self.generate_client_id(symbol, intent.value, now_ms)
        
        # Idempotency check
        if client_id in self._sent_ids:
            log.warning(f"Duplicate order blocked: {client_id}")
            return None
        
        order = Order(
            client_id=client_id,
            symbol=symbol,
            intent=intent,
            side=side,
            requested_qty=qty,
            limit_price=limit_price,
            state=OrderState.NEW,
            created_ms=now_ms,
            decision_snapshot=decision_snapshot,
        )
        
        self._orders[client_id] = order
        return order
    
    def mark_sent(self, order: Order) -> None:
        """Mark order as sent to exchange."""
        order.state = OrderState.SENT
        order.sent_ms = int(time.time() * 1000)
        order.last_update_ms = order.sent_ms
        self._sent_ids.add(order.client_id)
        log.debug(f"Order sent: {order.client_id}")
    
    def on_ack(self, client_id: str, exchange_id: str) -> Optional[Order]:
        """
        Handle order acknowledgement.
        
        NOTE: Does NOT release locks - order is still inflight.
        """
        order = self._orders.get(client_id)
        if not order:
            log.warning(f"ACK for unknown order: {client_id}")
            return None
        
        now_ms = int(time.time() * 1000)
        order.exchange_id = exchange_id
        order.state = OrderState.ACK
        order.ack_ms = now_ms
        order.last_update_ms = now_ms
        
        self._exchange_id_map[exchange_id] = client_id
        
        log.debug(f"Order ACK: {client_id} -> {exchange_id} (latency: {order.ack_latency_ms}ms)")
        return order
    
    def on_fill(
        self,
        client_id: Optional[str] = None,
        exchange_id: Optional[str] = None,
        fill_qty: float = 0.0,
        fill_price: float = 0.0,
        fee: float = 0.0,
    ) -> Optional[Order]:
        """
        Handle fill event.
        
        Updates position and releases lock on full fill.
        """
        order = self._lookup(client_id, exchange_id)
        if not order:
            log.warning(f"Fill for unknown order: client={client_id} exch={exchange_id}")
            return None
        
        now_ms = int(time.time() * 1000)
        
        # Update fill tracking with VWAP
        old_total = order.filled_qty * order.avg_fill_price
        order.filled_qty += fill_qty
        order.fees += fee
        
        if order.filled_qty > 0:
            order.avg_fill_price = (old_total + fill_qty * fill_price) / order.filled_qty
        
        order.last_update_ms = now_ms
        
        # Call fill callback
        if self._on_fill:
            try:
                self._on_fill(order, fill_qty, fill_price)
            except Exception as e:
                log.error(f"Fill callback error: {e}")
        
        # Check if full fill
        if order.filled_qty >= order.requested_qty - 1e-9:
            order.state = OrderState.FILLED
            log.info(f"Order FILLED: {order.client_id} @ {order.avg_fill_price:.4f}")
            self._handle_terminal(order)
        else:
            order.state = OrderState.PARTIAL
            log.info(f"Order PARTIAL: {order.client_id} {order.filled_qty}/{order.requested_qty}")
        
        return order
    
    def on_reject(self, client_id: str, reason: str = "") -> Optional[Order]:
        """Handle order rejection."""
        order = self._orders.get(client_id)
        if not order:
            return None
        
        order.state = OrderState.REJECTED
        order.reject_reason = reason
        order.last_update_ms = int(time.time() * 1000)
        
        log.warning(f"Order REJECTED: {client_id} - {reason}")
        self._handle_terminal(order)
        return order
    
    def on_cancel(
        self,
        client_id: Optional[str] = None,
        exchange_id: Optional[str] = None,
    ) -> Optional[Order]:
        """Handle order cancellation."""
        order = self._lookup(client_id, exchange_id)
        if not order:
            return None
        
        order.state = OrderState.CANCELED
        order.last_update_ms = int(time.time() * 1000)
        
        log.info(f"Order CANCELED: {order.client_id} (filled: {order.filled_qty})")
        self._handle_terminal(order)
        return order
    
    def on_timeout(self, client_id: str) -> Optional[Order]:
        """Handle order timeout (watchdog)."""
        order = self._orders.get(client_id)
        if not order or order.is_terminal:
            return None
        
        order.state = OrderState.EXPIRED
        order.reject_reason = "TIMEOUT"
        order.last_update_ms = int(time.time() * 1000)
        
        log.warning(f"Order TIMEOUT: {client_id}")
        self._handle_terminal(order)
        return order
    
    def _lookup(self, client_id: Optional[str], exchange_id: Optional[str]) -> Optional[Order]:
        """Lookup order by client_id or exchange_id."""
        if client_id and client_id in self._orders:
            return self._orders[client_id]
        if exchange_id and exchange_id in self._exchange_id_map:
            cid = self._exchange_id_map[exchange_id]
            return self._orders.get(cid)
        return None
    
    def _handle_terminal(self, order: Order) -> None:
        """Handle terminal state - release locks and callbacks."""
        if self._on_terminal:
            try:
                self._on_terminal(order)
            except Exception as e:
                log.error(f"Terminal callback error: {e}")
    
    def get_inflight_orders(self, symbol: Optional[str] = None) -> List[Order]:
        """Get all non-terminal orders."""
        orders = [o for o in self._orders.values() if not o.is_terminal]
        if symbol:
            orders = [o for o in orders if o.symbol == symbol]
        return orders
    
    def get_stuck_orders(self, now_ms: Optional[int] = None) -> List[Order]:
        """Get orders that may be stuck (no update past timeout)."""
        now = now_ms or int(time.time() * 1000)
        stuck = []
        
        for order in self._orders.values():
            if order.is_terminal:
                continue
            if order.state in (OrderState.SENT, OrderState.ACK):
                age = now - order.last_update_ms
                if age > self.ack_timeout_ms:
                    stuck.append(order)
        
        return stuck
    
    def has_inflight_entry(self, symbol: str) -> bool:
        """Check if there's an inflight entry order."""
        return any(
            o.symbol == symbol and o.is_entry and not o.is_terminal
            for o in self._orders.values()
        )
    
    def has_inflight_exit(self, symbol: str) -> bool:
        """Check if there's an inflight exit order."""
        return any(
            o.symbol == symbol and o.is_exit and not o.is_terminal
            for o in self._orders.values()
        )
    
    def cleanup_old(self, max_age_ms: int = 3600000) -> int:
        """Remove old terminal orders from registry."""
        now = int(time.time() * 1000)
        to_remove = []
        
        for cid, order in self._orders.items():
            if order.is_terminal:
                age = now - order.last_update_ms
                if age > max_age_ms:
                    to_remove.append(cid)
        
        for cid in to_remove:
            order = self._orders.pop(cid, None)
            if order and order.exchange_id:
                self._exchange_id_map.pop(order.exchange_id, None)
        
        return len(to_remove)
