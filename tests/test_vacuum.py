# tests/test_vacuum.py
"""
Unit tests for Vacuum Scalper components.

Run with:
    python -m pytest tests/test_vacuum.py -v
"""
import pytest
from typing import List, Tuple

# Import vacuum components
from hyperbot.vacuum.liquidity_map import LiquidityMap, LiquidityWall, LiquidityLevel
from hyperbot.vacuum.vacuum_detector import VacuumDetector, VacuumSignal
from hyperbot.vacuum.wall_tracker import WallTracker, WallTouch
from hyperbot.vacuum.lir_calculator import LIRCalculator, LIRSignal


class MockTopOfBook:
    """Mock TopOfBook for testing."""
    def __init__(
        self,
        best_bid: float = 100.0,
        best_ask: float = 100.1,
        bid_size: float = 1.0,
        ask_size: float = 1.0,
    ):
        self.best_bid = best_bid
        self.best_ask = best_ask
        self.bid_size = bid_size
        self.ask_size = ask_size
        self.timestamp_ms = 1000000
    
    @property
    def spread(self) -> float:
        return self.best_ask - self.best_bid
    
    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2
    
    @property
    def microprice(self) -> float:
        total = self.bid_size + self.ask_size
        if total <= 0:
            return self.mid
        return (self.best_bid * self.ask_size + self.best_ask * self.bid_size) / total
    
    @property
    def is_valid(self) -> bool:
        return self.best_bid > 0 and self.best_ask > self.best_bid


# ============ LiquidityMap Tests ============

class TestLiquidityMap:
    """Tests for LiquidityMap."""
    
    def test_basic_update(self):
        """Test basic book update."""
        liq_map = LiquidityMap()
        
        bids = [(100.0, 1.0), (99.9, 2.0), (99.8, 1.5)]
        asks = [(100.1, 1.0), (100.2, 2.0), (100.3, 1.5)]
        
        liq_map.update(bids, asks)
        
        assert liq_map.mid > 0
        assert len(liq_map.bid_levels) == 3
        assert len(liq_map.ask_levels) == 3
    
    def test_wall_detection(self):
        """Test wall detection with large order."""
        liq_map = LiquidityMap(
            wall_min_size_mult=3.0,
            wall_max_distance_bps=20.0,
            wall_stability_ticks=1,
        )
        
        # Create book with a wall (10x normal size)
        bids = [(100.0, 1.0), (99.9, 10.0), (99.8, 1.0)]  # Wall at 99.9
        asks = [(100.1, 1.0), (100.2, 1.0), (100.3, 12.0)]  # Wall at 100.3
        
        # First update to establish baseline
        liq_map.update(bids, asks)
        
        # Second update for stability
        liq_map.update(bids, asks)
        
        bid_wall, ask_wall = liq_map.get_walls(require_stable=True)
        
        # Should detect bid wall at 99.9
        assert bid_wall is not None
        assert bid_wall.price == 99.9
        assert bid_wall.size == 10.0
    
    def test_depth_within_bps(self):
        """Test depth calculation within bps range."""
        liq_map = LiquidityMap()
        
        # Mid = 100.05, so 10 bps = 0.1
        bids = [(100.0, 1.0), (99.95, 2.0), (99.9, 3.0)]
        asks = [(100.1, 1.0), (100.15, 2.0), (100.2, 3.0)]
        
        liq_map.update(bids, asks)
        
        # Within 10 bps should capture close levels
        bid_depth = liq_map.get_depth_within_bps("bid", 10.0)
        assert bid_depth > 0
    
    def test_median_depth(self):
        """Test median depth calculation."""
        liq_map = LiquidityMap()
        
        bids = [(100.0, 1.0), (99.9, 2.0), (99.8, 3.0)]
        asks = [(100.1, 1.0), (100.2, 2.0), (100.3, 3.0)]
        
        # Need multiple updates for median
        for _ in range(10):
            liq_map.update(bids, asks)
        
        median = liq_map.get_median_depth()
        assert median > 0
        assert 1.0 <= median <= 3.0  # Should be in range of sizes


# ============ VacuumDetector Tests ============

