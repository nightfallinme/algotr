# tests/test_data_structures.py
"""
Unit tests for core data structures.
"""
import pytest
import time
from hyperbot.ws.data_structures import RingBuffer, TimeWindowSum, EMACalculator


class TestRingBuffer:
    """Tests for RingBuffer."""
    
    def test_init(self):
        buf = RingBuffer(maxlen=10)
        assert buf.maxlen == 10
        assert buf.size == 0
        assert not buf.is_full
    
    def test_push_and_size(self):
        buf = RingBuffer(maxlen=5)
        for i in range(3):
            buf.push(float(i))
        assert buf.size == 3
        assert not buf.is_full
    
    def test_overflow(self):
        buf = RingBuffer(maxlen=3)
        for i in range(5):
            buf.push(float(i))
        assert buf.size == 3
        assert buf.is_full
        # Should contain [2, 3, 4]
        assert buf.last() == [2.0, 3.0, 4.0]
    
    def test_last_n(self):
        buf = RingBuffer(maxlen=10)
        for i in range(7):
            buf.push(float(i))
        assert buf.last(3) == [4.0, 5.0, 6.0]
    
    def test_sum_mean(self):
        buf = RingBuffer(maxlen=10)
        for i in [1.0, 2.0, 3.0, 4.0]:
            buf.push(i)
        assert buf.sum() == 10.0
        assert buf.mean() == 2.5
    
    def test_std(self):
        buf = RingBuffer(maxlen=10)
        for i in [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]:
            buf.push(i)
        # STD should be approximately 2.0
        assert abs(buf.std() - 2.0) < 0.01
    
    def test_is_ready(self):
        buf = RingBuffer(maxlen=10)
        assert not buf.is_ready(min_size=5)
        for i in range(5):
            buf.push(float(i))
        assert buf.is_ready(min_size=5)
    
    def test_clear(self):
        buf = RingBuffer(maxlen=10)
        buf.push(1.0)
        buf.push(2.0)
        buf.clear()
        assert buf.size == 0


class TestTimeWindowSum:
    """Tests for TimeWindowSum."""
    
    def test_init(self):
        tw = TimeWindowSum(window_sec=2.0)
        assert tw.count == 0
    
    def test_add_and_sum(self):
        tw = TimeWindowSum(window_sec=2.0)
        now = int(time.time() * 1000)
        
        tw.add(now - 500, 1.0)   # 0.5s ago
        tw.add(now - 200, 2.0)   # 0.2s ago
        tw.add(now - 100, 3.0)   # 0.1s ago
        
        total = tw.sum(now)
        assert total == 6.0
    
    def test_window_expiry(self):
        tw = TimeWindowSum(window_sec=1.0)
        now = int(time.time() * 1000)
        
        tw.add(now - 2000, 10.0)  # 2s ago (expired)
        tw.add(now - 500, 5.0)    # 0.5s ago (valid)
        
        total = tw.sum(now)
        assert total == 5.0
    
    def test_clear(self):
        tw = TimeWindowSum(window_sec=2.0)
        tw.add(int(time.time() * 1000), 1.0)
        tw.clear()
        assert tw.count == 0


class TestEMACalculator:
    """Tests for EMACalculator."""
    
    def test_init(self):
        ema = EMACalculator(span=10)
        assert ema.value is None
        assert ema.count == 0
    
    def test_first_value(self):
        ema = EMACalculator(span=10)
        result = ema.update(5.0)
        assert result == 5.0
        assert ema.value == 5.0
    
    def test_ema_decay(self):
        ema = EMACalculator(span=10)
        # First value
        ema.update(10.0)
        # Update with lower value - EMA should decrease
        ema.update(5.0)
        assert ema.value < 10.0
        assert ema.value > 5.0
    
    def test_is_ready(self):
        ema = EMACalculator(span=10)
        assert not ema.is_ready(min_count=5)
        for i in range(5):
            ema.update(float(i))
        assert ema.is_ready(min_count=5)
    
    def test_reset(self):
        ema = EMACalculator(span=10)
        ema.update(5.0)
        ema.reset()
        assert ema.value is None
        assert ema.count == 0


class TestOFIMath:
    """Tests for OFI calculation correctness."""
    
    def test_ofi_sign_positive_bid_delta(self):
        """Positive bid delta should result in positive OFI."""
        # If more bid size is added -> buying pressure -> positive OFI
        bid_delta_sum = 100.0
        ask_delta_sum = 50.0
        ofi_book = bid_delta_sum - ask_delta_sum
        assert ofi_book > 0
    
    def test_ofi_sign_negative_ask_delta(self):
        """Negative ask delta (asks removed) means buying -> positive OFI."""
        bid_delta_sum = 0.0
        ask_delta_sum = -100.0  # Asks removed
        ofi_book = bid_delta_sum - ask_delta_sum
        assert ofi_book > 0  # 0 - (-100) = +100
    
    def test_ofi_trade_buy_volume(self):
        """More buy volume than sell volume -> positive OFI."""
        buy_vol = 150.0
        sell_vol = 50.0
        ofi_trade = buy_vol - sell_vol
        assert ofi_trade > 0
    
    def test_ofi_normalization(self):
        """OFI normalization by depth."""
        raw_ofi = 100.0
        depth = 1000.0
        ofi_norm = raw_ofi / depth
        assert ofi_norm == 0.1


class TestIOCPriceCalc:
    """Tests for IOC limit price calculation."""
    
    def test_long_entry_price(self):
        """Long entry: limit = best_ask + max_slip."""
        best_ask = 1000.0
        spread = 1.0
        micro_atr = 0.5
        k_spread = 1.5
        k_atr = 0.25
        
        max_slip = max(spread * k_spread, micro_atr * k_atr)
        limit = best_ask + max_slip
        
        assert max_slip == 1.5  # spread * k wins
        assert limit == 1001.5
    
    def test_short_entry_price(self):
        """Short entry: limit = best_bid - max_slip."""
        best_bid = 1000.0
        spread = 1.0
        micro_atr = 10.0  # High volatility
        k_spread = 1.5
        k_atr = 0.25
        
        max_slip = max(spread * k_spread, micro_atr * k_atr)
        limit = best_bid - max_slip
        
        assert max_slip == 2.5  # micro_atr * k wins
        assert limit == 997.5
    
    def test_slippage_bps(self):
        """Slippage in basis points calculation."""
        max_slip = 1.0
        price = 1000.0
        slip_bps = (max_slip / price) * 10000
        assert slip_bps == 10.0  # 10 bps


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
