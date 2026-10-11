import json
import os
from datetime import datetime, timezone
from collections import defaultdict

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False

STRATEGY_DB = "strategies.json"
TRADES_DB = "trades.json"
REPORT_HTML = "report.html"
ALL_LABEL = "All"


# ==================================================================
# Utilities
# ==================================================================
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


def esc(s):
    if s is None:
        return ""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# ==================================================================
# Strategy DB
# ==================================================================
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
        if s.lower() == ALL_LABEL.lower():
            continue
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


# ==================================================================
# MT5 helpers
# ==================================================================
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
                "position_id": pid, "symbol": d.symbol, "type": None,
                "volume": 0.0, "open_time": None, "open_price": None,
                "sl": 0.0, "tp": 0.0, "close_time": None, "close_price": None,
                "commission": 0.0, "swap": 0.0, "profit": 0.0,
                "comment": "", "magic": d.magic,
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
            "ticket": p.ticket, "symbol": p.symbol,
            "type": "buy" if p.type == 0 else "sell",
            "volume": p.volume, "open_time": ts_to_str(p.time),
            "open_price": p.price_open, "sl": p.sl, "tp": p.tp,
            "current_price": p.price_current,
            "swap": round(p.swap, 2), "profit": round(p.profit, 2),
            "comment": p.comment,
        })
    return out


# ==================================================================
# Interactive strategy tagging
# ==================================================================
def ask_strategy_for_trades(trades, strategy_list):
    if not trades:
        return strategy_list

    print(f"\n{'=' * 62}")
    print(f"  Strategy tagging — {len(trades)} trade(s) to review")
    print(f"{'=' * 62}")
    print("For each trade: type the number of the strategy used,")
    print("or type a new strategy name, or 'q' to stop early.")
    print(f"The last option is always '{ALL_LABEL}' — pick it if you used")
    print("multiple strategies on that trade.\n")

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

        options = list(strategy_list) + [ALL_LABEL]
        print("\n  Available options:")
        for idx, s in enumerate(options, 1):
            marker = " <-- current" if s == t.get("strategy") else ""
            print(f"    [{idx}] {s}{marker}")

        while True:
            try:
                answer = input("\n  Pick (number / new name / q to quit): ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n  Interrupted. Stopping strategy tagging.")
                return strategy_list

            if answer.lower() == "q":
                print("  Stopping strategy tagging early.")
                return strategy_list

            if answer == "":
                print("  [!] Empty input not allowed — pick a number or type a name.")
                continue

            if answer.isdigit():
                idx = int(answer) - 1
                if 0 <= idx < len(options):
                    chosen = options[idx]
                    t["strategy"] = chosen
                    print(f"  → Assigned: {chosen}")
                    if chosen.lower() != ALL_LABEL.lower():
                        if chosen.lower() not in {s.lower() for s in strategy_list}:
                            new_strategies_to_add.append(chosen)
                            strategy_list.append(chosen)
                    break
                else:
                    print(f"  [!] Number out of range (1-{len(options)}). Try again.")
                    continue

            t["strategy"] = answer
            print(f"  → Assigned: {answer}")
            if answer.lower() not in {s.lower() for s in strategy_list} \
               and answer.lower() != ALL_LABEL.lower():
                new_strategies_to_add.append(answer)
                strategy_list.append(answer)
            break

    if new_strategies_to_add:
        _, added, _ = save_strategies(STRATEGY_DB, new_strategies_to_add)
        if added:
            print(f"\nAdded {len(added)} new strategy(ies) to '{STRATEGY_DB}':")
            for s in added:
                print(f"  + {s}")

    return strategy_list


# ==================================================================
# Stats
# ==================================================================
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
            "trades": len(rows), "wins": len(wins), "losses": len(losses),
            "win_rate": round(len(wins) / len(rows) * 100, 2),
            "gross_profit": gp, "gross_loss": gl, "net_profit": net,
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


