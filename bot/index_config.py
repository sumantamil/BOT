"""
Index Configuration Registry

Central registry for all supported indices (NIFTY, BANKNIFTY, SENSEX).
Every index-specific parameter lives here so nothing is hardcoded elsewhere.
"""

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class IndexConfig:
    name: str               # "NIFTY", "BANKNIFTY", "SENSEX"
    display_name: str       # "NIFTY 50", "BANK NIFTY", "SENSEX"
    yahoo_symbol: str       # Yahoo Finance ticker
    expiry_weekday: int     # 0=Mon, 1=Tue, 2=Wed, 3=Thu, ...
    weekly_expiry: bool     # True if index has weekly options
    strike_interval: int    # 50 for NIFTY, 100 for others
    lot_size: int
    exchange: str           # "NSE" or "BSE"
    nse_deriv_param: str    # param for NSE liveEquity-derivatives API ("" if N/A)
    expiry_day_name: str    # "Tuesday", "Thursday", etc.
    strike_range: tuple     # (min, max) for validation


NIFTY = IndexConfig(
    name="NIFTY",
    display_name="NIFTY 50",
    yahoo_symbol="^NSEI",
    expiry_weekday=1,       # Tuesday
    weekly_expiry=True,
    strike_interval=50,
    lot_size=65,
    exchange="NSE",
    nse_deriv_param="nse50_opt",
    expiry_day_name="Tuesday",
    strike_range=(15000, 35000),
)

BANKNIFTY = IndexConfig(
    name="BANKNIFTY",
    display_name="BANK NIFTY",
    yahoo_symbol="^NSEBANK",
    expiry_weekday=1,       # Last Tuesday of month (monthly only)
    weekly_expiry=False,
    strike_interval=100,
    lot_size=30,
    exchange="NSE",
    nse_deriv_param="nifty_bank_opt",
    expiry_day_name="Tuesday",
    strike_range=(30000, 65000),
)

SENSEX = IndexConfig(
    name="SENSEX",
    display_name="SENSEX",
    yahoo_symbol="^BSESN",
    expiry_weekday=3,       # Thursday
    weekly_expiry=True,
    strike_interval=100,
    lot_size=20,
    exchange="BSE",
    nse_deriv_param="",
    expiry_day_name="Thursday",
    strike_range=(50000, 110000),
)

INDEX_REGISTRY: Dict[str, IndexConfig] = {
    "NIFTY": NIFTY,
    "BANKNIFTY": BANKNIFTY,
    "SENSEX": SENSEX,
}

DEFAULT_INDEX = "NIFTY"


def get_index(name: str) -> Optional[IndexConfig]:
    """Look up an index config by name (case-insensitive)."""
    return INDEX_REGISTRY.get(name.upper())


def list_indices() -> list:
    """Return list of supported index names."""
    return list(INDEX_REGISTRY.keys())
