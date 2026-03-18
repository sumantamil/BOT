"""
Configuration settings for NIFTY Options Trading Bot

IMPORTANT: Never commit this file with real credentials to version control!
Create a .env file with your actual credentials instead.
"""

import os
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from typing import Optional


class ZerodhaConfig(BaseSettings):
    """Zerodha Kite credentials and settings"""
    
    user_id: str = Field(default="", description="Your Zerodha User ID")
    password: str = Field(default="", description="Your Zerodha Password")
    totp_secret: Optional[str] = Field(default=None, description="TOTP secret for auto 2FA (optional)")
    
    # Kite URLs
    login_url: str = "https://kite.zerodha.com/"
    
    model_config = SettingsConfigDict(
        env_prefix="ZERODHA_",
        env_file=".env",
        extra="ignore"
    )


class KiteAPIConfig(BaseSettings):
    """Kite Connect API configuration (recommended for auto-trading)"""
    
    api_key: str = Field(default="", description="Kite API Key from https://kite.trade/")
    api_secret: str = Field(default="", description="Kite API Secret")
    access_token: str = Field(default="", description="Access token (obtained after login)")
    use_kite_api: bool = Field(default=True, description="Use Kite API instead of browser automation")
    
    model_config = SettingsConfigDict(
        env_prefix="KITE_",
        env_file=".env",
        extra="ignore"
    )


class DhanConfig(BaseSettings):
    """Dhan API configuration (free alternative to Kite Connect)"""

    client_id: str = Field(default="", description="Dhan Client ID from https://dhanhq.co/")
    access_token: str = Field(default="", description="Dhan permanent access token")
    mobile: str = Field(default="", description="Registered mobile number (used by get_dhan_token.py for OTP login)")
    password: str = Field(default="", description="Dhan account password (legacy, kept for reference)")
    portal_url: str = Field(
        default="https://developer.dhanhq.co",
        description="Dhan developer portal URL",
    )

    model_config = SettingsConfigDict(
        env_prefix="DHAN_",
        env_file=".env",
        extra="ignore"
    )


class TradingConfig(BaseSettings):
    """Trading parameters and risk management"""
    
    # Default lot size — matches NIFTY lot size; bot also auto-resolves via index_config.lot_size
    default_quantity: int = Field(default=65, description="Number of units per trade (NIFTY lot size)")
    
    # Maximum positions at any time
    max_positions: int = Field(default=2, description="Maximum open positions (supports 2 simultaneous: 1 ORB + 1 VWAP)")
    
    # Risk management — calibrated for ₹45,000 capital
    # max_loss_per_trade = 5% of capital  (20% SL on ₹6,500 NIFTY lot ≈ ₹1,300 — well within limit)
    # max_daily_loss    = 10% of capital  (~3 SL hits before bot halts for the day)
    max_loss_per_trade: float = Field(default=2250.0, description="Max loss per trade in INR (5%% of ₹45k capital)")
    max_daily_loss: float = Field(default=4500.0, description="Max daily loss in INR (10%% of ₹45k capital)")
    stop_loss_percentage: float = Field(default=20.0, description="Stop loss percentage")
    target_percentage: float = Field(default=30.0, description="Target profit percentage")
    
    # Trailing stop loss
    use_trailing_stop_loss: bool = Field(default=False, description="Enable trailing stop loss")
    trailing_stop_percentage: float = Field(default=10.0, description="Trailing stop loss percentage (distance from highest price)")
    trailing_stop_amount: float = Field(default=500.0, description="Trailing stop loss amount in INR (alternative to percentage)")
    use_trailing_stop_amount: bool = Field(default=False, description="Use amount-based trailing stop instead of percentage")
    # Trailing stop only activates once the position is this far into profit.
    # Prevents premature arming on tiny ticks; fixed SL guards until threshold is reached.
    trailing_stop_activation_pct: float = Field(default=10.0, description="Min profit %% before trailing stop arms (e.g. 10 = trail only after +10%%)")
    
    # Trading hours (IST)
    market_open_hour: int = 9
    market_open_minute: int = 15
    market_close_hour: int = 15
    market_close_minute: int = 30
    
    # Safety: Market close protection
    close_all_before_market_close: int = Field(default=3, description="Close all trades N minutes before market close")
    
    # Safety: Consecutive losses & cool-off
    max_consecutive_losses: int = Field(default=3, description="Maximum consecutive losses before pausing")
    pause_after_losses_minutes: int = Field(default=30, description="Minutes to pause trading after consecutive losses")
    
    # Safety: Daily trade limit — 3 max for ₹45k capital (3 SL hits ≈ ₹3,900, near daily limit)
    max_trades_per_day: int = Field(default=3, description="Maximum number of trades per day")
    min_time_between_trades_minutes: int = Field(default=5, description="Minimum time between consecutive trades")
    
    # Phase 2: Profit taking tiers (partial exits)
    use_profit_tiers: bool = Field(default=False, description="Enable tiered profit-taking (partial exits)")
    take_profit_tier_1_percent: float = Field(default=15.0, description="First tier profit percentage")
    take_profit_tier_1_quantity_percent: float = Field(default=50.0, description="Percentage of position to exit at tier 1")
    take_profit_tier_2_percent: float = Field(default=30.0, description="Second tier profit percentage")
    take_profit_tier_2_quantity_percent: float = Field(default=50.0, description="Percentage of remaining position to exit at tier 2")
    
    # Phase 2: Market condition filters
    minimum_rsi_range: tuple = Field(default=(40, 60), description="RSI range to avoid (uncertain market)")
    avoid_rsi_range: bool = Field(default=False, description="Skip trades when RSI is in uncertain range")
    volatility_threshold: float = Field(default=2.0, description="Skip trading if market volatility exceeds threshold")
    avoid_low_volume_hours: bool = Field(default=False, description="Skip first 30min and last hour of trading")
    
    # GTT (Good Till Triggered) exchange-level stop orders
    # These survive bot crashes, restarts, and internet outages.
    use_gtt: bool = Field(default=True, description="Place GTT stop-loss order on Kite exchange after every entry")

    # AMO (After Market Orders) — place orders outside 9:15–15:30 for strong gap scenarios
    # Dhan AMO window: 17:00–23:59 and 00:00–09:08 on weekdays
    # Uses CNC product type so the order sits in queue and executes at open
    amo_enabled: bool = Field(default=True, description="Allow AMO orders during pre/post-market for gap strategies")

    # Time-stop: exit positions that never move into profit
    # After N minutes open, if peak has never exceeded entry by 2%, exit to free capital.
    # Set 0 to disable.  Default: 45 minutes.
    time_stop_minutes: int = Field(default=45, description="Exit if open > N min with peak ≤ entry+2% (0=disabled)")

    # Auto-trading settings
    auto_trade_enabled: bool = Field(default=False, description="Enable automatic trading")
    
    model_config = SettingsConfigDict(
        env_prefix="TRADING_",
        env_file=".env",
        extra="ignore"
    )