# ==================================================================
# AI Analysis Engine
# ==================================================================
def ai_analyze(payload):
    """Rule-based AI analysis: score + insights + recommendations."""
    history = payload["history"]
    stats = payload["stats"]
    overall = stats["overall"]

    if not history:
        return {"score": 0, "grade": "N/A", "profit_factor": 0,
                "risk_reward": 0, "insights": [], "recommendations": []}

    gp, gl = overall["gross_profit"], overall["gross_loss"]
    pf = round(gp / abs(gl), 2) if gl else (float("inf") if gp > 0 else 0)
    rr = round(overall["avg_win"] / abs(overall["avg_loss"]), 2) if overall["avg_loss"] else 0

    score = 0.0
    score += min(overall["win_rate"], 100) * 0.4
    score += 30 if pf == float("inf") else min(pf, 3) / 3 * 30
    score += min(rr, 3) / 3 * 30
    score = round(score, 1)

    grade = ("A+" if score >= 90 else "A" if score >= 80 else "B" if score >= 70
             else "C" if score >= 60 else "D" if score >= 50 else "F")

    insights, recommendations = [], []

    # --- Best / worst strategy (min 3 trades) ---
    strat = stats["by_strategy"]
    eligible = {k: v for k, v in strat.items() if v["trades"] >= 3}
    if eligible:
        best_s = max(eligible.items(), key=lambda x: x[1]["net_profit"])
        insights.append({"type": "good",
                         "title": f"Best strategy: {best_s[0]}",
                         "body": f"{best_s[1]['trades']} trades • "
                                 f"{best_s[1]['win_rate']}% win rate • "
                                 f"net {best_s[1]['net_profit']:+.2f}"})
        worst_s = min(eligible.items(), key=lambda x: x[1]["net_profit"])
        if worst_s[1]["net_profit"] < 0 and worst_s[0] != best_s[0]:
            insights.append({"type": "bad",
                             "title": f"Losing strategy: {worst_s[0]}",
                             "body": f"{worst_s[1]['trades']} trades • "
                                     f"{worst_s[1]['win_rate']}% win rate • "
                                     f"net {worst_s[1]['net_profit']:+.2f}"})
            recommendations.append(
                f"Review or pause '{worst_s[0]}' — it lost "
                f"{abs(worst_s[1]['net_profit']):.2f} over {worst_s[1]['trades']} trades.")

    # --- Best symbol ---
    sym = stats["by_symbol"]
    eligible_sym = {k: v for k, v in sym.items() if v["trades"] >= 3}
    if eligible_sym:
        best_sym = max(eligible_sym.items(), key=lambda x: x[1]["net_profit"])
        insights.append({"type": "good",
                         "title": f"Best symbol: {best_sym[0]}",
                         "body": f"{best_sym[1]['trades']} trades • "
                                 f"net {best_sym[1]['net_profit']:+.2f}"})
        worst_sym = min(eligible_sym.items(), key=lambda x: x[1]["net_profit"])
        if worst_sym[1]["net_profit"] < 0 and worst_sym[0] != best_sym[0]:
            recommendations.append(
                f"{worst_sym[0]} is net negative ({worst_sym[1]['net_profit']:+.2f}). "
                f"Consider trading it less or reviewing your entry rules on it.")

    # --- Direction bias ---
    longs = [t for t in history if t["type"] == "buy"]
    shorts = [t for t in history if t["type"] == "sell"]
    if longs and shorts:
        l_net = round(sum(t["net_profit"] for t in longs), 2)
        s_net = round(sum(t["net_profit"] for t in shorts), 2)
        bias = "long" if l_net > s_net else "short"
        insights.append({"type": "info",
                         "title": f"Directional edge: {bias}",
                         "body": f"Long: {l_net:+.2f} ({len(longs)}) • "
                                 f"Short: {s_net:+.2f} ({len(shorts)})"})

    # --- Best hour ---
    by_hour = defaultdict(list)
    for t in history:
        if t.get("open_time"):
            try:
                h = int(t["open_time"][11:13])
                by_hour[h].append(t["net_profit"])
            except (ValueError, IndexError):
                pass
    hours = {h: {"count": len(v), "net": round(sum(v), 2)}
             for h, v in by_hour.items() if len(v) >= 3}
    if hours:
        best_h = max(hours.items(), key=lambda x: x[1]["net"])
        worst_h = min(hours.items(), key=lambda x: x[1]["net"])
        insights.append({"type": "info",
                         "title": f"Best hour: {best_h[0]:02d}:00",
                         "body": f"{best_h[1]['count']} trades • net {best_h[1]['net']:+.2f}"})
        if worst_h[1]["net"] < 0 and worst_h[0] != best_h[0]:
            recommendations.append(
                f"Avoid trading around {worst_h[0]:02d}:00 — "
                f"net {worst_h[1]['net']:+.2f} over {worst_h[1]['count']} trades.")

    # --- Best / worst weekday ---
    by_day = defaultdict(list)
    for t in history:
        if t.get("open_time"):
            try:
                d = datetime.strptime(t["open_time"], "%Y-%m-%d %H:%M:%S").strftime("%A")
                by_day[d].append(t["net_profit"])
            except ValueError:
                pass
    if by_day:
        best_d = max(by_day.items(), key=lambda x: sum(x[1]))
        worst_d = min(by_day.items(), key=lambda x: sum(x[1]))
        insights.append({"type": "info",
                         "title": f"Best weekday: {best_d[0]}",
                         "body": f"{len(best_d[1])} trades • net {sum(best_d[1]):+.2f}"})
        if sum(worst_d[1]) < 0:
            recommendations.append(
                f"{worst_d[0]}s are net negative ({sum(worst_d[1]):+.2f}). "
                f"Reduce size or skip that day.")

    # --- Streaks ---
    sorted_t = sorted(history, key=lambda x: x["close_time"] or "")
    max_w = max_l = cur_w = cur_l = 0
    for t in sorted_t:
        if t["net_profit"] > 0:
            cur_w += 1; cur_l = 0; max_w = max(max_w, cur_w)
        elif t["net_profit"] < 0:
            cur_l += 1; cur_w = 0; max_l = max(max_l, cur_l)
    insights.append({"type": "info", "title": "Streak analysis",
                     "body": f"Longest win streak: {max_w} • "
                             f"Longest losing streak: {max_l}"})
    if max_l >= 5:
        recommendations.append(
            f"You had a {max_l}-trade losing streak. Consider a daily "
            f"stop-loss rule to cap drawdowns.")

    # --- Exit reasons ---
    exits = defaultdict(int)
    for t in history:
        exits[t.get("exit_reason", "unknown")] += 1
    if exits:
        insights.append({"type": "info", "title": "Exit reasons",
                         "body": " • ".join(f"{k}: {v}" for k, v in exits.items())})
        manual = exits.get("manual", 0)
        if manual > len(history) * 0.7:
            recommendations.append(
                f"{manual}/{len(history)} trades closed manually. Letting TP/SL "
                f"run could improve consistency.")

    # --- R:R commentary ---
    if rr and rr < 1:
        recommendations.append(
            f"Avg win ({overall['avg_win']:+.2f}) is smaller than avg loss "
            f"({abs(overall['avg_loss']):.2f}). Aim for R:R ≥ 1.5 or tighten stops.")
    elif rr >= 1.5:
        insights.append({"type": "good",
                         "title": f"Healthy R:R: {rr}",
                         "body": f"Avg win {overall['avg_win']:+.2f} vs "
                                 f"avg loss {abs(overall['avg_loss']):.2f}"})

    # --- Profit factor ---
    if pf != float("inf") and pf < 1:
        recommendations.append(f"Profit factor {pf} (< 1.0) — currently unprofitable.")
    elif pf != float("inf") and pf >= 2:
        insights.append({"type": "good",
                         "title": f"Strong profit factor: {pf}",
                         "body": "Gross profit is at least 2× gross loss."})

    # --- Win rate ---
    if overall["win_rate"] >= 60:
        insights.append({"type": "good",
                         "title": f"Strong win rate: {overall['win_rate']}%",
                         "body": f"{overall['wins']}W / {overall['losses']}L"})
    elif overall["win_rate"] < 40:
        recommendations.append(
            f"Win rate {overall['win_rate']}% — be more selective on entries.")

    if not recommendations:
        recommendations.append("No major issues detected. Keep journaling and "
                               "sticking to your plan.")

    return {
        "score": score, "grade": grade,
        "profit_factor": "∞" if pf == float("inf") else pf,
        "risk_reward": rr,
        "insights": insights, "recommendations": recommendations,
    }


