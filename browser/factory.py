"""
Broker Factory

Returns the appropriate broker instance based on the BROKER env variable.

Usage:
    from browser.factory import create_broker
    kite = create_broker()      # returns ZerodhaKite or DhanBroker

Supported values for BROKER in .env:
    zerodha  (default) — uses Kite Connect API + Playwright fallback
    dhan     — uses Dhan free API, no monthly fees, no browser needed
"""

from config import settings


def create_broker():
    """Instantiate and return the broker configured via BROKER env var."""
    broker = (settings.broker or "zerodha").lower().strip()

    if broker == "dhan":
        from browser.dhan import DhanBroker
        return DhanBroker()

    # Default: Zerodha / Kite Connect
    from browser.zerodha import ZerodhaKite
    return ZerodhaKite()
