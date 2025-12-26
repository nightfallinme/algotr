# test_mock_exchange.py
"""
MockExchange Test Script

Runs the bot with test_strategy indicator to exercise all MockExchange scenarios:
- Position entry (LONG/SHORT)
- Stop loss triggers
- Take profit triggers
- Time-based exits
- Position persistence (restart test)

Usage:
    python test_mock_exchange.py
"""
import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import time
from datetime import datetime

from hyperbot.orchestrator import Orchestrator
from hyperbot.config import Config
from hyperbot.logger import setup_logging, get_logger

setup_logging()
log = get_logger(__name__)


def run_test():
    """Run MockExchange test session."""
    print("""
    ╔════════════════════════════════════════════════════════╗
    ║     MockExchange Test Suite                            ║
    ║     Testing: Entry, SL, TP, Time Exit, Persistence     ║
    ╚════════════════════════════════════════════════════════╝
    """)
    
    # Load test config
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "config_test.yaml")
    config = Config.load(config_path)
    
    print(f"    📊 Symbol: {config.data.get('symbol')}")
    print(f"    🎲 Test mode: random signals with 40% probability")
    print(f"    ⏱️  Tick interval: 5 seconds")
    print(f"    🛑 Max bars in trade: 5")
    print()
    
    # Create orchestrator
    orchestrator = Orchestrator(config=config, use_mock=True)
    
    # Track test metrics
    start_time = datetime.now()
    start_balance = orchestrator.execution_manager.get_balance()
    
    print(f"    💰 Starting balance: ${start_balance:.2f}")
    print()
    print("    🚀 Starting test run... (Press Ctrl+C to stop)")
    print("    " + "=" * 50)
    print()
    
    try:
        orchestrator.run()
    except KeyboardInterrupt:
        pass
    
    # Print test results
    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()
    
    stats = orchestrator.execution_manager.get_stats()
    end_balance = orchestrator.execution_manager.get_balance()
    
    print()
    print("    " + "=" * 50)
    print("    📊 TEST RESULTS")
    print("    " + "=" * 50)
    print(f"    Duration: {duration:.0f} seconds")
    print(f"    Total trades: {stats['total_trades']}")
    print(f"    Wins: {stats['wins']}")
    print(f"    Losses: {stats['losses']}")
    print(f"    Win rate: {stats['win_rate']:.1f}%")
    print(f"    Total PnL: ${stats['total_pnl']:.2f}")
    print(f"    Ending balance: ${end_balance:.2f}")
    print()
    
    # Detailed trade log
    trades = orchestrator.execution_manager.get_trade_history()
    if trades:
        print("    📋 Trade History:")
        for t in trades:
            emoji = "✅" if t.pnl > 0 else "❌"
            print(f"      {emoji} #{t.trade_id} | {t.side.upper()} | "
                  f"Entry: ${t.entry_price:.2f} → Exit: ${t.exit_price:.2f} | "
                  f"PnL: ${t.pnl:.2f} | Reason: {t.exit_reason}")
    else:
        print("    ⚠️  No trades executed during test")
    
    print()
    print("    Test complete!")
    

if __name__ == "__main__":
    run_test()
