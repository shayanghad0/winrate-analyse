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
# Utilities
# ------------------------------------------------------------------
def ts_to_str(ts):
    if ts in (None, 0):
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def format_duration(seconds):
    if not seconds or seconds < 0:
        return None
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    if not parts: parts.append(f"{s}s")
    return " ".join(parts)


def deal_type_name(t):
    return {0: "buy", 1: "sell"}.get(t, str(t))


def detect_exit_reason(close_price, sl, tp, symbol_digits=5):
    if close_price is None:
        return "unknown"
    tol = 10 ** (-symbol_digits + 1)
    if tp and abs(close_price - tp) < tol:
        return "tp"
    if sl and abs(close_price - sl) < tol:
        return "sl"
    return "manual"


# ------------------------------------------------------------------
# Strategy DB
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
    for s in new_strategies:
        if s.lower() in seen:
            skipped.append(s)
        else:
            existing.append(s)
            seen.add(s.lower())
            added.append(s)
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
        for s in added: print(f"  + {s}")
    if skipped:
        print(f"\nSkipped {len(skipped)} duplicate(s):")
        for s in skipped: print(f"  - {s}")
    print(f"\nTotal strategies in database: {len(all_strategies)}")


# ------------------------------------------------------------------
# MT5 helpers
# ------------------------------------------------------------------
def build_orders_map(orders):
    out = {}
    for o in orders:
        pid = o.position_id
        if pid == 0:
            continue
        if o.sl or o.tp:
            entry = out.setdefault(pid, {"sl": 0.0, "tp": 0.0})
            if o.sl: entry["sl"] = o.sl
            if o.tp: entry["tp"] = o.tp
    return out


def match_strategy(comment, strategy_list):
    if not comment:
        return None
    c = comment.lower().strip()
    for s in strategy_list:
        if s.lower().strip() in c:
            return s
    return None


def build_trade_history(deals, orders, strategy_list):
    orders_map = build_orders_map(orders)
    trades = {}

    for d in deals:
        pid = d.position_id
        if pid == 0:
            continue
        if pid not in trades:
            trades[pid] = {
                "position_id": pid,
                "symbol": d.symbol,
                "type": None,
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

        if d.entry == 0:
            t["type"] = deal_type_name(d.type)
            t["open_time"] = d.time
            t["open_price"] = d.price
        elif d.entry in (1, 3):
            t["close_time"] = d.time
            t["close_price"] = d.price

        if d.comment:
            t["comment"] = d.comment

    out = []
    for pid, t in trades.items():
        o = orders_map.get(pid, {})
        t["sl"] = round(o.get("sl", 0.0), 5)
        t["tp"] = round(o.get("tp", 0.0), 5)
        t["exit_reason"] = detect_exit_reason(t["close_price"], t["sl"], t["tp"])

        duration_sec = None
        if t["open_time"] and t["close_time"]:
            duration_sec = t["close_time"] - t["open_time"]
        t["duration_seconds"] = duration_sec
        t["duration"] = format_duration(duration_sec)

        t["strategy"] = match_strategy(t["comment"], strategy_list)

        t["volume"] = round(t["volume"], 2)
        t["commission"] = round(t["commission"], 2)
        t["swap"] = round(t["swap"], 2)
        t["profit"] = round(t["profit"], 2)
        t["net_profit"] = round(t["profit"] + t["commission"] + t["swap"], 2)
        t["open_time"] = ts_to_str(t["open_time"])
        t["close_time"] = ts_to_str(t["close_time"])

        out.append(t)

    out.sort(key=lambda x: x["open_time"] or "")
    return out


def build_open_positions(positions):
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


# ------------------------------------------------------------------
# Interactive strategy tagging (NEW)
# ------------------------------------------------------------------
def ask_strategy_for_trades(trades, strategy_list):
    """
    Ask the user, trade by trade, which strategy was used.
    Returns the (possibly updated) strategy_list.
    """
    if not trades:
        return strategy_list

    print(f"\n{'=' * 62}")
    print(f"  Strategy tagging — {len(trades)} trade(s) to review")
    print(f"{'=' * 62}")
    print("For each trade: type a number from the list, type a strategy name,")
    print("press ENTER to keep current / skip, or 'q' to stop tagging.\n")

    new_strategies_to_add = []

    for i, t in enumerate(trades, 1):
        print(f"\n--- Trade {i}/{len(trades)} ----------------------------------")
        print(f"  Symbol     : {t['symbol']}")
        print(f"  Type       : {t['type']}")
        print(f"  Volume     : {t['volume']}")
        print(f"  Open       : {t['open_time']} @ {t['open_price']}")
        print(f"  Close      : {t['close_time']} @ {t['close_price']}")
        print(f"  Duration   : {t['duration']}")
        print(f"  Net P/L    : {t['net_profit']}")
        if t.get("comment"):
            print(f"  Comment    : {t['comment']}")

        if strategy_list:
            print("\n  Available strategies:")
            for idx, s in enumerate(strategy_list, 1):
                marker = " <-- current" if s == t.get("strategy") else ""
                print(f"    [{idx}] {s}{marker}")

        current = t.get("strategy")
        prompt = "\n  Strategy (number / name / ENTER to skip / q to quit): "
        try:
            answer = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Interrupted. Stopping strategy tagging.")
            break

        if answer.lower() == "q":
            print("  Stopping strategy tagging early.")
            break

        if answer == "":
            # keep whatever auto-match gave (could be None)
            if current:
                print(f"  → Kept: {current}")
            else:
                print("  → Skipped (no strategy).")
            continue

        # Number pick
        if answer.isdigit() and strategy_list:
            idx = int(answer) - 1
            if 0 <= idx < len(strategy_list):
                t["strategy"] = strategy_list[idx]
                print(f"  → Assigned: {t['strategy']}")
                continue
            else:
                print(f"  [!] Invalid number. Skipping this trade.")
                continue

        # Free-text strategy name
        t["strategy"] = answer
        print(f"  → Assigned: {answer}")

        # If it's new, remember to add it to the DB
        if answer.lower() not in {s.lower() for s in strategy_list}:
            new_strategies_to_add.append(answer)
            strategy_list.append(answer)

    # Persist any new strategies the user invented during tagging
    if new_strategies_to_add:
        _, added, _ = save_strategies(STRATEGY_DB, new_strategies_to_add)
        if added:
            print(f"\nAdded {len(added)} new strategy(ies) to '{STRATEGY_DB}':")
            for s in added:
                print(f"  + {s}")

    return strategy_list


# ------------------------------------------------------------------
# Stats
# ------------------------------------------------------------------
def compute_stats(trades):
    def _stats(rows):
        if not rows:
            return {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0,
                    "gross_profit": 0, "gross_loss": 0, "net_profit": 0,
                    "avg_win": 0, "avg_loss": 0}
        wins = [t for t in rows if t["net_profit"] > 0]
        losses = [t for t in rows if t["net_profit"] < 0]
        gp = round(sum(t["net_profit"] for t in wins), 2)
        gl = round(sum(t["net_profit"] for t in losses), 2)
        net = round(gp + gl, 2)
        return {
            "trades": len(rows),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(rows) * 100, 2),
            "gross_profit": gp,
            "gross_loss": gl,
            "net_profit": net,
            "avg_win": round(gp / len(wins), 2) if wins else 0,
            "avg_loss": round(gl / len(losses), 2) if losses else 0,
        }

    by_symbol, by_strategy = {}, {}
    for t in trades:
        by_symbol.setdefault(t["symbol"], []).append(t)
        key = t["strategy"] or "untagged"
        by_strategy.setdefault(key, []).append(t)

    return {
        "overall": _stats(trades),
        "by_symbol": {k: _stats(v) for k, v in by_symbol.items()},
        "by_strategy": {k: _stats(v) for k, v in by_strategy.items()},
    }


