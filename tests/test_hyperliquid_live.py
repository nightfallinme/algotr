
import os
import sys
import time
from dotenv import load_dotenv

# Add project root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hyperbot.logger import setup_logging, get_logger
from hyperbot.exchanges.hyperliquid_exchange import HyperliquidExchange

def test_hyperliquid_live():
    # Load env (credentials)
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env'))
    
    setup_logging()
    log = get_logger("test_live")
    
    # Configuration
    config = {
        "exchange": "hyperliquid",
        "symbol": "ETH",
        "leverage": 5,
        "slippage_pct": 0.05,
        "secret_key": os.getenv("HL_SECRET_KEY"),
        "account_address": os.getenv("HL_ACCOUNT_ADDRESS")
    }
    
    exchange = HyperliquidExchange(config)
    
    print("\n" + "="*50)
    print("🚀 STARTING HYPERLIQUID LIVE TEST")
    print("⚠️  WARNING: THIS WILL OPEN REAL POSITIONS")
    print("="*50 + "\n")
    
    # 1. Connect
    print("1. [CONNECT] Connecting...")
    if not exchange.connect():
        print("❌ Failed to connect")
        return
    print("✅ Connected!")
    
    # 2. Balance
    balance = exchange.get_balance()
    print(f"2. [BALANCE] Current Balance: ${balance:.2f}")
    
    if balance < 5: 
        print("❌ Insufficient balance for test (Less than $5)")
        return

    # 3. Enter Position (Buy 0.004 ETH ~ $14 depending on price)
    # Minimum trade size on HL is usually $10 USD equivalent
    size = 0.004  # Safe small size for ~$7 balance x5 leverage
    
    print(f"3. [ENTRY] Opening LONG position for {size} ETH...")
    res = exchange.market_order("ETH", "buy", size)
    
    if res.get("status") == "ok":
        print(f"✅ Order Placed! Details: {res}")
    else:
        print(f"❌ Order Failed: {res.get('message', res)}")
        return

    # Wait for fill
    time.sleep(2)
    
    # 4. Check Position
    print("4. [POSITION] Checking open position...")
    pos = exchange.get_position("ETH")
    if pos:
        print(f"✅ Position Found: {pos['size']} ETH @ ${pos['entry_price']:.2f}")
    else:
        print("❌ No position found!")
        return
        
    entry_price = pos['entry_price']
    
    # 5. Set SL/TP
    # SL at -1% , TP at +1%
    sl = round(entry_price * 0.99, 1)
    tp = round(entry_price * 1.01, 1)
    
    print(f"5. [SL/TP] Setting SL: {sl}, TP: {tp}...")
    res_sl_tp = exchange.set_sl_tp("ETH", sl, tp)
    print(f"ℹ️  Result: {res_sl_tp}")
    
    # Verify open orders
    open_orders = exchange.get_open_orders("ETH")
    print(f"ℹ️  Open Orders Count: {len(open_orders)}")
    for o in open_orders:
        print(f"   -> {o.get('orderType')} Price: {o.get('limitPx') or o.get('triggerCondition')}")

    print("⏳ Waiting 5 seconds...")
    time.sleep(5)
    
    # 6. Close Position
    print("6. [CLOSE] Closing position...")
    res_close = exchange.close_position("ETH")
    print(f"ℹ️  Result: {res_close}")
    
    time.sleep(2)
    
    # Verify Closed
    pos_final = exchange.get_position("ETH")
    if not pos_final:
        print("✅ Position successfully closed!")
    else:
        print(f"⚠️  Position still open: {pos_final}")
        
    print("\n" + "="*50)
    print("🎉 TEST COMPLETED")
    print("="*50 + "\n")

if __name__ == "__main__":
    test_hyperliquid_live()