# ==================================================================
# HTML report renderer (fully self-contained)
# ==================================================================
_CSS = """
* { box-sizing: border-box; }
body { margin:0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
       background:#0f172a; color:#e2e8f0; line-height:1.5; }
.wrap { max-width:1100px; margin:0 auto; padding:32px 24px 64px; }
header { padding:24px 0 32px; border-bottom:1px solid #1e293b; margin-bottom:32px; }
h1 { margin:0 0 6px; font-size:28px; letter-spacing:-0.02em; }
h2 { font-size:18px; margin:32px 0 16px; color:#cbd5e1; text-transform:uppercase; letter-spacing:0.08em; font-weight:600; }
h3 { font-size:15px; margin:0 0 12px; color:#94a3b8; text-transform:uppercase; letter-spacing:0.06em; font-weight:600; }
.muted { color:#64748b; font-size:13px; margin:0; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:14px; margin-bottom:32px; }
.card { background:#1e293b; border-radius:12px; padding:18px 20px; border:1px solid #334155; }
.card .label { font-size:11px; text-transform:uppercase; letter-spacing:0.08em; color:#94a3b8; margin-bottom:6px; }
.card .value { font-size:24px; font-weight:700; letter-spacing:-0.02em; }
.pos { color:#4ade80; } .neg { color:#f87171; } .neu { color:#e2e8f0; }
.ai-panel { background:linear-gradient(135deg,#1e293b 0%,#1a2334 100%);
            border:1px solid #334155; border-radius:14px; padding:24px; margin-bottom:32px; }
.ai-header { display:flex; align-items:center; gap:20px; margin-bottom:20px; }
.grade { font-size:44px; font-weight:800; letter-spacing:-0.04em; }
.grade.A\\+, .grade.A { color:#4ade80; } .grade.B { color:#a3e635; }
.grade.C { color:#facc15; } .grade.D, .grade.F { color:#f87171; }
.score-bar { flex:1; height:8px; background:#0f172a; border-radius:4px; overflow:hidden; }
.score-fill { height:100%; background:linear-gradient(90deg,#f87171,#facc15,#4ade80); }
.score-txt { font-size:13px; color:#94a3b8; margin-top:4px; }
ul.insights, ul.recs { list-style:none; padding:0; margin:0; }
ul.insights li { padding:12px 14px; border-radius:8px; margin-bottom:8px;
                 border-left:3px solid #475569; background:#0f172a; }
ul.insights li.good { border-left-color:#4ade80; }
ul.insights li.bad  { border-left-color:#f87171; }
ul.insights li.warn { border-left-color:#facc15; }
ul.insights li.info { border-left-color:#60a5fa; }
ul.insights .t { font-weight:600; font-size:14px; }
ul.insights .b { font-size:13px; color:#94a3b8; margin-top:2px; }
ul.recs li { padding:10px 14px; background:#0f172a; border-radius:8px;
             margin-bottom:6px; font-size:13px; color:#cbd5e1; }
ul.recs li::before { content:"→ "; color:#60a5fa; font-weight:700; }
.grid2 { display:grid; grid-template-columns:1fr 1fr; gap:20px; }
.chart-box { background:#1e293b; border:1px solid #334155; border-radius:12px; padding:18px; }
.bar-row { display:grid; grid-template-columns:140px 1fr 80px; gap:10px;
           align-items:center; margin-bottom:6px; font-size:13px; }
.bar-label { color:#cbd5e1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.bar-track { background:#0f172a; height:14px; border-radius:4px; overflow:hidden; }
.bar-fill { height:100%; border-radius:4px; }
.bar-value { text-align:right; font-variant-numeric:tabular-nums; font-weight:600; }
table { width:100%; border-collapse:collapse; font-size:12px;
        font-variant-numeric:tabular-nums; }
th, td { padding:8px 10px; text-align:left; border-bottom:1px solid #1e293b; }
th { background:#1e293b; color:#94a3b8; font-weight:600; text-transform:uppercase;
     letter-spacing:0.05em; font-size:10px; position:sticky; top:0; }
tbody tr:hover { background:#1e293b; }
td.r { text-align:right; }
.tbl-wrap { max-height:520px; overflow:auto; border-radius:10px;
            border:1px solid #334155; background:#0b1220; }
.tag { display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px;
       background:#334155; color:#e2e8f0; }
footer { margin-top:48px; padding-top:16px; border-top:1px solid #1e293b;
         font-size:12px; color:#64748b; text-align:center; }
"""


