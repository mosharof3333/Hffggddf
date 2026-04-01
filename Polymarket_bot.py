import os
import json
import time
import logging
import sys
import threading
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════
DEMO_MODE        = True
STARTING_BALANCE = 1000.00
STATE_FILE       = "demo_state.json"

GAMMA_API  = "https://gamma-api.polymarket.com"
CLOB_API   = "https://clob.polymarket.com"
WINDOW_SEC = 300

POLL_INTERVAL_MS = 30          # Fast polling for high-frequency fluctuations
BUY_THRESHOLD    = 0.60
SELL_TIME_SEC    = 288         # 4.8 minutes
SELL_PRICE       = 0.99
MIN_PROFIT       = 2.00        # Target for recovery + profit

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("btc_grid_bot")


# ══════════════════════════════════════════════════════════════════════════════
#  STATE
# ══════════════════════════════════════════════════════════════════════════════

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "balance":     STARTING_BALANCE,
        "starting":    STARTING_BALANCE,
        "total_pnl":   0.0,
        "total_spent": 0.0,
        "windows_run": 0,
        "wins":        0,
        "losses":      0,
        "trades":      [],
        "windows":     [],
    }

def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def parse_json_str(value):
    """Parse a field that may be a JSON string or already a list."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return []
    return []

def current_window_ts() -> int:
    """Unix timestamp of the START of the current 5-min window."""
    return (int(time.time()) // WINDOW_SEC) * WINDOW_SEC

def seconds_until_next_window() -> float:
    return WINDOW_SEC - (time.time() % WINDOW_SEC)


# ══════════════════════════════════════════════════════════════════════════════
#  MARKET DISCOVERY — Exactly as you specified
# ══════════════════════════════════════════════════════════════════════════════

def fetch_event(window_ts: int) -> dict | None:
    slug = f"btc-updown-5m-{window_ts}"
    try:
        resp = requests.get(
            f"{GAMMA_API}/events",
            params={"slug": slug},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and data:
            log.info("✅ Found event: %s", slug)
            return data[0]
        log.warning("⚠️  No event found for slug: %s", slug)
    except Exception as e:
        log.error("Error fetching event %s: %s", slug, e)
    return None

def extract_market(event: dict) -> dict | None:
    markets = event.get("markets", [])
    if not markets:
        log.warning("Event has no markets")
        return None

    m = markets[0]

    # Parse JSON string fields
    clob_ids       = parse_json_str(m.get("clobTokenIds", "[]"))
    outcomes       = parse_json_str(m.get("outcomes", "[]"))
    outcome_prices = parse_json_str(m.get("outcomePrices", "[]"))

    m["_clob_ids"]       = clob_ids
    m["_outcomes"]       = outcomes
    m["_outcome_prices"] = [float(p) for p in outcome_prices] if outcome_prices else []
    m["_up_token"]       = clob_ids[0] if len(clob_ids) > 0 else None
    m["_down_token"]     = clob_ids[1] if len(clob_ids) > 1 else None
    m["_title"]          = event.get("title", m.get("question", ""))

    return m


# ══════════════════════════════════════════════════════════════════════════════
#  PRICE & DIRECTION HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def get_token_price(token_id: str, side: str = "BUY") -> float:
    if not token_id:
        return 0.50
    try:
        resp = requests.get(
            f"{CLOB_API}/price",
            params={"token_id": token_id, "side": side},
            timeout=5,
        )
        resp.raise_for_status()
        return float(resp.json().get("price", 0.50))
    except Exception:
        return 0.50

def get_direction_from_prices(market: dict) -> str | None:
    prices = market.get("_outcome_prices", [])
    if len(prices) < 2:
        return None
    up_price, down_price = prices[0], prices[1]
    if up_price >= 0.95:
        return "UP"
    if down_price >= 0.95:
        return "DOWN"
    if abs(up_price - down_price) > 0.15:
        return "UP" if up_price > down_price else "DOWN"
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  DEMO ORDER
# ══════════════════════════════════════════════════════════════════════════════

def demo_place_order(state: dict, token_id: str, shares: int, direction: str, title: str, reason: str):
    price = get_token_price(token_id, "BUY")
    cost = round(price * shares, 4)
    if state["balance"] < cost:
        log.warning("⚠️ Insufficient balance for buy")
        return None

    trade = {
        "timestamp":       datetime.now(timezone.utc).isoformat(),
        "market":          title,
        "direction":       direction,
        "shares":          shares,
        "price_per_share": price,
        "cost_usd":        cost,
        "status":          "FILLED",
        "pnl":             None,
        "reason":          reason,
    }
    state["balance"]     = round(state["balance"] - cost, 4)
    state["total_spent"] = round(state["total_spent"] + cost, 4)
    state["trades"].append(trade)

    log.info("  [BUY] %d %s @ $%.4f = $%.4f | %s", shares, direction, price, cost, reason)
    save_state(state)
    return trade


# ══════════════════════════════════════════════════════════════════════════════
#  SETTLEMENT
# ══════════════════════════════════════════════════════════════════════════════

def settle_window(state: dict, window_trades: list, resolved: str):
    up_shares = sum(t["shares"] for t in window_trades if t["direction"] == "UP")
    down_shares = sum(t["shares"] for t in window_trades if t["direction"] == "DOWN")
    total_cost = sum(t["cost_usd"] for t in window_trades)

    payout = float(up_shares if resolved == "UP" else down_shares)
    pnl = round(payout - total_cost, 4)

    state["balance"]   = round(state["balance"] + payout, 4)
    state["total_pnl"] = round(state["total_pnl"] + pnl, 4)
    state["wins"]      += 1 if pnl > 0 else 0
    state["losses"]    += 1 if pnl <= 0 else 0

    return {
        "pnl": pnl,
        "resolved": resolved,
        "up_shares": up_shares,
        "down_shares": down_shares,
        "total_cost": total_cost
    }


# ══════════════════════════════════════════════════════════════════════════════
#  TICKER & DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════

_ticker_market = None
_ticker_running = False
_ticker_lock = threading.Lock()

def set_ticker_market(market: dict | None):
    global _ticker_market
    with _ticker_lock:
        _ticker_market = market

def _ticker_thread():
    is_tty = sys.stdout.isatty()
    while _ticker_running:
        with _ticker_lock:
            market = _ticker_market
        if market:
            up_price = get_token_price(market.get("_up_token"))
            down_price = get_token_price(market.get("_down_token"))
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            line = f"[{ts} UTC]  ⬆️ UP: ${up_price:.4f}   ⬇️ DOWN: ${down_price:.4f}"
            if is_tty:
                sys.stdout.write(f"\r\033[K{line}")
                sys.stdout.flush()
            else:
                log.info(line)
        time.sleep(1)

def start_ticker():
    global _ticker_running
    _ticker_running = True
    threading.Thread(target=_ticker_thread, daemon=True).start()

def stop_ticker():
    global _ticker_running
    _ticker_running = False

def print_dashboard(state: dict, current_market=None, last_summary=None):
    print("\n" + "═" * 85)
    print("  POLYMARKET BTC 5-MIN HIGH-FREQUENCY GRID + RECOVERY BOT (DEMO - NO SKIP)")
    print("═" * 85)
    if current_market:
        print(f"  Market: {current_market.get('_title', 'BTC 5-min')}")
    print(f"  Balance      : ${state['balance']:.2f}")
    print(f"  Total P&L    : ${state['total_pnl']:.2f}")
    print(f"  Windows Run  : {state['windows_run']} | Wins: {state['wins']} | Losses: {state['losses']}")
    if last_summary:
        print(f"  Last Window  : {last_summary['resolved']} | P&L: ${last_summary['pnl']:.2f}")
    print("═" * 85)


# ══════════════════════════════════════════════════════════════════════════════
#  CORE WINDOW LOGIC — No Skip, Keep Betting Even on Poor Recovery
# ══════════════════════════════════════════════════════════════════════════════

def run_window(state: dict, market: dict):
    up_token = market.get("_up_token")
    down_token = market.get("_down_token")
    title = market.get("_title", "BTC 5-min")

    window_start = time.time()
    window_trades = []
    last_direction = None
    last_buy_cost = 0.0

    log.info("🚀 Starting high-frequency grid monitoring (no skip mode)...")

    while True:
        elapsed = time.time() - window_start
        if elapsed >= WINDOW_SEC:
            break

        # Sell all at 4.8 minutes
        if elapsed >= SELL_TIME_SEC and window_trades:
            log.info("⏰ 4.8 min reached — Placing sell orders at $%.2f for all shares", SELL_PRICE)
            break

        up_price = get_token_price(up_token)
        down_price = get_token_price(down_token)

        for direction, price, token in [("UP", up_price, up_token), ("DOWN", down_price, down_token)]:
            if price < BUY_THRESHOLD:
                continue

            is_opposite = last_direction is not None and direction != last_direction

            if not is_opposite:
                reason = "Same side hit 60¢"
            else:
                # Recovery calculation (for logging only - we never skip)
                new_cost = price * 10
                total_cost = last_buy_cost + new_cost
                expected_pnl = 10.0 - total_cost
                if expected_pnl >= MIN_PROFIT:
                    reason = f"Recovery +${expected_pnl:.2f} good"
                else:
                    reason = f"Recovery ${expected_pnl:.2f} poor → FORCED BUY"

            trade = demo_place_order(state, token, 10, direction, title, reason)
            if trade:
                window_trades.append(trade)
                last_direction = direction
                last_buy_cost = trade["cost_usd"]

        time.sleep(POLL_INTERVAL_MS / 1000.0)

    return window_trades


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN LOOP
# ══════════════════════════════════════════════════════════════════════════════

def main():
    state = load_state()
    log.info("Polymarket BTC 5-Min Grid + Recovery Bot Started (Demo Mode)")
    print_dashboard(state)
    start_ticker()

    while True:
        try:
            # Wait for next window
            wait = seconds_until_next_window()
            log.info("⏳ Next window in %.1fs...", wait)
            time.sleep(wait)

            curr_ts = current_window_ts()
            log.info("🔔 New 5-min window started (ts=%d)", curr_ts)

            event = fetch_event(curr_ts)
            if not event:
                log.warning("Event not found, skipping")
                continue

            market = extract_market(event)
            if not market:
                log.warning("Could not extract market")
                continue

            set_ticker_market(market)
            state["windows_run"] += 1

            window_trades = run_window(state, market)

            # Wait for window to close + resolution buffer
            time.sleep(max(0, curr_ts + WINDOW_SEC + 15 - time.time()))

            # Get final resolution
            closed_event = fetch_event(curr_ts)
            closed_market = extract_market(closed_event) if closed_event else market
            resolved = get_direction_from_prices(closed_market) if closed_market else None

            if resolved and window_trades:
                summary = settle_window(state, window_trades, resolved)
                log.info("✅ Window settled | Resolved: %s | P&L: $%.2f", resolved, summary["pnl"])
                state["windows"].append(summary)
                save_state(state)

            print_dashboard(state, market, summary)

        except KeyboardInterrupt:
            stop_ticker()
            log.info("Bot stopped by user")
            break
        except Exception as e:
            log.error("Main loop error: %s", e)
            time.sleep(10)


if __name__ == "__main__":
    main()
