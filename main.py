"""
NIFTY Options Trading Bot - Main Entry Point

Usage:
    python main.py              # Start the web server
    python main.py --analyze    # Run single analysis
    python main.py --test       # Test browser automation
"""

import argparse
import asyncio
import sys
from loguru import logger

# Configure logging
logger.remove()
logger.add(
    sys.stdout,
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>",
    level="INFO"
)
logger.add(
    "trading_bot.log",
    rotation="1 day",
    retention="7 days",
    level="DEBUG"
)


def run_server():
    """Run the web server"""
    import uvicorn
    from config import settings
    import threading
    import webbrowser
    import socket

    # ── Single-instance guard ──────────────────────────────────────────────
    # Try binding to the bot port before starting uvicorn.  If it's already
    # in use, another bot instance is running — print a clear message and exit
    # rather than silently spawning a second process that could place duplicate
    # trades or double-close positions.
    _test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _test_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    try:
        _test_sock.bind((settings.web.host, settings.web.port))
        _test_sock.close()
    except OSError:
        _test_sock.close()
        logger.error(
            f"Port {settings.web.port} is already in use — another bot instance is running!\n"
            f"Stop the existing bot first (Stop-Process -Name python) then restart."
        )
        sys.exit(1)

    logger.info("Starting NIFTY Trading Bot Web Server...")
    logger.info(f"Open http://{settings.web.host}:{settings.web.port} in your browser")

    def _open_browser():
        try:
            webbrowser.open(f"http://{settings.web.host}:{settings.web.port}")
        except Exception as e:
            logger.warning(f"Could not open browser: {e}")

    threading.Timer(1.0, _open_browser).start()
    
    # Suppress favicon noise from uvicorn access log
    import logging
    class _FaviconFilter(logging.Filter):
        def filter(self, record):
            return "favicon.ico" not in record.getMessage()
    logging.getLogger("uvicorn.access").addFilter(_FaviconFilter())

    uvicorn.run(
        "web.app:app",
        host=settings.web.host,
        port=settings.web.port,
        reload=False,
        log_level="info",
        access_log=True,
    )


def run_analysis():
    """Run a single trend analysis"""
    from bot.trend_analyzer import TrendAnalyzer
    
    logger.info("Running trend analysis...")
    
    analyzer = TrendAnalyzer()
    signal = analyzer.analyze()
    
    if signal:
        print(analyzer.format_analysis_report(signal))
    else:
        print("Analysis failed - check market hours and data availability")


async def test_browser():
    """Test browser automation"""
    from browser.zerodha import ZerodhaKite
    
    logger.info("Testing browser automation...")
    
    kite = ZerodhaKite()
    
    try:
        await kite.initialize(headless=False)
        print("\nBrowser launched successfully!")
        print("Please login to Zerodha Kite manually...")
        
        success = await kite.login(wait_for_2fa=True)
        
        if success:
            print("\nLogin successful!")
            await kite.get_screenshot("test_screenshot.png")
            print("Screenshot saved to test_screenshot.png")
            
            input("\nPress Enter to close browser...")
        else:
            print("\nLogin failed or timed out")
            
    except Exception as e:
        logger.error(f"Test failed: {e}")
    finally:
        await kite.close()


def main():
    parser = argparse.ArgumentParser(
        description="NIFTY Options Trading Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py              Start the web server (default)
  python main.py --analyze    Run single trend analysis
  python main.py --test       Test browser automation
        """
    )
    
    parser.add_argument(
        "--analyze", "-a",
        action="store_true",
        help="Run a single trend analysis and exit"
    )
    
    parser.add_argument(
        "--test", "-t",
        action="store_true",
        help="Test browser automation"
    )
    
    parser.add_argument(
        "--host",
        default=None,
        help="Override web server host"
    )
    
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=None,
        help="Override web server port"
    )
    
    args = parser.parse_args()
    
    # Override settings if provided
    if args.host or args.port:
        from config import settings
        if args.host:
            settings.web.host = args.host
        if args.port:
            settings.web.port = args.port
    
    # Execute based on mode
    if args.analyze:
        run_analysis()
    elif args.test:
        asyncio.run(test_browser())
    else:
        run_server()


if __name__ == "__main__":
    main()
