import json
import os
from datetime import datetime, timezone

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False

STRATEGY_DB = "strategies.json"
TRADES_DB = "trades.json"


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
def ts_to_str(ts):
    """Convert a UNIX timestamp (int/float) to a readable string."""
    if ts in (None, 0):
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def deal_type_name(t):
    """MT5 deal type → readable string."""
    return {0: "buy", 1: "sell"}.get(t, str(t))


# ------------------------------------------------------------------
# Strategy database
# ------------------------------------------------------------------
def load_existing_strategies(filepath):
    if not os.path.exists(filepath):
        return []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f).get("strategies", [])
    except (json.JSONDecodeError, IOError):
        return []


def collect_strategies(start_index=1):
    strategies = []
    print("\nEnter your strategies one by one.")
    print("Press ENTER on an empty line to save and exit.\n")

    while True:
        try:
            entry = input(f"Strategy #{start_index + len(strategies)}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nInput interrupted. Saving collected strategies...")
            break
        if entry == "":
            break
        strategies.append(entry)

    return strategies


def save_strategies(filepath, new_strategies):
    existing = load_existing_strategies(filepath)
    seen = {s.lower() for s in existing}
    added, skipped = [], []

    for strategy in new_strategies:
        if strategy.lower() in seen:
            skipped.append(strategy)
        else:
            existing.append(strategy)
            seen.add(strategy.lower())
            added.append(strategy)

    payload = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "count": len(existing),
        "strategies": existing,
    }
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=4, ensure_ascii=False)

    return existing, added, skipped


def strategy_step():
    existing = load_existing_strategies(STRATEGY_DB)

    if existing:
        print(f"\nFound existing strategy database with {len(existing)} entries.")
        answer = input("Do you want to modify the strategy database? (y/n): ").strip().lower()
        if answer not in ("y", "yes"):
            print("Skipping strategy update.")
            return

    strategies = collect_strategies(start_index=len(existing) + 1)
    if not strategies:
        print("\nNo strategies entered. Nothing to save.")
        return

    all_strategies, added, skipped = save_strategies(STRATEGY_DB, strategies)

    if added:
        print(f"\nSaved {len(added)} new strategy(ies) to '{STRATEGY_DB}'.")
        for s in added:
            print(f"  + {s}")
    if skipped:
        print(f"\nSkipped {len(skipped)} duplicate(s):")
        for s in skipped:
            print(f"  - {s}")

    print(f"\nTotal strategies in database: {len(all_strategies)}")


# ------------------------------------------------------------------
# MT5 trade export (History-tab style)
# ------------------------------------------------------------------
def build_trade_history(deals):
    """
    Group deals by position_id and return one clean record per trade,
    shaped like a MetaTrader5 History row.
    """
    trades = {}

    for d in deals:
        pid = d.position_id
        if pid == 0:
            # Balance / credit operations — skip
            continue

        if pid not in trades:
            trades[pid] = {
                "position_id": pid,
                "symbol": d.symbol,
                "type": None,            # buy / sell (from entry deal)
                "volume": 0.0,
                "open_time": None,
                "open_price": None,
                "sl": 0.0,
                "tp": 0.0,
                "close_time": None,
                "close_price": None,
                "commission": 0.0,
                "swap": 0.0,
                "profit": 0.0,
                "comment": "",
                "magic": d.magic,
            }

        t = trades[pid]
        t["volume"] += d.volume
        t["commission"] += d.commission
        t["swap"] += d.swap
        t["profit"] += d.profit

        # Entry deal (DEAL_ENTRY_IN == 0)
        if d.entry == 0:
            t["type"] = deal_type_name(d.type)
            t["open_time"] = d.time
            t["open_price"] = d.price

        # Exit deal (DEAL_ENTRY_OUT == 1 or OUT_BY == 3)
        elif d.entry in (1, 3):
            t["close_time"] = d.time
            t["close_price"] = d.price

        if d.comment:
            t["comment"] = d.comment

    # Order records + format timestamps
    ordered = sorted(trades.values(), key=lambda x: x["open_time"] or 0)

    for t in ordered:
        t["volume"] = round(t["volume"], 2)
        t["commission"] = round(t["commission"], 2)
        t["swap"] = round(t["swap"], 2)
        t["profit"] = round(t["profit"], 2)
        t["net_profit"] = round(t["profit"] + t["commission"] + t["swap"], 2)
        t["open_time"] = ts_to_str(t["open_time"])
        t["close_time"] = ts_to_str(t["close_time"])

    return ordered


def build_open_positions(positions):
    """Shape open positions like the MT5 Trade tab."""
    out = []
    for p in positions:
        out.append({
            "ticket": p.ticket,
            "symbol": p.symbol,
            "type": "buy" if p.type == 0 else "sell",
            "volume": p.volume,
            "open_time": ts_to_str(p.time),
            "open_price": p.price_open,
            "sl": p.sl,
            "tp": p.tp,
            "current_price": p.price_current,
            "swap": round(p.swap, 2),
            "profit": round(p.profit, 2),
            "comment": p.comment,
        })
    return out


def export_mt5_trades(filepath=TRADES_DB):
    if not MT5_AVAILABLE:
        print("\n[!] MetaTrader5 package not installed.")
        print("    Install it with:  pip install MetaTrader5")
        return

    print("\nConnecting to MetaTrader5...")
    if not mt5.initialize():
        print(f"[!] MT5 initialize() failed, error code = {mt5.last_error()}")
        print("    Make sure the MT5 terminal is installed and running.")
        return

    try:
        account = mt5.account_info()
        if account is None:
            print("[!] Could not read account info. Are you logged in?")
            return

        from_date = datetime(2000, 1, 1)
        to_date = datetime.now(timezone.utc)

        raw_deals = mt5.history_deals_get(from_date, to_date) or []
        positions = mt5.positions_get() or []

        trade_history = build_trade_history(raw_deals)
        open_positions = build_open_positions(positions)

        # Summary stats (like MT5's History bottom bar)
        total_profit = round(sum(t["profit"] for t in trade_history), 2)
        total_commission = round(sum(t["commission"] for t in trade_history), 2)
        total_swap = round(sum(t["swap"] for t in trade_history), 2)
        net = round(total_profit + total_commission + total_swap, 2)

        payload = {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "account": {
                "login": account.login,
                "server": account.server,
                "name": account.name,
                "currency": account.currency,
                "balance": account.balance,
                "equity": account.equity,
                "leverage": account.leverage,
            },
            "summary": {
                "total_trades": len(trade_history),
                "open_positions": len(open_positions),
                "total_profit": total_profit,
                "total_commission": total_commission,
                "total_swap": total_swap,
                "net_result": net,
            },
            "history": trade_history,
            "open_positions": open_positions,
        }

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=4, ensure_ascii=False, default=str)

        print(f"\nExported {len(trade_history)} trade(s) from history.")
        print(f"Open positions: {len(open_positions)}")
        print(f"Net result: {net} {account.currency}")
        print(f"Saved to '{filepath}'.")
    finally:
        mt5.shutdown()


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------
def main():
    strategy_step()
    export_mt5_trades()


if __name__ == "__main__":
    main()