# hyperbot/logger.py
"""
Industrial-grade logging system with categorical event tagging.

Features:
- Tagged event categories (SIGNAL, RISK, EXEC, STATE, DCA, etc.)
- Debug-level decision points with clear reasoning
- Config-based toggle for each category
- Emoji prefixes for quick visual scanning
"""
import logging
import sys
from pathlib import Path
from typing import Optional, Dict, Set
from enum import Enum


# ============================================
# EVENT CATEGORIES
# ============================================
class LogCategory(str, Enum):
    """Event categories for filtering logs."""
    # === CORE TRADING ===
    SIGNAL = "signal"      # Indicator signals and aggregation
    RISK = "risk"          # Risk engine decisions (SL/TP, sizing)
    EXEC = "exec"          # Execution/order events (open/close/reduce)
    STATE = "state"        # State machine transitions (IDLE→ARMED→FIRING)
    DCA = "dca"            # DCA/Pyramiding decisions
    EXIT = "exit"          # Exit condition checks (SL/TP/time/strategy)
    
    # === DATA & MARKET ===
    PRICE = "price"        # Price sync/basis events
    REGIME = "regime"      # Regime filter events (trend/volatility)
    TICK = "tick"          # Per-tick data fetch (very verbose)
    
    # === SYSTEM ===
    INIT = "init"          # Startup/initialization logs
    TRADE = "trade"        # Trade lifecycle (entry/exit snapshots)
    STRESS = "stress"      # Stress test scenario logs
    STATUS = "status"      # Periodic status updates


# Category emoji prefixes for visual scanning
CATEGORY_EMOJI = {
    LogCategory.SIGNAL: "📶",
    LogCategory.RISK: "⚠️",
    LogCategory.EXEC: "💰",
    LogCategory.STATE: "🔄",
    LogCategory.DCA: "📈",
    LogCategory.EXIT: "🚪",
    LogCategory.PRICE: "💱",
    LogCategory.REGIME: "🌡️",
    LogCategory.TICK: "⏱️",
    LogCategory.INIT: "🚀",
    LogCategory.TRADE: "📝",
    LogCategory.STRESS: "🧪",
    LogCategory.STATUS: "📊",
}

# Global enabled categories (controlled by config)
_enabled_categories: Set[LogCategory] = set(LogCategory)  # All enabled by default
_debug_mode: bool = False
_loggers: dict = {}


def configure_categories(
    enabled: Optional[list] = None,
    disabled: Optional[list] = None,
    debug_mode: bool = False
) -> None:
    """
    Configure which log categories are enabled.
    
    Args:
        enabled: List of category names to enable (enables ONLY these)
        disabled: List of category names to disable (disables from all)
        debug_mode: If True, show DEBUG level logs for enabled categories
    """
    global _enabled_categories, _debug_mode
    _debug_mode = debug_mode
    
    if enabled is not None:
        # Enable only specified categories
        _enabled_categories = set()
        for cat in enabled:
            try:
                _enabled_categories.add(LogCategory(cat.lower()))
            except ValueError:
                pass  # Invalid category name, ignore
    elif disabled is not None:
        # Start with all, remove disabled
        _enabled_categories = set(LogCategory)
        for cat in disabled:
            try:
                _enabled_categories.discard(LogCategory(cat.lower()))
            except ValueError:
                pass


def is_category_enabled(category: LogCategory) -> bool:
    """Check if a category is enabled."""
    return category in _enabled_categories


def setup_logging(
    level: str = "INFO",
    log_file: str = "hyperbot.log",
    console: bool = True,
    log_format: Optional[str] = None,
    debug_categories: Optional[list] = None,
    disabled_categories: Optional[list] = None
) -> None:
    """
    Configure root logging settings.
    
    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: Path to log file
        console: Whether to also log to console
        log_format: Custom log format string
        debug_categories: List of categories to enable in debug mode
        disabled_categories: List of categories to disable entirely
    """
    global _debug_mode
    
    if log_format is None:
        log_format = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    
    # Configure categories
    # IMPORTANT: 
    #   - debug_categories=None -> all categories enabled (default)
    #   - debug_categories=[] -> NO categories enabled (all commented out)
    #   - debug_categories=['dca','risk'] -> only those enabled
    if debug_categories is not None:  # Key exists in config (even if empty list)
        configure_categories(enabled=debug_categories, debug_mode=True)
    if disabled_categories:
        configure_categories(disabled=disabled_categories)
    
    _debug_mode = level.upper() == "DEBUG"
    
    # Get root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG if _debug_mode else getattr(logging, level.upper(), logging.INFO))
    
    # Clear existing handlers
    root_logger.handlers.clear()
    
    formatter = logging.Formatter(log_format)
    
    # File handler - always DEBUG level for full history
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)
    
    # Console handler
    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.DEBUG if _debug_mode else getattr(logging, level.upper(), logging.INFO))
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)
    
    # === SUPPRESS NOISY THIRD-PARTY LOGGERS ===
    # These libraries are extremely verbose at DEBUG level
    noisy_loggers = [
        "ccxt",
        "ccxt.base.exchange",
        "urllib3",
        "urllib3.connectionpool",
        "requests",
        "websockets",
        "asyncio",
        "httpx",
        "httpcore",
    ]
    for logger_name in noisy_loggers:
        logging.getLogger(logger_name).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """
    Get a configured logger by name.
    
    Args:
        name: Logger name (typically __name__)
        
    Returns:
        Configured logger instance
    """
    if name not in _loggers:
        logger = logging.getLogger(name)
        _loggers[name] = logger
    return _loggers[name]


# ============================================
# CATEGORICAL EVENT LOGGERS
# ============================================

