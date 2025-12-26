"""
Analytics system for OFI Scalper.
Tracks filter blocks and other metrics for post-run analysis.
"""
import json
import time
import re
from dataclasses import dataclass, field
from typing import Dict, Any, Optional
from collections import defaultdict
from pathlib import Path
from datetime import datetime

from .logger import get_logger

log = get_logger(__name__)


@dataclass
class FilterStats:
    """Statistics for a specific filter."""
    total_checks: int = 0
    blocked_count: int = 0
    # Map of specific reason -> count
    reasons: Dict[str, int] = field(default_factory=lambda: defaultdict(int))


class Analytics:
    """
    Central analytics tracker.
    Persists metrics to JSON incrementally to save memory.
    """
    
    def __init__(self, output_file: str = "analytics.json"):
        self.output_file = output_file
        self.start_time = datetime.now()
        
        # Filter stats: filter_name -> FilterStats
        self.filters: Dict[str, FilterStats] = defaultdict(FilterStats)
        
        # General counters
        self.counters: Dict[str, int] = defaultdict(int)
        
    def log_filter_block(self, filter_name: str, reason: str):
        """Log a blocked trade attempt."""
        stats = self.filters[filter_name]
        stats.blocked_count += 1
        stats.reasons[reason] += 1
        
    def log_filter_check(self, filter_name: str):
        """Log a filter check (pass or fail)."""
        self.filters[filter_name].total_checks += 1
        
    def increment(self, metric: str, value: int = 1):
        """Increment a general counter."""
        self.counters[metric] += value
        
    def save(self):
        """
        Save analytics to JSON file incrementally.
        Loads existing file, merges current memory stats, saves, then clears memory.
        """
        duration = datetime.now() - self.start_time
        now_iso = datetime.now().isoformat()
        
        # 1. Load existing data if valid
        existing_data = {}
        try:
            if Path(self.output_file).exists():
                with open(self.output_file, "r") as f:
                    existing_data = json.load(f)
        except Exception as e:
            log.warning(f"Could not load existing analytics (creating new): {e}")
            existing_data = {}
            
        # 2. Initialize or update base structure
        if not existing_data:
            existing_data = {
                "session_start": self.start_time.isoformat(),
                "summary": {},
                "counters": {},
                "filters": {}
            }
        
        existing_data["session_end"] = now_iso
        existing_data["duration_sec"] = duration.total_seconds()
        
        # 3. Merge counters
        merged_counters = existing_data.get("counters", {})
        for k, v in self.counters.items():
            merged_counters[k] = merged_counters.get(k, 0) + v
        existing_data["counters"] = merged_counters
        
        # 4. Merge filters
        merged_filters = existing_data.get("filters", {})
        
        for name, stats in self.filters.items():
            if name not in merged_filters:
                merged_filters[name] = {
                    "checks": 0,
                    "blocks": 0,
                    "block_rate": 0.0,
                    "reasons": {}
                }
            
            f_data = merged_filters[name]
            f_data["checks"] += stats.total_checks
            f_data["blocks"] += stats.blocked_count
            
            # Recalculate rate
            total = f_data["checks"]
            f_data["block_rate"] = (f_data["blocks"] / total) if total > 0 else 0.0
            
            # Merge reasons
            saved_reasons = f_data.get("reasons", {})
            for r_key, r_count in stats.reasons.items():
                saved_reasons[r_key] = saved_reasons.get(r_key, 0) + r_count
            f_data["reasons"] = saved_reasons
            
        existing_data["filters"] = merged_filters
        
        # Generate top-level summary
        summary = {}
        for name, data in merged_filters.items():
            summary[name] = data["blocks"]
        existing_data["summary"] = summary
        
        # 5. Save to disk
        try:
            with open(self.output_file, "w") as f:
                json.dump(existing_data, f, indent=2)
            log.info(f"Analytics saved to {self.output_file}")
            
            # 6. Clear memory stats (CRITICAL for "incremental" logic)
            self.filters.clear()
            self.counters.clear()
            
        except Exception as e:
            log.error(f"Failed to save analytics: {e}")
