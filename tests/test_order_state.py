# tests/test_order_state.py
"""
Unit tests for order state machine.
"""
import pytest
from hyperbot.execution.order_manager import OrderManager, Order, OrderState, OrderIntent, TERMINAL_STATES


class TestOrderState:
    """Tests for order state machine."""
    
    def test_terminal_states(self):
        """Verify terminal states are correctly defined."""
        assert OrderState.FILLED in TERMINAL_STATES
        assert OrderState.CANCELED in TERMINAL_STATES
        assert OrderState.REJECTED in TERMINAL_STATES
        assert OrderState.EXPIRED in TERMINAL_STATES
        
        assert OrderState.NEW not in TERMINAL_STATES
        assert OrderState.SENT not in TERMINAL_STATES
        assert OrderState.ACK not in TERMINAL_STATES
        assert OrderState.PARTIAL not in TERMINAL_STATES
    
    def test_create_order(self):
        """Test order creation."""
        mgr = OrderManager(bot_id="test")
        order = mgr.create_order(
            symbol="ETH",
            side="buy",
            qty=0.1,
            intent=OrderIntent.ENTRY,
        )
        
        assert order is not None
        assert order.state == OrderState.NEW
        assert order.symbol == "ETH"
        assert not order.is_terminal
    
    def test_idempotency(self):
        """Test duplicate order prevention."""
        mgr = OrderManager(bot_id="test")
        order1 = mgr.create_order(
            symbol="ETH",
            side="buy",
            qty=0.1,
            intent=OrderIntent.ENTRY,
        )
        mgr.mark_sent(order1)
        
        # Try to create another order with same params in same ms
        # The counter increment should prevent exact duplicate
        order2 = mgr.create_order(
            symbol="ETH",
            side="buy",
            qty=0.1,
            intent=OrderIntent.ENTRY,
        )
        
        # Should get a different order (different counter)
        assert order2 is not None
        assert order2.client_id != order1.client_id
    
    def test_state_transition_sent(self):
        """Test NEW -> SENT transition."""
        mgr = OrderManager(bot_id="test")
        order = mgr.create_order("ETH", "buy", 0.1, OrderIntent.ENTRY)
        assert order.state == OrderState.NEW
        
        mgr.mark_sent(order)
        assert order.state == OrderState.SENT
        assert order.sent_ms > 0
    
    def test_state_transition_ack(self):
        """Test SENT -> ACK transition (lock should NOT release)."""
        mgr = OrderManager(bot_id="test")
        order = mgr.create_order("ETH", "buy", 0.1, OrderIntent.ENTRY)
        mgr.mark_sent(order)
        
        mgr.on_ack(order.client_id, "exch_123")
        assert order.state == OrderState.ACK
        assert order.exchange_id == "exch_123"
        
        # Order is NOT terminal - still inflight
        assert not order.is_terminal
        assert mgr.has_inflight_entry("ETH")
    
    def test_state_transition_fill(self):
        """Test full fill -> FILLED (terminal, lock releases)."""
        terminal_called = [False]
        
        def on_terminal(o):
            terminal_called[0] = True
        
        mgr = OrderManager(bot_id="test", on_terminal=on_terminal)
        order = mgr.create_order("ETH", "buy", 0.1, OrderIntent.ENTRY)
        mgr.mark_sent(order)
        mgr.on_ack(order.client_id, "exch_123")
        
        # Full fill
        mgr.on_fill(client_id=order.client_id, fill_qty=0.1, fill_price=1000.0)
        
        assert order.state == OrderState.FILLED
        assert order.is_terminal
        assert order.filled_qty == 0.1
        assert order.avg_fill_price == 1000.0
        assert terminal_called[0]  # Callback was invoked
        
        # No more inflight
        assert not mgr.has_inflight_entry("ETH")
    
    def test_state_transition_partial(self):
        """Test partial fill -> PARTIAL (lock should NOT release)."""
        mgr = OrderManager(bot_id="test")
        order = mgr.create_order("ETH", "buy", 0.1, OrderIntent.ENTRY)
        mgr.mark_sent(order)
        mgr.on_ack(order.client_id, "exch_123")
        
        # Partial fill
        mgr.on_fill(client_id=order.client_id, fill_qty=0.05, fill_price=1000.0)
        
        assert order.state == OrderState.PARTIAL
        assert not order.is_terminal  # Still inflight!
        assert mgr.has_inflight_entry("ETH")
        
        # Complete fill
        mgr.on_fill(client_id=order.client_id, fill_qty=0.05, fill_price=1001.0)
        
        assert order.state == OrderState.FILLED
        assert order.is_terminal
        assert order.filled_qty == 0.1
        # VWAP: (0.05*1000 + 0.05*1001) / 0.1 = 1000.5
        assert abs(order.avg_fill_price - 1000.5) < 0.01
    
    def test_state_transition_reject(self):
        """Test reject -> REJECTED (terminal)."""
        mgr = OrderManager(bot_id="test")
        order = mgr.create_order("ETH", "buy", 0.1, OrderIntent.ENTRY)
        mgr.mark_sent(order)
        
        mgr.on_reject(order.client_id, "INSUFFICIENT_MARGIN")
        
        assert order.state == OrderState.REJECTED
        assert order.is_terminal
        assert order.reject_reason == "INSUFFICIENT_MARGIN"
    
    def test_state_transition_cancel(self):
        """Test cancel -> CANCELED (terminal)."""
        mgr = OrderManager(bot_id="test")
        order = mgr.create_order("ETH", "buy", 0.1, OrderIntent.ENTRY)
        mgr.mark_sent(order)
        mgr.on_ack(order.client_id, "exch_123")
        
        mgr.on_cancel(client_id=order.client_id)
        
        assert order.state == OrderState.CANCELED
        assert order.is_terminal
    
    def test_stuck_order_detection(self):
        """Test stuck order watchdog."""
        import time
        mgr = OrderManager(bot_id="test", ack_timeout_ms=100)
        order = mgr.create_order("ETH", "buy", 0.1, OrderIntent.ENTRY)
        mgr.mark_sent(order)
        
        # Simulate time passing
        order.last_update_ms = int(time.time() * 1000) - 500
        
        stuck = mgr.get_stuck_orders()
        assert len(stuck) == 1
        assert stuck[0].client_id == order.client_id
    
    def test_entry_vs_exit_inflight(self):
        """Test separate tracking of entry/exit inflight."""
        mgr = OrderManager(bot_id="test")
        
        entry = mgr.create_order("ETH", "buy", 0.1, OrderIntent.ENTRY)
        mgr.mark_sent(entry)
        
        exit = mgr.create_order("ETH", "sell", 0.1, OrderIntent.EXIT_TPSL)
        mgr.mark_sent(exit)
        
        assert mgr.has_inflight_entry("ETH")
        assert mgr.has_inflight_exit("ETH")
        
        # Complete entry
        mgr.on_fill(client_id=entry.client_id, fill_qty=0.1, fill_price=1000.0)
        
        assert not mgr.has_inflight_entry("ETH")
        assert mgr.has_inflight_exit("ETH")  # Exit still pending


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