def _svg_equity_curve(history, width=760, height=220, pad=36):
    if not history:
        return "<p class='muted'>No closed trades.</p>"
    sorted_t = sorted(history, key=lambda x: x["close_time"] or "")
    points, cum = [], 0.0
    for t in sorted_t:
        cum += t["net_profit"]
        points.append(cum)
    if not points:
        return ""
    min_v, max_v = min(points + [0]), max(points + [0])
    rng = (max_v - min_v) or 1
    n = len(points)

    def x(i): return pad + (i / max(n - 1, 1)) * (width - 2 * pad)
    def y(v): return height - pad - ((v - min_v) / rng) * (height - 2 * pad)

    poly = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(points))
    zero_y = y(0)
    color = "#4ade80" if points[-1] >= 0 else "#f87171"
    area = f"{pad},{y(0):.1f} " + poly + f" {x(n - 1):.1f},{y(0):.1f}"

    return f'''<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto">
  <defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0%" stop-color="{color}" stop-opacity="0.35"/>
    <stop offset="100%" stop-color="{color}" stop-opacity="0"/>
  </linearGradient></defs>
  <line x1="{pad}" y1="{zero_y:.1f}" x2="{width-pad}" y2="{zero_y:.1f}"
        stroke="#475569" stroke-dasharray="4 4" stroke-width="1"/>
  <polygon points="{area}" fill="url(#g)"/>
  <polyline points="{poly}" fill="none" stroke="{color}" stroke-width="2.2"/>
  <text x="{pad}" y="{pad-10}" fill="#94a3b8" font-size="11">max {max_v:+.2f}</text>
  <text x="{width-pad}" y="{pad-10}" fill="#94a3b8" font-size="11"
        text-anchor="end">final {points[-1]:+.2f}</text>
  <text x="{pad}" y="{height-10}" fill="#94a3b8" font-size="11">min {min_v:+.2f}</text>
</svg>'''


