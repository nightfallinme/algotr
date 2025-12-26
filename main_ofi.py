# main_ofi.py
"""
OFI Scalper Entry Point.

Usage:
    # Shadow mode (default) - logs signals, no execution
    python main_ofi.py --config config_ofi.yaml
    
    # Paper mode - small size real execution
    python main_ofi.py --config config_ofi.yaml --mode paper
    
    # Live mode - full execution (use with caution!)
    python main_ofi.py --config config_ofi.yaml --mode live
"""
import asyncio
import argparse
import signal
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from hyperbot.config import Config
from hyperbot.ofi_orchestrator import OFIOrchestrator, ExecutionMode
from hyperbot.logger import setup_logging, get_logger


def parse_args():
    parser = argparse.ArgumentParser(description="OFI Scalper Bot")
    parser.add_argument(
        "--config", "-c",
        type=str,
        default="config_ofi.yaml",
        help="Path to config file"
    )
    parser.add_argument(
        "--mode", "-m",
        type=str,
        choices=["shadow", "paper", "live"],
        default=None,
        help="Execution mode (overrides config)"
    )
    parser.add_argument(
        "--symbols", "-s",
        type=str,
        nargs="+",
        default=None,
        help="Trading symbols (overrides config)"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging"
    )
    return parser.parse_args()


def get_mode(args, config) -> ExecutionMode:
    """Determine execution mode from args or config."""
    if args.mode:
        mode_str = args.mode.lower()
    else:
        mode_str = config.as_dict().get("ofi", {}).get("mode", "shadow").lower()
    
    if mode_str == "live":
        return ExecutionMode.LIVE
    elif mode_str == "paper":
        return ExecutionMode.PAPER
    else:
        return ExecutionMode.SHADOW


async def main():
    args = parse_args()
    
    # Setup logging
    log_level = "DEBUG" if args.debug else "INFO"
    setup_logging(level=log_level)
    log = get_logger(__name__)
    
    log.info("=" * 50)
    log.info("OFI Scalper Starting")
    log.info("=" * 50)
    
    # Load config
    try:
        config = Config.load(args.config)
        log.info(f"Loaded config from {args.config}")
    except FileNotFoundError:
        log.error(f"Config file not found: {args.config}")
        sys.exit(1)
    
    # Determine mode
    mode = get_mode(args, config)
    log.info(f"Execution mode: {mode.name}")
    
    if mode == ExecutionMode.LIVE:
        log.warning("=" * 50)
        log.warning("LIVE MODE - REAL MONEY AT RISK!")
        log.warning("=" * 50)
        # Add confirmation prompt for safety
        print("\n⚠️  LIVE MODE - Real orders will be placed!")
        print("Press ENTER to continue or Ctrl+C to abort...")
        try:
            input()
        except KeyboardInterrupt:
            log.info("Aborted by user")
            sys.exit(0)
    
    # Get symbols
    if args.symbols:
        symbols = args.symbols
    else:
        symbols = config.as_dict().get("data", {}).get("symbols", ["ETH"])
    
    log.info(f"Trading symbols: {symbols}")
    
    # Create orchestrator
    orchestrator = OFIOrchestrator(
        config=config,
        symbols=symbols,
        execution_mode=mode,
    )
    
    # Setup signal handlers
    loop = asyncio.get_event_loop()
    
    def shutdown_handler():
        log.info("Shutdown signal received")
        asyncio.create_task(orchestrator.stop())
    
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown_handler)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass
    
    # Run
    try:
        await orchestrator.run()
    except KeyboardInterrupt:
        log.info("Keyboard interrupt")
    finally:
        await orchestrator.stop()
    
    log.info("OFI Scalper stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass  # Clean exit on Ctrl+C