class TrendConfig(BaseSettings):
    """Technical analysis parameters"""
    
    # Moving averages
    sma_short_period: int = Field(default=20, description="Short SMA period")
    sma_long_period: int = Field(default=50, description="Long SMA period")
    
    # RSI settings
    rsi_period: int = Field(default=14, description="RSI calculation period")
    rsi_oversold: float = Field(default=30.0, description="RSI oversold threshold")
    rsi_overbought: float = Field(default=70.0, description="RSI overbought threshold")
    
    # MACD settings
    macd_fast: int = Field(default=12, description="MACD fast period")
    macd_slow: int = Field(default=26, description="MACD slow period")
    macd_signal: int = Field(default=9, description="MACD signal period")
    
    # Analysis interval
    analysis_interval_seconds: int = Field(default=60, description="Trend analysis interval in seconds")
    
    # Data fetch settings
    data_period: str = Field(default="5d", description="Historical data period for analysis")
    data_interval: str = Field(default="5m", description="Data interval (1m, 5m, 15m, etc.)")
    
    model_config = SettingsConfigDict(
        env_prefix="TREND_",
        env_file=".env",
        extra="ignore"
    )


class AlertConfig(BaseSettings):
    """Phase 3: Notifications and alerts configuration"""
    
    # Telegram Bot (primary)
    telegram_enabled: bool = Field(default=False, description="Enable Telegram notifications")
    telegram_bot_token: str = Field(default="", description="Telegram Bot API token")
    telegram_chat_id: str = Field(default="", description="Telegram chat ID for notifications")

    # Telegram Bot (secondary — optional second channel/user)
    telegram2_enabled: bool = Field(default=False, description="Enable secondary Telegram notifications")
    telegram2_bot_token: str = Field(default="", description="Secondary Telegram Bot API token (can reuse primary bot)")
    telegram2_chat_id: str = Field(default="", description="Secondary Telegram chat ID for notifications")
    
    # Email
    email_enabled: bool = Field(default=False, description="Enable email notifications")
    email_smtp_server: str = Field(default="smtp.gmail.com", description="SMTP server")
    email_smtp_port: int = Field(default=587, description="SMTP port")
    email_sender: str = Field(default="", description="Sender email address")
    email_password: str = Field(default="", description="Email password / app password")
    email_recipient: str = Field(default="", description="Recipient email address")
    
    # Alert triggers
    alert_on_trade_entry: bool = Field(default=True, description="Alert when trade opens")
    alert_on_trade_exit: bool = Field(default=True, description="Alert when trade closes")
    alert_on_daily_loss_limit: bool = Field(default=True, description="Alert when daily loss limit hit")
    alert_on_consecutive_losses: bool = Field(default=True, description="Alert when pause activated")
    alert_on_analysis: bool = Field(default=False, description="Alert every analysis (verbose)")
    
    # Daily report
    send_daily_report: bool = Field(default=True, description="Send daily report at market close")
    daily_report_time: str = Field(default="15:30", description="Time to send daily report (HH:MM IST)")
    
    model_config = SettingsConfigDict(
        env_prefix="ALERT_",
        env_file=".env",
        extra="ignore"
    )