def _bar_chart(title, items):
    if not items:
        return ""
    max_abs = max(abs(v) for _, v in items) or 1
    rows = []
    for label, value in items:
        pct = abs(value) / max_abs * 100
        color = "#4ade80" if value >= 0 else "#f87171"
        rows.append(
            f'<div class="bar-row">'
            f'<div class="bar-label" title="{esc(label)}">{esc(label)}</div>'
            f'<div class="bar-track"><div class="bar-fill" '
            f'style="width:{pct:.1f}%;background:{color}"></div></div>'
            f'<div class="bar-value" style="color:{color}">{value:+.2f}</div>'
            f'</div>')
    return f"<h3>{esc(title)}</h3>" + "".join(rows)


def render_html_report(payload, analysis, filepath=REPORT_HTML):
    acc = payload["account"]
    s = payload["summary"]
    stats = payload["stats"]
    history = payload["history"]
    o = stats["overall"]

    # ---- Summary cards ----
    net_cls = "pos" if o["net_profit"] >= 0 else "neg"
    cards = f'''
    <div class="card"><div class="label">Net Result</div>
      <div class="value {net_cls}">{o['net_profit']:+.2f} {esc(acc['currency'])}</div></div>
    <div class="card"><div class="label">Win Rate</div>
      <div class="value neu">{o['win_rate']}%</div></div>
    <div class="card"><div class="label">Trades</div>
      <div class="value neu">{o['trades']}</div></div>
    <div class="card"><div class="label">Profit Factor</div>
      <div class="value neu">{analysis['profit_factor']}</div></div>
    <div class="card"><div class="label">Risk / Reward</div>
      <div class="value neu">{analysis['risk_reward']}</div></div>
    <div class="card"><div class="label">Open Positions</div>
      <div class="value neu">{s['open_positions']}</div></div>
    '''

    # ---- AI insights ----
    insights_html = ""
    for i in analysis["insights"]:
        insights_html += (f'<li class="{i["type"]}">'
                          f'<div class="t">{esc(i["title"])}</div>'
                          f'<div class="b">{esc(i["body"])}</div></li>')
    recs_html = "".join(f"<li>{esc(r)}</li>" for r in analysis["recommendations"])

    grade_cls = analysis["grade"].replace("+", "\\+")
    ai_block = f'''
    <div class="ai-panel">
      <div class="ai-header">
        <div class="grade {grade_cls}">{esc(analysis['grade'])}</div>
        <div style="flex:1">
          <div class="score-bar"><div class="score-fill" style="width:{analysis['score']}%"></div></div>
          <div class="score-txt">AI Score: {analysis['score']} / 100</div>
        </div>
      </div>
      <h3>Insights</h3>
      <ul class="insights">{insights_html or "<li class='info'>No insights.</li>"}</ul>
      <h3 style="margin-top:20px">Recommendations</h3>
      <ul class="recs">{recs_html}</ul>
    </div>
    '''

    # ---- Charts ----
    strat_items = sorted(
        [(k, v["net_profit"]) for k, v in stats["by_strategy"].items() if v["trades"] > 0],
        key=lambda x: x[1], reverse=True)
    sym_items = sorted(
        [(k, v["net_profit"]) for k, v in stats["by_symbol"].items() if v["trades"] > 0],
        key=lambda x: x[1], reverse=True)

    charts = f'''
    <div class="chart-box"><h3>Equity Curve</h3>{_svg_equity_curve(history)}</div>
    <div class="grid2" style="margin-top:20px">
      <div class="chart-box">{_bar_chart("Net P/L by Strategy", strat_items)}</div>
      <div class="chart-box">{_bar_chart("Net P/L by Symbol", sym_items)}</div>
    </div>
    '''

    # ---- Trade table ----
    rows = []
    for t in history:
        cls = "pos" if t["net_profit"] >= 0 else "neg"
        rows.append(
            f'<tr>'
            f'<td>{esc(t["open_time"])}</td>'
            f'<td>{esc(t["symbol"])}</td>'
            f'<td>{esc(t["type"])}</td>'
            f'<td class="r">{t["volume"]}</td>'
            f'<td class="r">{t["open_price"]}</td>'
            f'<td class="r">{t["close_price"]}</td>'
            f'<td class="r">{t["sl"]}</td>'
            f'<td class="r">{t["tp"]}</td>'
            f'<td>{esc(t["exit_reason"])}</td>'
            f'<td>{esc(t["duration"])}</td>'
            f'<td><span class="tag">{esc(t["strategy"] or "—")}</span></td>'
            f'<td class="r {cls}">{t["net_profit"]:+.2f}</td>'
            f'</tr>')

    table = f'''
    <div class="tbl-wrap"><table>
      <thead><tr>
        <th>Open</th><th>Symbol</th><th>Type</th><th class="r">Vol</th>
        <th class="r">Open</th><th class="r">Close</th>
        <th class="r">SL</th><th class="r">TP</th>
        <th>Exit</th><th>Duration</th><th>Strategy</th><th class="r">Net</th>
      </tr></thead>
      <tbody>{"".join(rows)}</tbody>
    </table></div>
    '''

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trading Report — {esc(acc["login"])}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>AI Trading Report</h1>
    <p class="muted">Account {esc(acc["login"])} · {esc(acc["server"])} ·
       Balance {acc["balance"]} {esc(acc["currency"])} ·
       Equity {acc["equity"]} {esc(acc["currency"])} ·
       Leverage 1:{acc["leverage"]}</p>
    <p class="muted">Generated {esc(payload["exported_at"])}</p>
  </header>

  <h2>Summary</h2>
  <div class="cards">{cards}</div>

  <h2>AI Analysis</h2>
  {ai_block}

  <h2>Charts</h2>
  {charts}

  <h2>Trade History ({len(history)})</h2>
  {table}

  <footer>Generated by winrate-analyse · {esc(datetime.now(timezone.utc).isoformat())}</footer>
