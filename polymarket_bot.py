import asyncio
import aiohttp
import json
import time
import os
from datetime import datetime

STATE_FILE = "bot_state.json"
BASE_SHARES = 10
TARGET_EXTRA_PROFIT = 2.0          # ← Edit this for your "some profit" on flips
POLL_INTERVAL = 0.2                # 200ms polling = millisecond reaction

class BotState:
    def __init__(self):
        self.capital = 1000.0
        self.last_window_ts = None
        self.current_side = None      # 'up' or 'down' or None
        self.up_shares = 0.0
        self.down_shares = 0.0
        self.up_cost = 0.0
        self.down_cost = 0.0

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
            state = BotState()
            state.capital = data.get("capital", 1000.0)
            state.last_window_ts = data.get("last_window_ts")
            state.current_side = data.get("current_side")
            state.up_shares = data.get("up_shares", 0.0)
            state.down_shares = data.get("down_shares", 0.0)
            state.up_cost = data.get("up_cost", 0.0)
            state.down_cost = data.get("down_cost", 0.0)
            return state
    return BotState()

def save_state(state):
    data = {
        "capital": round(state.capital, 4),
        "last_window_ts": state.last_window_ts,
        "current_side": state.current_side,
        "up_shares": state.up_shares,
        "down_shares": state.down_shares,
        "up_cost": state.up_cost,
        "down_cost": state.down_cost,
    }
    with open(STATE_FILE, "w") as f:
        json.dump(data, f, indent=2)

async def fetch_event(session, slug):
    url = f"https://gamma-api.polymarket.com/events?slug={slug}"
    try:
        async with session.get(url, timeout=5) as resp:
            if resp.status == 200:
                return await resp.json()
    except:
        pass
    return None

def get_prices_and_status(event_data):
    if not event_data or not isinstance(event_data, list) or len(event_data) == 0:
        return None, None, None, None, True
    event = event_data[0]
    market = event.get("markets", [event])[0]
    closed = market.get("closed", event.get("closed", False))
    
    # clobTokenIds (as string per your spec)
    clob_str = market.get("clobTokenIds", event.get("clobTokenIds", "[]"))
    if isinstance(clob_str, str):
        clob_ids = json.loads(clob_str)
    else:
        clob_ids = clob_str
    up_id = clob_ids[0] if clob_ids else None
    down_id = clob_ids[1] if len(clob_ids) > 1 else None
    
    # prices from outcomePrices
    if "outcomePrices" in market:
        prices_str = json.loads(market["outcomePrices"])
        up_price = float(prices_str[0])
        down_price = float(prices_str[1])
    else:
        up_price = down_price = 0.5
    
    return up_price, down_price, closed, up_id, down_id

async def main():
    state = load_state()
    print(f"🚀 BTC 5m Bot started | Capital: ${state.capital:.2f}")
    
    async with aiohttp.ClientSession() as session:
        while True:
            now_ts = (int(time.time()) // 300) * 300
            slug = f"btc-updown-5m-{now_ts}"
            
            event_data = await fetch_event(session, slug)
            if not event_data:
                await asyncio.sleep(POLL_INTERVAL)
                continue
            
            up_price, down_price, closed, up_id, down_id = get_prices_and_status(event_data)
            
            # New window → reset positions
            if now_ts != state.last_window_ts:
                if state.last_window_ts is not None:
                    print(f"✅ Window {state.last_window_ts} ended. Capital now: ${state.capital:.2f}")
                state.last_window_ts = now_ts
                state.current_side = None
                state.up_shares = state.down_shares = 0.0
                state.up_cost = state.down_cost = 0.0
                save_state(state)
                print(f"🌟 NEW 5m WINDOW: {slug} | Up {up_price:.2f} / Down {down_price:.2f}")
            
            if closed:
                # Resolve P&L
                up_wins = up_price >= 0.999
                payout = (state.up_shares if up_wins else state.down_shares) * 1.0
                state.capital += payout
                save_state(state)
                print(f"🏁 WINDOW RESOLVED → {'UP' if up_wins else 'DOWN'} wins | Payout ${payout:.2f} | Capital ${state.capital:.2f}")
                await asyncio.sleep(2)  # wait for next window
                continue
            
            # === YOUR STRATEGY LOGIC ===
            if state.current_side is None:
                # First entry: whichever hits 60¢ first
                if up_price >= 0.60:
                    cost = BASE_SHARES * up_price
                    if state.capital >= cost:
                        state.up_shares += BASE_SHARES
                        state.up_cost += cost
                        state.capital -= cost
                        state.current_side = "up"
                        save_state(state)
                        print(f"📈 FIRST BUY UP 10 shares @ {up_price:.2f} | Cost ${cost:.2f} | Capital ${state.capital:.2f}")
                elif down_price >= 0.60:
                    cost = BASE_SHARES * down_price
                    if state.capital >= cost:
                        state.down_shares += BASE_SHARES
                        state.down_cost += cost
                        state.capital -= cost
                        state.current_side = "down"
                        save_state(state)
                        print(f"📉 FIRST BUY DOWN 10 shares @ {down_price:.2f} | Cost ${cost:.2f} | Capital ${state.capital:.2f}")
            
            elif state.current_side == "up":
                if up_price <= 0.40 and down_price >= 0.60:
                    prev_loss = state.up_cost
                    needed = prev_loss + TARGET_EXTRA_PROFIT
                    new_p = down_price
                    recovery_shares = int((needed / (1 - new_p)) + 0.999)  # ceil
                    cost = recovery_shares * new_p
                    if state.capital >= cost:
                        state.down_shares += recovery_shares
                        state.down_cost += cost
                        state.capital -= cost
                        state.current_side = "down"
                        save_state(state)
                        print(f"🔄 FLIP to DOWN {recovery_shares} shares @ {new_p:.2f} | Cost ${cost:.2f} | Recovers ${prev_loss:.2f} + ${TARGET_EXTRA_PROFIT} profit | Capital ${state.capital:.2f}")
            
            elif state.current_side == "down":
                if down_price <= 0.40 and up_price >= 0.60:
                    prev_loss = state.down_cost
                    needed = prev_loss + TARGET_EXTRA_PROFIT
                    new_p = up_price
                    recovery_shares = int((needed / (1 - new_p)) + 0.999)
                    cost = recovery_shares * new_p
                    if state.capital >= cost:
                        state.up_shares += recovery_shares
                        state.up_cost += cost
                        state.capital -= cost
                        state.current_side = "up"
                        save_state(state)
                        print(f"🔄 FLIP to UP {recovery_shares} shares @ {new_p:.2f} | Cost ${cost:.2f} | Recovers ${prev_loss:.2f} + ${TARGET_EXTRA_PROFIT} profit | Capital ${state.capital:.2f}")
            
            await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())