class VWAPConfig(BaseSettings):
    """VWAP Mean Reversion strategy configuration (active in RANGING regime)"""

    enabled: bool = Field(default=True, description="Enable VWAP mean-reversion strategy")
    deviation_pct: float = Field(default=0.6, description="% deviation from VWAP required to trigger signal (0.4 was too small — caught normal intraday noise at NIFTY 23k=92pts)")
    rsi_oversold: float = Field(default=38.0, description="RSI threshold for LONG signal (dip) — 42 was too loose, caught near-neutral readings")
    rsi_overbought: float = Field(default=62.0, description="RSI threshold for SHORT signal (pop) — 58 was too loose")
    stop_pct: float = Field(default=0.3, description="Stop loss % from entry price")

    model_config = SettingsConfigDict(
        env_prefix="VWAP_",
        env_file=".env",
        extra="ignore"
    )


class ORBConfig(BaseSettings):
    """Opening Range Breakout strategy configuration"""

    enabled: bool = Field(default=True, description="Enable ORB strategy")
    window_minutes: int = Field(default=15, description="Opening range window in minutes (from 9:15 AM)")
    entry_end_hour: int = Field(default=11, description="No new ORB entries after this hour (IST)")
    entry_end_minute: int = Field(default=30, description="No new ORB entries after this minute (IST)")
    target_multiplier: float = Field(default=1.5, description="Target 1 = range_width x multiplier")
    target_multiplier_2: float = Field(default=2.0, description="Target 2 = range_width x multiplier_2")
    volume_confirmation: bool = Field(default=True, description="Require volume spike (1.2x avg) to confirm breakout")
    breakout_buffer_pct: float = Field(default=0.15, description="% buffer above/below range boundary to confirm breakout")

    model_config = SettingsConfigDict(
        env_prefix="ORB_",
        env_file=".env",
        extra="ignore"
    )


class GapConfig(BaseSettings):
    """Gap up/down detection and trading strategy configuration"""

    enabled: bool = Field(default=True, description="Enable gap detection strategy")
    min_gap_pct: float = Field(default=0.75, description="Minimum gap % to trigger moderate gap trade")
    strong_gap_pct: float = Field(default=1.5, description="Gap % to trigger immediate (no-confirmation) entry")
    stop_loss_pct: float = Field(default=25.0, description="Stop loss % for gap trades (wider than normal, gaps are volatile)")
    target_pct: float = Field(default=50.0, description="Target % for gap trades (bigger reward for gap-and-go)")
    quantity_multiplier: float = Field(default=1.0, description="Position size multiplier for strong gaps (1.0 = normal size)")

    model_config = SettingsConfigDict(
        env_prefix="GAP_",
        env_file=".env",
        extra="ignore"
    )


class EODConfig(BaseSettings):
    """End-of-Day closing momentum strategy (fires once after 14:30 IST).

    After 2:30 PM institutional traders square off positions. The direction
    of the last completed 15-minute candle reliably predicts where the index
    will close. A strong (non-doji) candle → buy CE or PE in that direction.
    The trade runs until the 15:27 force-exit.
    """

    enabled: bool = Field(default=True, description="Enable EOD closing momentum strategy")
    entry_start_hour: int = Field(default=14, description="Start scanning for EOD signal (IST hour)")
    entry_start_minute: int = Field(default=30, description="Start scanning for EOD signal (IST minute)")
    entry_end_hour: int = Field(default=15, description="Stop taking new EOD entries (IST hour)")
    entry_end_minute: int = Field(default=0, description="Stop taking new EOD entries (IST minute)")
    min_body_pct: float = Field(
        default=0.5,
        description="Min candle body as fraction of high-low range (0.5 = 50%). "
                    "Below this it is a doji/indecision candle — no trade taken."
    )

    model_config = SettingsConfigDict(
        env_prefix="EOD_",
        env_file=".env",
        extra="ignore"
    )


class WebConfig(BaseSettings):
    """Web server configuration"""
    
    host: str = Field(default="127.0.0.1", description="Server host")
    port: int = Field(default=8000, description="Server port")
    
    model_config = SettingsConfigDict(
        env_prefix="WEB_",
        env_file=".env",
        extra="ignore"
    )