</div>
</body>
</html>'''

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)
    return filepath


# ==================================================================
# Main export
# ==================================================================
def export_mt5_trades(json_path=TRADES_DB, html_path=REPORT_HTML, ask_strategies=True):
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

        if ask_strategies and history:
            ask_strategy_for_trades(history, strategy_list)

        stats = compute_stats(history)

        payload = {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "account": {
                "login": account.login, "server": account.server,
                "name": account.name, "currency": account.currency,
                "balance": account.balance, "equity": account.equity,
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

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=4, ensure_ascii=False, default=str)

        # ---- AI analysis + HTML report ----
        print("\nRunning AI analysis...")
        analysis = ai_analyze(payload)
        render_html_report(payload, analysis, html_path)

        o = stats["overall"]
        print(f"\nExported {len(history)} trade(s) → '{json_path}'")
        print(f"HTML report          → '{html_path}'")
        print(f"  Open positions : {len(open_positions)}")
        print(f"  Net result     : {o['net_profit']} {account.currency}")
        print(f"  Win rate       : {o['win_rate']}%  ({o['wins']}W / {o['losses']}L)")
        print(f"  AI grade       : {analysis['grade']} ({analysis['score']}/100)")
    finally:
        mt5.shutdown()


def main():
    strategy_step()
    export_mt5_trades(ask_strategies=True)


if __name__ == "__main__":
    main()