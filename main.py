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
# Strategy database helpers
# ------------------------------------------------------------------
def load_existing_strategies(filepath):
    """Load existing strategies from the JSON file if it exists."""
    if not os.path.exists(filepath):
        return []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("strategies", [])
    except (json.JSONDecodeError, IOError):
        return []


def collect_strategies(start_index=1):
    """Prompt the user for strategies until an empty input is given."""
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
    """Save strategies to the JSON database, skipping duplicates."""
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
    """Handle the strategy input flow (skippable if DB exists)."""
    existing = load_existing_strategies(STRATEGY_DB)

    if existing:
        print(f"\nFound existing strategy database with {len(existing)} entries.")
        answer = input("Do you want to modify the strategy database? (y/n): ").strip().lower()
        if answer not in ("y", "yes"):
            print("Skipping strategy update.")
            return

    start_index = len(existing) + 1
    strategies = collect_strategies(start_index=start_index)

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
# MetaTrader5 trade export
# ------------------------------------------------------------------
def _serialize(obj):
    """Make MT5 objects / datetimes JSON-safe."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "_asdict"):          # MT5 namedtuple-like objects
        return {k: _serialize(v) for k, v in obj._asdict().items()}
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialize(v) for v in obj]
    return obj


def _group_deals_into_trades(deals):
    """Group MT5 deals into one entry per trade (position)."""
    trades = {}

    for deal in deals:
        pid = deal.position_id
        if pid == 0:
            # Balance operations (deposit/withdraw/credit) — skip
            continue

        if pid not in trades:
            trades[pid] = {
                "position_id": pid,
                "symbol": deal.symbol,
                "type": deal.type,          # 0 = buy, 1 = sell
                "volume": 0.0,
                "open_time": None,
                "close_time": None,
                "open_price": None,
                "close_price": None,
                "profit": 0.0,
                "commission": 0.0,
                "swap": 0.0,
                "comment": "",
                "magic": deal.magic,
                "deals": [],                # raw deals for reference
            }

        t = trades[pid]
        t["deals"].append(_serialize(deal))
        t["volume"] += deal.volume
        t["profit"] += deal.profit
        t["commission"] += deal.commission
        t["swap"] += deal.swap

        # Entry deal (DEAL_ENTRY_IN = 0)
        if deal.entry == 0:
            t["open_time"] = deal.time
            t["open_price"] = deal.price
        # Exit deal (DEAL_ENTRY_OUT = 1) or OUT_BY (3)
        elif deal.entry in (1, 3):
            t["close_time"] = deal.time
            t["close_price"] = deal.price

        if deal.comment:
            t["comment"] = deal.comment

    # Sort by open time
    return sorted(trades.values(), key=lambda x: x["open_time"] or 0)


def export_mt5_trades(filepath=TRADES_DB):
    """Connect to MetaTrader5 and export all trades (grouped by position)."""
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

        # Group deals → real trades
        trades = _group_deals_into_trades(raw_deals)

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
            "trades_count": len(trades),
            "open_positions_count": len(positions),
            "raw_deals_count": len(raw_deals),      # for debugging
            "trades": trades,
            "open_positions": [_serialize(p) for p in positions],
        }

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=4, ensure_ascii=False, default=str)

        print(f"\nGrouped {len(raw_deals)} deals → {len(trades)} trade(s).")
        print(f"Open positions: {len(positions)}")
        print(f"Saved to '{filepath}'.")
    finally:
        mt5.shutdown()


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------
def main():
    # Step 1 — strategy database (optional modify)
    strategy_step()

    # Step 2 — export MT5 trades
    export_mt5_trades()


if __name__ == "__main__":
    main()