class Settings(BaseSettings):
    """Main settings class combining all configurations"""

    zerodha: ZerodhaConfig = ZerodhaConfig()
    kite_api: KiteAPIConfig = KiteAPIConfig()
    dhan: DhanConfig = DhanConfig()
    broker: str = Field(default="zerodha", description="Active broker: 'zerodha' or 'dhan'")
    trading: TradingConfig = TradingConfig()
    trend: TrendConfig = TrendConfig()
    vwap: VWAPConfig = VWAPConfig()
    orb: ORBConfig = ORBConfig()
    gap: GapConfig = GapConfig()
    eod: EODConfig = EODConfig()
    web: WebConfig = WebConfig()
    alert: AlertConfig = AlertConfig()
    
    # Logging
    log_level: str = Field(default="INFO", description="Logging level")
    log_file: str = Field(default="trading_bot.log", description="Log file path")
    
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore"
    )


# Global settings instance
settings = Settings()


# Example .env file content (for reference)
ENV_TEMPLATE = """
# ============ BROKER SELECTION ============
# Set to 'zerodha' or 'dhan'
BROKER=zerodha

# ============ ZERODHA CREDENTIALS (if BROKER=zerodha) ============
ZERODHA_USER_ID=your_user_id
ZERODHA_PASSWORD=your_password
ZERODHA_TOTP_SECRET=your_totp_secret_optional

# ============ DHAN API (if BROKER=dhan) ============
# Sign up at https://dhanhq.co/ — free, permanent access token
DHAN_CLIENT_ID=your_dhan_client_id
DHAN_ACCESS_TOKEN=your_dhan_permanent_access_token

# Trading Settings — calibrated for ₹45,000 capital
# max_loss_per_trade = 5% of capital  |  max_daily_loss = 10% of capital  |  max_trades = 3/day
TRADING_DEFAULT_QUANTITY=65
TRADING_MAX_POSITIONS=2
TRADING_MAX_LOSS_PER_TRADE=2250
TRADING_MAX_DAILY_LOSS=4500
TRADING_STOP_LOSS_PERCENTAGE=20
TRADING_TARGET_PERCENTAGE=30
TRADING_AUTO_TRADE_ENABLED=false

# Trailing Stop Loss
TRADING_USE_TRAILING_STOP_LOSS=true
TRADING_TRAILING_STOP_PERCENTAGE=10           # trail distance: 10% below peak
TRADING_TRAILING_STOP_ACTIVATION_PCT=10       # arm only after +10% profit
TRADING_USE_TRAILING_STOP_AMOUNT=false
TRADING_TRAILING_STOP_AMOUNT=500

# Trend Analysis Settings
TREND_SMA_SHORT_PERIOD=20
TREND_SMA_LONG_PERIOD=50
TREND_RSI_PERIOD=14
TREND_ANALYSIS_INTERVAL_SECONDS=60

# Web Server
WEB_HOST=127.0.0.1
WEB_PORT=8000

# Safety Features (Phase 1)
TRADING_CLOSE_ALL_BEFORE_MARKET_CLOSE=3
TRADING_MAX_CONSECUTIVE_LOSSES=3
TRADING_PAUSE_AFTER_LOSSES_MINUTES=30
TRADING_MAX_TRADES_PER_DAY=3
TRADING_MIN_TIME_BETWEEN_TRADES_MINUTES=5

# Profit Taking Tiers (Phase 2)
TRADING_USE_PROFIT_TIERS=false
TRADING_TAKE_PROFIT_TIER_1_PERCENT=15
TRADING_TAKE_PROFIT_TIER_1_QUANTITY_PERCENT=50
TRADING_TAKE_PROFIT_TIER_2_PERCENT=30
TRADING_TAKE_PROFIT_TIER_2_QUANTITY_PERCENT=50

# Market Condition Filters (Phase 2)
TRADING_AVOID_RSI_RANGE=false
TRADING_VOLATILITY_THRESHOLD=2.0
TRADING_AVOID_LOW_VOLUME_HOURS=false

# Logging
LOG_LEVEL=INFO
"""


def create_env_template():
    """Create a template .env file if it doesn't exist"""
    env_path = os.path.join(os.path.dirname(__file__), ".env.template")
    if not os.path.exists(env_path):
        with open(env_path, "w") as f:
            f.write(ENV_TEMPLATE.strip())
        print(f"Created .env template at: {env_path}")


if __name__ == "__main__":
    create_env_template()
    print("Current settings:")
    print(f"  Zerodha User ID: {'*' * len(settings.zerodha.user_id) if settings.zerodha.user_id else 'Not set'}")
    print(f"  Default Quantity: {settings.trading.default_quantity}")
    print(f"  Auto Trade: {settings.trading.auto_trade_enabled}")
    print(f"  Web Server: {settings.web.host}:{settings.web.port}")