def log_event(
    category: LogCategory,
    message: str,
    level: str = "DEBUG",
    logger_name: Optional[str] = None
) -> None:
    """
    Log a categorized event.
    
    Args:
        category: Event category for filtering
        message: Log message
        level: Log level (DEBUG, INFO, WARNING, ERROR)
        logger_name: Optional logger name override
    """
    if not is_category_enabled(category):
        return
    
    emoji = CATEGORY_EMOJI.get(category, "📋")
    logger = get_logger(logger_name or category.value)
    
    log_level = getattr(logging, level.upper(), logging.DEBUG)
    
    # For DEBUG level, only log if debug mode is on
    if log_level == logging.DEBUG and not _debug_mode:
        return
        
    logger.log(log_level, f"{emoji} [{category.value.upper()}] {message}")


def log_signal_event(
    indicator: str,
    direction: str,
    strength: float,
    reason: str,
    passed: bool = True
) -> None:
    """Log indicator signal generation."""
    status = "✅ PASS" if passed else "❌ FAIL"
    log_event(
        LogCategory.SIGNAL,
        f"{indicator} → {direction} ({strength:.2f}) | {reason} | {status}",
        level="INFO" if passed else "DEBUG"
    )


def log_risk_event(
    event: str,
    details: str,
    passed: bool = True
) -> None:
    """Log risk engine decision."""
    status = "✅" if passed else "❌ BLOCKED"
    log_event(
        LogCategory.RISK,
        f"{event} | {details} | {status}",
        level="INFO" if passed else "WARNING"
    )


def log_exec_event(
    action: str,
    symbol: str,
    side: str,
    size: float,
    price: float,
    **kwargs
) -> None:
    """Log execution/order events."""
    details = " | ".join(f"{k}={v}" for k, v in kwargs.items())
    message = f"{action} {symbol} {side} | size={size:.4f} @ {price:.2f}"
    if details:
        message += f" | {details}"
    log_event(LogCategory.EXEC, message, level="INFO")


def log_state_event(old_state: str, new_state: str, reason: str) -> None:
    """Log state machine transitions."""
    log_event(
        LogCategory.STATE,
        f"{old_state} → {new_state} | {reason}",
        level="INFO"
    )


def log_dca_event(
    event: str,
    details: str,
    approved: bool = False
) -> None:
    """Log DCA/Pyramiding decisions."""
    status = "✅ APPROVED" if approved else "❌ DENIED"
    log_event(
        LogCategory.DCA,
        f"{event} | {details} | {status}",
        level="INFO" if approved else "DEBUG"
    )


def log_exit_event(
    check: str,
    result: bool,
    details: str = ""
) -> None:
    """Log exit condition checks."""
    status = "🔴 TRIGGER" if result else "⚪ SKIP"
    msg = f"{check} | {status}"
    if details:
        msg += f" | {details}"
    log_event(
        LogCategory.EXIT,
        msg,
        level="INFO" if result else "DEBUG"
    )


def log_price_event(
    event: str,
    data_price: float,
    exec_price: float,
    **kwargs
) -> None:
    """Log price sync/basis events."""
    basis = exec_price - data_price
    basis_pct = (basis / data_price) * 100 if data_price else 0
    details = " | ".join(f"{k}={v}" for k, v in kwargs.items())
    message = f"{event} | Data=${data_price:.2f} Exec=${exec_price:.2f} Basis=${basis:.2f} ({basis_pct:+.3f}%)"
    if details:
        message += f" | {details}"
    log_event(LogCategory.PRICE, message, level="DEBUG")


def log_regime_event(
    event: str,
    trend: str,
    valid: bool,
    reason: str = ""
) -> None:
    """Log regime filter events."""
    status = "✅ VALID" if valid else "❌ INVALID"
    msg = f"{event} | Trend={trend} | {status}"
    if reason:
        msg += f" | {reason}"
    log_event(
        LogCategory.REGIME,
        msg,
        level="INFO" if valid else "DEBUG"
    )


def log_tick_event(message: str) -> None:
    """Log per-tick processing (very verbose)."""
    log_event(LogCategory.TICK, message, level="DEBUG")


# ============================================
# LEGACY COMPATIBILITY
# ============================================

def log_trade(
    action: str,
    symbol: str,
    side: str,
    size: float,
    price: float,
    **kwargs
) -> None:
    """Legacy trade logging - redirects to log_exec_event."""
    log_exec_event(action, symbol, side, size, price, **kwargs)


def log_state_change(old_state: str, new_state: str, reason: str) -> None:
    """Legacy state logging - redirects to log_state_event."""
    log_state_event(old_state, new_state, reason)


def log_signal(
    indicator: str,
    direction: str,
    strength: float,
    reason: str
) -> None:
    """Legacy signal logging - redirects to log_signal_event."""
    log_signal_event(indicator, direction, strength, reason, passed=True)


# ============================================
# NEW CATEGORY HELPERS
# ============================================

def log_init_event(module: str, message: str) -> None:
    """Log initialization/startup events."""
    log_event(LogCategory.INIT, f"{module} | {message}", level="INFO")


def log_trade_event(
    event: str,
    symbol: str,
    details: str,
    **kwargs
) -> None:
    """Log trade lifecycle events (entry/exit snapshots)."""
    msg = f"{event} {symbol} | {details}"
    for key, val in kwargs.items():
        msg += f" | {key}={val}"
    log_event(LogCategory.TRADE, msg, level="INFO")


def log_stress_event(message: str, is_info: bool = False) -> None:
    """Log stress test scenario events."""
    log_event(LogCategory.STRESS, message, level="INFO" if is_info else "DEBUG")


def log_status_event(message: str) -> None:
    """Log periodic status updates."""
    log_event(LogCategory.STATUS, message, level="INFO")