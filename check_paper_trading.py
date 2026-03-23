import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

print("\n" + "="*70)
print("  PAPER TRADING STATUS CHECK")
print("="*70 + "\n")

# CHECK 1: .env file exists?
env_file = Path(".env")
if not env_file.exists():
    print("❌ CRITICAL: .env file NOT FOUND")
    print("   📝 Create .env file in project root")
    sys.exit(1)
else:
    print("✅ .env file found")

# CHECK 2: Paper trading setting
print("\n📋 Paper Trading Configuration:")
print("-" * 70)

auto_trade = os.getenv('TRADING_AUTO_TRADE_ENABLED', 'NOT_SET')

print(f"\nTRADING_AUTO_TRADE_ENABLED = {auto_trade}")

if auto_trade == 'NOT_SET':
    print("\n❌ CRITICAL: TRADING_AUTO_TRADE_ENABLED not set in .env")
    print("\n💡 ADD THIS TO .env:")
    print("   TRADING_AUTO_TRADE_ENABLED=false")
    print("\n   false = Paper trading (SAFE - no real trades)")
    print("   true  = Live trading (DANGER - real money!)")
    
elif auto_trade.lower() == 'true':
    print("\n⚠️  WARNING: LIVE TRADING MODE ENABLED!")
    print("   Bot will place REAL trades with REAL money")
    print("\n💡 TO ENABLE PAPER TRADING:")
    print("   Change in .env: TRADING_AUTO_TRADE_ENABLED=false")
    
elif auto_trade.lower() == 'false':
    print("\n✅ PAPER TRADING MODE ENABLED")
    print("   Bot will simulate trades (no real money)")
    
else:
    print(f"\n❌ INVALID VALUE: '{auto_trade}'")
    print("   Must be 'true' or 'false'")

# CHECK 3: Verify bot code respects this setting
print("\n\n📋 Code Verification:")
print("-" * 70)

order_manager_file = Path("bot/order_manager.py")

if not order_manager_file.exists():
    print("❌ bot/order_manager.py NOT FOUND")
else:
    print("✅ bot/order_manager.py found")
    
    with open(order_manager_file, 'r') as f:
        content = f.read()
        
        # Check if code checks auto_trade setting
        if 'TRADING_AUTO_TRADE_ENABLED' in content or 'AUTO_TRADE' in content:
            print("✅ Code checks TRADING_AUTO_TRADE_ENABLED setting")
        else:
            print("❌ Code does NOT check TRADING_AUTO_TRADE_ENABLED")
            print("   Paper trading may not be implemented!")
        
        # Check for paper trade logging
        if 'paper' in content.lower() or 'simulation' in content.lower():
            print("✅ Paper trading logic found in code")
        else:
            print("⚠️  No paper trading logic found")
            print("   Paper trades may not be logged/tracked")

# SUMMARY
print("\n\n" + "="*70)
print("  SUMMARY")
print("="*70 + "\n")

if auto_trade == 'NOT_SET':
    print("🔴 PAPER TRADING: NOT CONFIGURED")
    print("\nAction needed: Add TRADING_AUTO_TRADE_ENABLED=false to .env")
    
elif auto_trade.lower() == 'true':
    print("🔴 PAPER TRADING: DISABLED (Live trading active!)")
    print("\nAction needed: Change to TRADING_AUTO_TRADE_ENABLED=false")
    
elif auto_trade.lower() == 'false':
    print("🟢 PAPER TRADING: ENABLED")
    print("\nBot will simulate trades without real money")
    
    # Additional check
    if order_manager_file.exists():
        with open(order_manager_file, 'r') as f:
            if 'TRADING_AUTO_TRADE_ENABLED' not in f.read():
                print("\n⚠️  BUT: order_manager.py may not implement it properly")
                print("   Run next prompt to add paper trading code")
else:
    print("🔴 PAPER TRADING: INVALID CONFIGURATION")
    print(f"\nCurrent value: {auto_trade}")
    print("Must be 'true' or 'false'")

print("\n" + "="*70 + "\n")
