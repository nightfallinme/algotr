# main.py
"""
HyperBot: Always-On Trend Scalper

Entry point for the trading bot.

Usage:
    python main.py           # Run with Hyperliquid (requires env vars)
    python main.py --mock    # Run with mock exchange for testing

Environment Variables:
    HL_SECRET_KEY       - Hyperliquid private key
    HL_ACCOUNT_ADDRESS  - Hyperliquid wallet address
"""
import argparse
import sys

from hyperbot.orchestrator import Orchestrator
from hyperbot.config import get_config
from hyperbot.logger import setup_logging, get_logger


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="HyperBot Always-On Trend Scalper"
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Use mock exchange for testing (no real trades)"
    )
    parser.add_argument(
        "--config",
        type=str,
        default="config.yaml",
        help="Path to configuration file"
    )
    return parser.parse_args()


def main():
    """Main entry point."""
    args = parse_args()
    
    print("""
    ╔═══════════════════════════════════════════════════════╗
    ║     HyperBot: Always-On Trend Scalper                 ║
    ║     Modular | Data-Driven | Aggressive yet Safe       ║
    ╚═══════════════════════════════════════════════════════╝
    """)
    
    if args.mock:
        print("    ⚠️  Running in MOCK mode (no real trades)\n")
    else:
        print("    🚀 Running in LIVE mode (Hyperliquid mainnet)\n")
    
    try:
        # Load configuration
        from hyperbot.config import Config
        config = Config.load(args.config if args.config != "config.yaml" else None)
        
        # Create and run orchestrator
        orchestrator = Orchestrator(config=config, use_mock=args.mock)
        orchestrator.run()
        
    except KeyboardInterrupt:
        print("\n\n    Shutdown requested. Goodbye!")
        sys.exit(0)
    except Exception as e:
        print(f"\n    ❌ Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()