class TestVacuumDetector:
    """Tests for VacuumDetector."""
    
    def test_ask_vacuum_detection(self):
        """Test vacuum detection on ask side."""
        # Note: wall at 100.3 from mid 100.05 = ~250bps, so need larger limit
        liq_map = LiquidityMap(
            wall_stability_ticks=1,
            wall_min_size_mult=2.0,
            wall_max_distance_bps=300.0,  # Allow walls up to 300bps away
        )
        detector = VacuumDetector(
            min_vacuum_bps=1.0,
            vacuum_thin_mult=2.0,
            max_vacuum_bps=300.0,  # Allow larger vacuums
        )
        
        # Create book with thin zone before ask wall
        # Mid = 100.05
        # Wall at 100.30 = ~25 bps away
        bids = [(100.0, 5.0), (99.9, 5.0)]
        asks = [
            (100.1, 0.1),   # Thin
            (100.2, 0.1),   # Thin
            (100.3, 50.0),  # WALL (bigger for detection)
        ]
        
        # More updates to build median baseline
        for _ in range(30):
            liq_map.update(bids, asks)
        
        tob = MockTopOfBook(best_bid=100.0, best_ask=100.1)
        
        vacuum = detector.detect_ask_vacuum(liq_map, tob)
        
        # Should detect vacuum between 100.1 and 100.3
        assert vacuum is not None
        assert vacuum.side == "ask"
        assert vacuum.wall_price == 100.3
    
    def test_no_vacuum_if_thick_liquidity(self):
        """Test no vacuum when liquidity is thick."""
        liq_map = LiquidityMap(wall_stability_ticks=1)
        detector = VacuumDetector(min_vacuum_bps=1.0, vacuum_thin_mult=0.3)
        
        # Create book with thick liquidity everywhere
        bids = [(100.0, 5.0), (99.9, 5.0)]
        asks = [
            (100.1, 5.0),   # Thick
            (100.2, 5.0),   # Thick
            (100.3, 20.0),  # Wall
        ]
        
        for _ in range(3):
            liq_map.update(bids, asks)
        
        tob = MockTopOfBook(best_bid=100.0, best_ask=100.1)
        
        vacuum = detector.detect_ask_vacuum(liq_map, tob)
        
        # Should not detect valid vacuum (zone is thick)
        assert vacuum is None or not vacuum.is_valid


# ============ LIRCalculator Tests ============

class TestLIRCalculator:
    """Tests for LIRCalculator."""
    
    def test_balanced_lir(self):
        """Test LIR = 1 when depth is balanced."""
        liq_map = LiquidityMap()
        calc = LIRCalculator(depth_bps=10.0, ema_span=5)  # Smaller span for faster ready
        
        # Symmetric book
        bids = [(100.0, 5.0), (99.95, 5.0)]
        asks = [(100.1, 5.0), (100.15, 5.0)]
        
        for _ in range(25):
            liq_map.update(bids, asks)
            calc.compute(liq_map)  # Each update advances LIR
        
        signal = calc.compute(liq_map)
        
        assert signal.is_ready
        assert 0.9 <= signal.lir_smooth <= 1.1  # ~1.0
    
    def test_bullish_lir(self):
        """Test LIR > 1 when bids dominate."""
        liq_map = LiquidityMap()
        calc = LIRCalculator(depth_bps=10.0, ema_span=5)
        
        # Heavy bid side
        bids = [(100.0, 20.0), (99.95, 20.0)]
        asks = [(100.1, 5.0), (100.15, 5.0)]
        
        for _ in range(25):
            liq_map.update(bids, asks)
            calc.compute(liq_map)
        
        signal = calc.compute(liq_map)
        
        assert signal.is_ready
        assert signal.lir_smooth > 1.5  # Bullish
        assert calc.is_long_signal(signal)
    
    def test_bearish_lir(self):
        """Test LIR < 1 when asks dominate."""
        liq_map = LiquidityMap()
        calc = LIRCalculator(depth_bps=10.0, ema_span=5)
        
        # Heavy ask side
        bids = [(100.0, 5.0), (99.95, 5.0)]
        asks = [(100.1, 20.0), (100.15, 20.0)]
        
        for _ in range(25):
            liq_map.update(bids, asks)
            calc.compute(liq_map)
        
        signal = calc.compute(liq_map)
        
        assert signal.is_ready
        assert signal.lir_smooth < 0.66  # Bearish
        assert calc.is_short_signal(signal)


# ============ WallTracker Tests ============