# ------------------------------------------------------------------
# Main export
# ------------------------------------------------------------------
def export_mt5_trades(filepath=TRADES_DB, ask_strategies=True):
    if not MT5_AVAILABLE:
        print("\n[!] MetaTrader5 package not installed.")
        print("    Install with:  pip install MetaTrader5")
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

        deals = mt5.history_deals_get(from_date, to_date) or []
        orders = mt5.history_orders_get(from_date, to_date) or []
        positions = mt5.positions_get() or []

        strategy_list = load_existing_strategies(STRATEGY_DB)

        print(f"Grouping {len(deals)} deal(s) into trades...")
        history = build_trade_history(deals, orders, strategy_list)
        open_positions = build_open_positions(positions)

        print(f"Found {len(history)} closed trade(s) and {len(open_positions)} open position(s).")

        # ---- NEW: interactive per-trade strategy tagging ----
        if ask_strategies and history:
            ask_strategy_for_trades(history, strategy_list)

        # Stats are computed AFTER tagging so by_strategy is accurate
        stats = compute_stats(history)

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
                "total_trades": len(history),
                "open_positions": len(open_positions),
                "net_result": stats["overall"]["net_profit"],
                "win_rate": stats["overall"]["win_rate"],
            },
            "stats": stats,
            "history": history,
            "open_positions": open_positions,
        }

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=4, ensure_ascii=False, default=str)

        o = stats["overall"]
        print(f"\nExported {len(history)} trade(s) → '{filepath}'")
        print(f"  Open positions : {len(open_positions)}")
        print(f"  Net result     : {o['net_profit']} {account.currency}")
        print(f"  Win rate       : {o['win_rate']}%  ({o['wins']}W / {o['losses']}L)")
    finally:
        mt5.shutdown()


def main():
    strategy_step()
    export_mt5_trades(ask_strategies=True)


if __name__ == "__main__":
    main()