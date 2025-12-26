# hyperbot/config.py
"""
Configuration loader with YAML support and environment variable overrides.
"""
import os
import yaml
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, Any, Optional


@dataclass
class Config:
    """Immutable configuration object loaded from YAML."""
    data: Dict[str, Any] = field(default_factory=dict)
    execution: Dict[str, Any] = field(default_factory=dict)
    regime: Dict[str, Any] = field(default_factory=dict)
    regime_filter: Dict[str, Any] = field(default_factory=dict)  # Macro trend filter
    indicators: Dict[str, Any] = field(default_factory=dict)
    risk: Dict[str, Any] = field(default_factory=dict)
    protection: Dict[str, Any] = field(default_factory=dict)
    logging: Dict[str, Any] = field(default_factory=dict)
    entry_filters: Dict[str, Any] = field(default_factory=dict)
    dca: Dict[str, Any] = field(default_factory=dict)  # DCA/Pyramiding
    ofi: Dict[str, Any] = field(default_factory=dict)  # OFI Scalper config
    kill_switch: Dict[str, Any] = field(default_factory=dict)  # Kill switch config
    binance: Dict[str, Any] = field(default_factory=dict)  # Binance cross-venue context
    maker_mode: Dict[str, Any] = field(default_factory=dict)  # Maker reversion mode
    exchange: Dict[str, Any] = field(default_factory=dict)  # Exchange selection (hyperliquid/bybit)
    
    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        """
        Load configuration from YAML file.
        
        Args:
            path: Path to config file. Defaults to config.yaml in project root.
            
        Returns:
            Config instance with all settings loaded.
        """
        if path is None:
            # Find config.yaml relative to project root
            path = Path(__file__).parent.parent / "config.yaml"
        else:
            path = Path(path)
            
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")
            
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        
        # Override secrets from environment variables
        raw.setdefault("execution", {})
        
        secret_key = os.getenv("HL_SECRET_KEY")
        account_address = os.getenv("HL_ACCOUNT_ADDRESS")
        
        if secret_key:
            raw["execution"]["secret_key"] = secret_key
        if account_address:
            raw["execution"]["account_address"] = account_address
            
        return cls(
            data=raw.get("data", {}),
            execution=raw.get("execution", {}),
            regime=raw.get("regime", {}),
            regime_filter=raw.get("regime_filter", {}),
            indicators=raw.get("indicators", {}),
            risk=raw.get("risk", {}),
            protection=raw.get("protection", {}),
            logging=raw.get("logging", {}),
            entry_filters=raw.get("entry_filters", {}),
            dca=raw.get("dca", {}),
            ofi=raw.get("ofi", {}),
            kill_switch=raw.get("kill_switch", {}),
            binance=raw.get("binance", {}),
            maker_mode=raw.get("maker_mode", {}),
            exchange=raw.get("exchange", {"type": "hyperliquid", "testnet": False}),
        )
    
    def get_indicator_config(self, name: str) -> Dict[str, Any]:
        """Get configuration for a specific indicator."""
        return self.indicators.get(name, {})
    
    def get_indicator_weight(self, name: str) -> float:
        """Get weight for a specific indicator."""
        return self.indicators.get(name, {}).get("weight", 0.5)
    
    def is_indicator_enabled(self, name: str) -> bool:
        """Check if an indicator is enabled."""
        return self.indicators.get(name, {}).get("enabled", True)
    
    def is_indicator_mandatory(self, name: str) -> bool:
        """Check if an indicator is mandatory."""
        return self.indicators.get(name, {}).get("is_mandatory", False)
    
    def as_dict(self) -> Dict[str, Any]:
        """Return full configuration as dictionary."""
        return {
            "data": self.data,
            "execution": self.execution,
            "regime": self.regime,
            "regime_filter": self.regime_filter,
            "indicators": self.indicators,
            "risk": self.risk,
            "protection": self.protection,
            "logging": self.logging,
            "entry_filters": self.entry_filters,
            "dca": self.dca,
            "ofi": self.ofi,
            "kill_switch": self.kill_switch,
            "binance": self.binance,
            "maker_mode": self.maker_mode,
            "exchange": self.exchange,
        }


# Singleton instance for global access
_config: Optional[Config] = None


def get_config() -> Config:
    """Get the global config instance, loading if necessary."""
    global _config
    if _config is None:
        _config = Config.load()
    return _config


def reload_config(path: Optional[str] = None) -> Config:
    """Force reload configuration from file."""
    global _config
    _config = Config.load(path)
    return _config