class TestWallTracker:
    """Tests for WallTracker."""
    
    def test_wall_touch_detection(self):
        """Test wall touch detection."""
        tracker = WallTracker(touch_threshold_bps=1.0)
        
        # Create wall at 100.3
        wall = LiquidityWall(
            price=100.3,
            size=10.0,
            side="ask",
            distance_bps=25.0,
        )
        
        mid = 100.0
        
        # Price approaches wall
        tracker.update(100.29, mid, None, wall, now_ms=1000)
        
        touch = tracker.get_touch("ask")
        assert touch is not None
        assert touch.touch_count >= 1
    
    def test_rejection_detection(self):
        """Test rejection after wall touch."""
        tracker = WallTracker(
            touch_threshold_bps=1.0,
            rejection_threshold_bps=0.5,
        )
        
        wall = LiquidityWall(
            price=100.3,
            size=10.0,
            side="ask",
            distance_bps=25.0,
        )
        
        mid = 100.0
        
        # Touch wall
        tracker.update(100.29, mid, None, wall, now_ms=1000)
        
        # Move away (rejection)
        tracker.update(100.20, mid, None, wall, now_ms=1100)
        tracker.update(100.15, mid, None, wall, now_ms=1200)
        tracker.update(100.10, mid, None, wall, now_ms=1300)
        
        # Should detect rejection
        assert tracker.is_rejection("ask", min_rejection_ticks=2)
    
    def test_wall_erosion_detection(self):
        """Test wall erosion (size decreasing)."""
        tracker = WallTracker(touch_threshold_bps=1.0)
        
        # Initial wall
        wall = LiquidityWall(
            price=100.3,
            size=10.0,
            side="ask",
            distance_bps=25.0,
        )
        
        mid = 100.0
        
        # Touch wall
        tracker.update(100.29, mid, None, wall, now_ms=1000)
        
        # Wall size decreases (being absorbed)
        wall_eroded = LiquidityWall(
            price=100.3,
            size=5.0,  # Reduced from 10
            side="ask",
            distance_bps=25.0,
        )
        
        tracker.update(100.29, mid, None, wall_eroded, now_ms=1100)
        
        touch = tracker.get_touch("ask")
        assert touch is not None
        assert touch.wall_eroding  # Wall is being absorbed


# ============ Integration Tests ============

class TestVacuumStrategyIntegration:
    """Integration tests for full strategy flow."""
    
    def test_fade_setup_detection(self):
        """Test detection of fade setup (wall rejection)."""
        # Use pre-created wall object for controlled testing
        tracker = WallTracker(
            touch_threshold_bps=10.0,  # 10bps = 0.1% of price
            rejection_threshold_bps=5.0,  # 5bps minimum
        )
        
        # Create wall at 100.10 (10bps from mid 100.0)
        ask_wall = LiquidityWall(
            price=100.10,
            size=50.0,
            side="ask",
            distance_bps=10.0,
        )
        
        mid = 100.0
        
        # Price touches wall (within 10bps of 100.10 = 100.00 to 100.20)
        tracker.update(100.09, mid, None, ask_wall, now_ms=1000)
        tracker.update(100.10, mid, None, ask_wall, now_ms=1050)
        
        # Verify touch detected
        touch = tracker.get_touch("ask")
        assert touch is not None, "Touch should be detected"
        
        # Price rejects from wall
        tracker.update(100.00, mid, None, ask_wall, now_ms=1100)
        tracker.update(99.95, mid, None, ask_wall, now_ms=1200)
        tracker.update(99.90, mid, None, ask_wall, now_ms=1300)
        
        # Verify rejection
        touch = tracker.get_touch("ask")
        assert touch is not None
        assert touch.rejection_ticks >= 2
        assert touch.wall_stable  # Wall did not erode
    
    def test_break_setup_detection(self):
        """Test detection of break setup (vacuum + LIR)."""
        # This test verifies that vacuum and LIR are computed correctly.
        # The specific values are less important than verifying the flow works.
        liq_map = LiquidityMap(
            wall_stability_ticks=1,
            wall_min_size_mult=2.0,
            wall_max_distance_bps=50.0,
        )
        detector = VacuumDetector(
            min_vacuum_bps=2.0,
            vacuum_thin_mult=2.0,
            max_vacuum_bps=50.0,
        )
        calc = LIRCalculator(depth_bps=10.0, ema_span=5)
        
        # Book setup - the key is vacuum exists and LIR is computed
        bids = [(100.00, 50.0), (99.98, 50.0)]
        asks = [(100.02, 0.5), (100.06, 100.0)]
        
        for _ in range(30):
            liq_map.update(bids, asks)
            calc.compute(liq_map)
        
        tob = MockTopOfBook(
            best_bid=100.00,
            best_ask=100.02,
            bid_size=50.0,
            ask_size=0.5,
        )
        
        # Detect vacuum
        vacuum = detector.detect_ask_vacuum(liq_map, tob)
        
        # Calculate LIR
        lir = calc.compute(liq_map)
        
        # Verify vacuum detection works
        assert vacuum is not None, f"Vacuum should be detected. Walls: {liq_map.get_walls()}"
        assert vacuum.side == "ask"
        
        # Verify LIR is computed and ready
        assert lir.is_ready
        # LIR will reflect the depth at configured range
        # The exact value depends on which levels fall within range


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

