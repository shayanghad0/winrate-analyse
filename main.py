import json
import os
import sys
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


# Windows consoles default to cp1252, which crashes on characters like '→'.
def _fix_console_encoding():
    for stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


_fix_console_encoding()


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
    if tp and abs(close_price - tp) < tol: return "tp"
    if sl and abs(close_price - sl) < tol: return "sl"
    return "manual"


def esc(s):
    if s is None:
        return ""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def prompt_choice(question, options, allow_skip=False):
    """
    Show a numbered multiple-choice question and return the 0-based index.
    options is a list of strings. If allow_skip, 's' returns None.
    """
    while True:
        print(f"\n  {question}")
        for i, opt in enumerate(options, 1):
            print(f"    [{i}] {opt}")
        suffix = " (1-{}, s=skip)".format(len(options)) if allow_skip else f" (1-{len(options)})"
        try:
            ans = input(f"  >{suffix}: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None
        if allow_skip and ans == "s":
            return None
        if ans.isdigit():
            i = int(ans) - 1
            if 0 <= i < len(options):
                return i
        print("  [!] Invalid. Try again.")


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
        if entry == "": break
        strategies.append(entry)
    return strategies


def save_strategies(filepath, new_strategies):
    existing = load_existing_strategies(filepath)
    seen = {s.lower() for s in existing}
    added, skipped = [], []
    for s in new_strategies:
        if s.lower() == ALL_LABEL.lower(): continue
        if s.lower() in seen: skipped.append(s)
        else:
            existing.append(s); seen.add(s.lower()); added.append(s)
    payload = {"last_updated": datetime.now(timezone.utc).isoformat(),
               "count": len(existing), "strategies": existing}
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
        if pid == 0: continue
        if o.sl or o.tp:
            entry = out.setdefault(pid, {"sl": 0.0, "tp": 0.0})
            if o.sl: entry["sl"] = o.sl
            if o.tp: entry["tp"] = o.tp
    return out


def match_strategy(comment, strategy_list):
    if not comment: return None
    c = comment.lower().strip()
    for s in strategy_list:
        if s.lower().strip() in c: return s
    return None


def build_trade_history(deals, orders, strategy_list):
    orders_map = build_orders_map(orders)
    trades = {}
    for d in deals:
        pid = d.position_id
        if pid == 0: continue
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
        if d.comment: t["comment"] = d.comment

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
# Psychology questionnaire
# ==================================================================
MOOD_OPTS    = ["Calm", "Confident", "Neutral", "Anxious", "FOMO / Revenge"]
FOCUS_OPTS   = ["Distracted", "Low", "Medium", "Focused", "Locked-in"]
STRESS_OPTS  = ["Relaxed", "Mild", "Moderate", "High", "Extreme"]
MARKET_OPTS  = ["No — market doesn't fit", "Partially fits", "Yes — ideal conditions"]
RULES_OPTS   = ["Broke rules", "Some slips", "Mostly followed", "Strictly followed", "Perfectly executed"]


def ask_psychology_for_trade(trade):
    """
    Ask the psychology / suitability / discipline questions for one trade.
    Returns a dict that gets stored on trade['psychology'].
    """
    print(f"\n  ── Psychology for {trade['symbol']} ({trade['type']}) ──")
    print("  Type 's' at any question to skip the whole psychology block.")

    mood_i = prompt_choice("1. Mood when you took this trade?", MOOD_OPTS, allow_skip=True)
    if mood_i is None:
        trade["psychology"] = None
        return

    focus_i  = prompt_choice("2. Focus level?", FOCUS_OPTS)
    stress_i = prompt_choice("3. Stress level?", STRESS_OPTS)
    market_i = prompt_choice("4. Did the market match this strategy's ideal conditions?", MARKET_OPTS)
    rules_i  = prompt_choice("5. How well did you follow this strategy's rules?", RULES_OPTS)

    # Normalize to 0–100 (higher always = better)
    mood_score       = (5 - mood_i) * 25           # calm=100, FOMO=0
    focus_score      = focus_i * 25                # distracted=0, locked-in=100
    stress_score     = (5 - stress_i) * 25         # relaxed=100, extreme=0
    suitability      = int(market_i / 2 * 100)     # 0, 50, 100
    discipline       = rules_i * 25                # broke=0, perfect=100

    psychology_score = round((mood_score + focus_score + stress_score) / 3, 1)
    overall_score    = round((psychology_score + suitability + discipline) / 3, 1)

    # Flags
    flags = []
    if mood_i >= 3:        flags.append("bad_mood")
    if focus_i <= 1:       flags.append("low_focus")
    if stress_i >= 3:      flags.append("high_stress")
    if market_i == 0:      flags.append("market_mismatch")
    if rules_i <= 1:       flags.append("broke_rules")
    if overall_score >= 80: flags.append("clean_setup")
    if not flags:          flags.append("neutral")

    trade["psychology"] = {
        "mood": MOOD_OPTS[mood_i],
        "focus": FOCUS_OPTS[focus_i],
        "stress": STRESS_OPTS[stress_i],
        "market_fit": MARKET_OPTS[market_i],
        "rules_followed": RULES_OPTS[rules_i],
        "mood_score": mood_score,
        "focus_score": focus_score,
        "stress_score": stress_score,
        "suitability_score": suitability,
        "discipline_score": discipline,
        "psychology_score": psychology_score,
        "overall_score": overall_score,
        "flags": flags,
    }
    print(f"  → Psychology score: {psychology_score}/100  |  Overall: {overall_score}/100")


# ==================================================================
# Interactive strategy + psychology tagging
# ==================================================================
def ask_strategy_for_trades(trades, strategy_list):
    if not trades: return strategy_list

    print(f"\n{'=' * 62}")
    print(f"  Strategy + Psychology tagging — {len(trades)} trade(s)")
    print(f"{'=' * 62}")
    print("For each trade: pick the strategy used, then answer 5 short")
    print(f"psychology questions. Last strategy option is always '{ALL_LABEL}'.")
    print("Type 'q' to stop early.\n")

    new_strategies_to_add = []

    for i, t in enumerate(trades, 1):
        print(f"\n--- Trade {i}/{len(trades)} ----------------------------------")
        print(f"  Symbol   : {t['symbol']}   Type: {t['type']}   Vol: {t['volume']}")
        print(f"  Open     : {t['open_time']} @ {t['open_price']}")
        print(f"  Close    : {t['close_time']} @ {t['close_price']}")
        print(f"  Duration : {t['duration']}   Net: {t['net_profit']}")
        if t.get("comment"):
            print(f"  Comment  : {t['comment']}")

        options = list(strategy_list) + [ALL_LABEL]
        print("\n  Available strategies:")
        for idx, s in enumerate(options, 1):
            marker = " <-- current" if s == t.get("strategy") else ""
            print(f"    [{idx}] {s}{marker}")

        while True:
            try:
                answer = input("\n  Pick strategy (number / new name / q to quit): ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n  Interrupted.")
                return strategy_list
            if answer.lower() == "q":
                print("  Stopping tagging early.")
                return strategy_list
            if answer == "":
                print("  [!] Empty input not allowed.")
                continue

            if answer.isdigit():
                idx = int(answer) - 1
                if 0 <= idx < len(options):
                    chosen = options[idx]
                    t["strategy"] = chosen
                    print(f"  → Strategy: {chosen}")
                    if chosen.lower() != ALL_LABEL.lower():
                        if chosen.lower() not in {s.lower() for s in strategy_list}:
                            new_strategies_to_add.append(chosen)
                            strategy_list.append(chosen)
                    break
                else:
                    print(f"  [!] Out of range (1-{len(options)}).")
                    continue

            t["strategy"] = answer
            print(f"  → Strategy: {answer}")
            if answer.lower() not in {s.lower() for s in strategy_list} \
               and answer.lower() != ALL_LABEL.lower():
                new_strategies_to_add.append(answer)
                strategy_list.append(answer)
            break

        # -------- Psychology block --------
        ask_psychology_for_trade(t)

    if new_strategies_to_add:
        _, added, _ = save_strategies(STRATEGY_DB, new_strategies_to_add)
        if added:
            print(f"\nAdded {len(added)} new strategy(ies) to '{STRATEGY_DB}':")
            for s in added: print(f"  + {s}")

    return strategy_list


# ==================================================================
# Stats
# ==================================================================
def _trade_stats(rows):
    if not rows:
        return {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0,
                "gross_profit": 0, "gross_loss": 0, "net_profit": 0,
                "avg_win": 0, "avg_loss": 0}
    wins = [t for t in rows if t["net_profit"] > 0]
    losses = [t for t in rows if t["net_profit"] < 0]
    gp = round(sum(t["net_profit"] for t in wins), 2)
    gl = round(sum(t["net_profit"] for t in losses), 2)
    return {
        "trades": len(rows), "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / len(rows) * 100, 2),
        "gross_profit": gp, "gross_loss": gl,
        "net_profit": round(gp + gl, 2),
        "avg_win": round(gp / len(wins), 2) if wins else 0,
        "avg_loss": round(gl / len(losses), 2) if losses else 0,
    }


def compute_stats(trades):
    by_symbol, by_strategy = {}, {}
    for t in trades:
        by_symbol.setdefault(t["symbol"], []).append(t)
        by_strategy.setdefault(t["strategy"] or "untagged", []).append(t)

    # Psychology buckets (only trades that have psychology data)
    psych_trades = [t for t in trades if t.get("psychology")]
    by_psych = {
        "clean_setup":   [t for t in psych_trades if "clean_setup"   in t["psychology"]["flags"]],
        "bad_mood":      [t for t in psych_trades if "bad_mood"      in t["psychology"]["flags"]],
        "high_stress":   [t for t in psych_trades if "high_stress"   in t["psychology"]["flags"]],
        "low_focus":     [t for t in psych_trades if "low_focus"     in t["psychology"]["flags"]],
        "market_mismatch":[t for t in psych_trades if "market_mismatch" in t["psychology"]["flags"]],
        "broke_rules":   [t for t in psych_trades if "broke_rules"   in t["psychology"]["flags"]],
    }

    return {
        "overall": _trade_stats(trades),
        "by_symbol":   {k: _trade_stats(v) for k, v in by_symbol.items()},
        "by_strategy": {k: _trade_stats(v) for k, v in by_strategy.items()},
        "by_psychology": {k: _trade_stats(v) for k, v in by_psych.items()},
        "psychology_averages": _psych_averages(psych_trades),
    }


def _psych_averages(psych_trades):
    if not psych_trades:
        return None
    n = len(psych_trades)
    keys = ["mood_score", "focus_score", "stress_score", "suitability_score",
            "discipline_score", "psychology_score", "overall_score"]
    return {k: round(sum(t["psychology"][k] for t in psych_trades) / n, 1) for k in keys}


# ==================================================================
# AI analysis
# ==================================================================
def ai_analyze(payload):
    history = payload["history"]
    stats = payload["stats"]
    overall = stats["overall"]

    if not history:
        return {"score": 0, "grade": "N/A", "profit_factor": 0,
                "risk_reward": 0, "insights": [], "recommendations": []}

    gp, gl = overall["gross_profit"], overall["gross_loss"]
    pf = round(gp / abs(gl), 2) if gl else (float("inf") if gp > 0 else 0)
    rr = round(overall["avg_win"] / abs(overall["avg_loss"]), 2) if overall["avg_loss"] else 0

    # Base score
    score = 0.0
    score += min(overall["win_rate"], 100) * 0.4
    score += 30 if pf == float("inf") else min(pf, 3) / 3 * 30
    score += min(rr, 3) / 3 * 30

    # Psychology bonus/penalty (up to ±10)
    pa = stats.get("psychology_averages")
    if pa:
        # 0-100 → -5..+5 for psych, -5..+5 for discipline
        score += (pa["psychology_score"] - 50) / 10
        score += (pa["discipline_score"] - 50) / 10
    score = round(max(0, min(100, score)), 1)

    grade = ("A+" if score >= 90 else "A" if score >= 80 else "B" if score >= 70
             else "C" if score >= 60 else "D" if score >= 50 else "F")

    insights, recommendations = [], []

    # --- Best / worst strategy ---
    eligible = {k: v for k, v in stats["by_strategy"].items() if v["trades"] >= 3}
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

    # --- Best / worst symbol ---
    sym = {k: v for k, v in stats["by_symbol"].items() if v["trades"] >= 3}
    if sym:
        best_sym = max(sym.items(), key=lambda x: x[1]["net_profit"])
        insights.append({"type": "good",
                         "title": f"Best symbol: {best_sym[0]}",
                         "body": f"{best_sym[1]['trades']} trades • net {best_sym[1]['net_profit']:+.2f}"})
        worst_sym = min(sym.items(), key=lambda x: x[1]["net_profit"])
        if worst_sym[1]["net_profit"] < 0 and worst_sym[0] != best_sym[0]:
            recommendations.append(
                f"{worst_sym[0]} is net negative ({worst_sym[1]['net_profit']:+.2f}). "
                f"Consider trading it less or reviewing entry rules.")

    # --- Direction ---
    longs  = [t for t in history if t["type"] == "buy"]
    shorts = [t for t in history if t["type"] == "sell"]
    if longs and shorts:
        l_net = round(sum(t["net_profit"] for t in longs), 2)
        s_net = round(sum(t["net_profit"] for t in shorts), 2)
        bias = "long" if l_net > s_net else "short"
        insights.append({"type": "info",
                         "title": f"Directional edge: {bias}",
                         "body": f"Long: {l_net:+.2f} ({len(longs)}) • "
                                 f"Short: {s_net:+.2f} ({len(shorts)})"})

    # --- Hour / weekday ---
    by_hour, by_day = defaultdict(list), defaultdict(list)
    for t in history:
        if t.get("open_time"):
            try:
                h = int(t["open_time"][11:13]); by_hour[h].append(t["net_profit"])
                d = datetime.strptime(t["open_time"], "%Y-%m-%d %H:%M:%S").strftime("%A")
                by_day[d].append(t["net_profit"])
            except (ValueError, IndexError):
                pass
    hours = {h: {"count": len(v), "net": round(sum(v), 2)}
             for h, v in by_hour.items() if len(v) >= 3}
    if hours:
        bh = max(hours.items(), key=lambda x: x[1]["net"])
        wh = min(hours.items(), key=lambda x: x[1]["net"])
        insights.append({"type": "info", "title": f"Best hour: {bh[0]:02d}:00",
                         "body": f"{bh[1]['count']} trades • net {bh[1]['net']:+.2f}"})
        if wh[1]["net"] < 0 and wh[0] != bh[0]:
            recommendations.append(f"Avoid trading around {wh[0]:02d}:00 — net "
                                   f"{wh[1]['net']:+.2f} over {wh[1]['count']} trades.")
    if by_day:
        bd = max(by_day.items(), key=lambda x: sum(x[1]))
        wd = min(by_day.items(), key=lambda x: sum(x[1]))
        insights.append({"type": "info", "title": f"Best weekday: {bd[0]}",
                         "body": f"{len(bd[1])} trades • net {sum(bd[1]):+.2f}"})
        if sum(wd[1]) < 0:
            recommendations.append(f"{wd[0]}s are net negative ({sum(wd[1]):+.2f}). Reduce size.")

    # --- Streaks ---
    sorted_t = sorted(history, key=lambda x: x["close_time"] or "")
    max_w = max_l = cur_w = cur_l = 0
    for t in sorted_t:
        if t["net_profit"] > 0:  cur_w += 1; cur_l = 0; max_w = max(max_w, cur_w)
        elif t["net_profit"] < 0: cur_l += 1; cur_w = 0; max_l = max(max_l, cur_l)
    insights.append({"type": "info", "title": "Streak analysis",
                     "body": f"Longest win: {max_w} • Longest loss: {max_l}"})
    if max_l >= 5:
        recommendations.append(f"{max_l}-trade losing streak — consider daily stop-loss.")

    # --- Exit reasons ---
    exits = defaultdict(int)
    for t in history: exits[t.get("exit_reason", "unknown")] += 1
    if exits:
        insights.append({"type": "info", "title": "Exit reasons",
                         "body": " • ".join(f"{k}: {v}" for k, v in exits.items())})
        manual = exits.get("manual", 0)
        if manual > len(history) * 0.7:
            recommendations.append(f"{manual}/{len(history)} trades closed manually — "
                                   f"letting TP/SL run improves consistency.")

    # --- R:R + PF + win rate ---
    if rr and rr < 1:
        recommendations.append(
            f"Avg win ({overall['avg_win']:+.2f}) < avg loss ({abs(overall['avg_loss']):.2f}). "
            f"Aim for R:R ≥ 1.5.")
    elif rr >= 1.5:
        insights.append({"type": "good", "title": f"Healthy R:R: {rr}",
                         "body": f"Avg win {overall['avg_win']:+.2f} vs "
                                 f"avg loss {abs(overall['avg_loss']):.2f}"})
    if pf != float("inf") and pf < 1:
        recommendations.append(f"Profit factor {pf} (< 1.0) — currently unprofitable.")
    elif pf != float("inf") and pf >= 2:
        insights.append({"type": "good", "title": f"Strong profit factor: {pf}",
                         "body": "Gross profit ≥ 2× gross loss."})
    if overall["win_rate"] >= 60:
        insights.append({"type": "good", "title": f"Strong win rate: {overall['win_rate']}%",
                         "body": f"{overall['wins']}W / {overall['losses']}L"})
    elif overall["win_rate"] < 40:
        recommendations.append(f"Win rate {overall['win_rate']}% — be more selective.")

    # --- PSYCHOLOGY insights ---
    if pa:
        insights.append({
            "type": "info",
            "title": "Psychology averages",
            "body": f"Mood {pa['mood_score']} • Focus {pa['focus_score']} • "
                    f"Stress {pa['stress_score']} • "
                    f"Suitability {pa['suitability_score']} • "
                    f"Discipline {pa['discipline_score']}"
        })

        pstats = stats["by_psychology"]

        # Discipline
        if pa["discipline_score"] < 60:
            recommendations.append(
                f"Discipline score {pa['discipline_score']}/100 — you frequently "
                f"deviate from your strategy rules. Consider a written pre-trade checklist.")
        elif pa["discipline_score"] >= 85:
            insights.append({"type": "good", "title": f"Excellent discipline ({pa['discipline_score']}/100)",
                             "body": "You stuck to your rules on most trades."})

        # Psychology
        if pa["psychology_score"] < 55:
            recommendations.append(
                f"Average psychology score {pa['psychology_score']}/100 — "
                f"you often trade in a poor mental state. Add a 5-min pre-trade "
                f"breathing / journaling routine.")
        elif pa["psychology_score"] >= 80:
            insights.append({"type": "good", "title": f"Strong mental state ({pa['psychology_score']}/100)",
                             "body": "You trade from a calm, focused baseline."})

        # Bad mood comparison
        bad = pstats["bad_mood"]
        good = pstats["clean_setup"]
        if bad["trades"] >= 3 and good["trades"] >= 3:
            if bad["net_profit"] < 0 < good["net_profit"]:
                insights.append({
                    "type": "bad",
                    "title": "Anxious / FOMO trades lose money",
                    "body": f"Bad-mood trades: {bad['net_profit']:+.2f} over "
                            f"{bad['trades']} trades • Clean setups: "
                            f"{good['net_profit']:+.2f} over {good['trades']}"
                })
                recommendations.append(
                    "Your bad-mood trades are net negative while clean setups are "
                    "profitable. Add a rule: no trading when mood is Anxious/FOMO.")

        # Stress comparison
        if pstats["high_stress"]["trades"] >= 3 and pstats["high_stress"]["net_profit"] < 0:
            recommendations.append(
                f"High-stress trades lost {abs(pstats['high_stress']['net_profit']):.2f} "
                f"over {pstats['high_stress']['trades']} trades — reduce size or skip "
                f"when stress ≥ Moderate.")

        # Focus
        if pstats["low_focus"]["trades"] >= 3 and pstats["low_focus"]["net_profit"] < 0:
            recommendations.append(
                f"Low-focus trades lost {abs(pstats['low_focus']['net_profit']):.2f} — "
                f"don't trade when distracted.")

        # Market mismatch
        if pstats["market_mismatch"]["trades"] >= 3 and pstats["market_mismatch"]["net_profit"] < 0:
            recommendations.append(
                f"Trades taken in wrong market conditions lost "
                f"{abs(pstats['market_mismatch']['net_profit']):.2f} — "
                f"wait for your strategy's ideal setup.")

        # Rule breakers
        if pstats["broke_rules"]["trades"] >= 3 and pstats["broke_rules"]["net_profit"] < 0:
            recommendations.append(
                f"Trades where you broke rules lost "
                f"{abs(pstats['broke_rules']['net_profit']):.2f} — "
                f"rule violations are directly costing you money.")

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
# HTML report
# ==================================================================
_CSS = r"""
:root{
  --bg:#0a0e17;--panel:#111a2e;--panel-2:#0d1526;--line:#1f2c47;
  --text:#e7edf9;--muted:#8797b3;--accent:#5b8cff;--accent2:#8b5cf6;
  --pos:#2fd47a;--neg:#ff5d73;--warn:#ffc857;
}
html[data-theme="light"]{
  --bg:#f3f6fc;--panel:#ffffff;--panel-2:#f8fafc;--line:#e3e9f3;
  --text:#0f172a;--muted:#64748b;--accent:#3b6cf6;--accent2:#7c3aed;
  --pos:#16a34a;--neg:#e11d48;--warn:#d97706;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
 font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
 line-height:1.5;-webkit-font-smoothing:antialiased}
.app{display:grid;grid-template-columns:240px minmax(0,1fr);min-height:100vh}

/* ---- sidebar ---- */
.side{position:sticky;top:0;height:100vh;padding:22px 14px;background:var(--panel-2);
 border-right:1px solid var(--line);display:flex;flex-direction:column;gap:4px}
.brand{display:flex;align-items:center;gap:10px;padding:0 8px 16px;
 border-bottom:1px solid var(--line);margin-bottom:14px}
.logo{width:34px;height:34px;flex:none;border-radius:10px;display:grid;place-items:center;
 background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff;font-weight:800;font-size:15px}
.brand b{font-size:14.5px;letter-spacing:-.01em;display:block}
.brand small{color:var(--muted);font-size:11px}
.nav-btn{display:flex;align-items:center;gap:10px;width:100%;padding:10px 12px;margin-bottom:2px;
 border:0;border-left:2px solid transparent;border-radius:8px;background:transparent;
 color:var(--muted);font-size:14px;cursor:pointer;text-align:left;transition:.15s}
.nav-btn:hover{background:var(--line);color:var(--text)}
.nav-btn.active{background:linear-gradient(90deg,rgba(91,140,255,.16),transparent);
 border-left-color:var(--accent);color:var(--text);font-weight:600}
.nav-btn i{font-style:normal;opacity:.8;width:16px;text-align:center}
.side .foot{margin-top:auto;padding:12px 8px 0;border-top:1px solid var(--line);
 color:var(--muted);font-size:11px}

/* ---- main / topbar ---- */
.main{padding:26px 32px 70px;max-width:1240px;width:100%;min-width:0}
.topbar{display:flex;flex-wrap:wrap;gap:16px;align-items:flex-start;justify-content:space-between;
 padding-bottom:20px;border-bottom:1px solid var(--line);margin-bottom:24px}
h1{margin:0;font-size:24px;letter-spacing:-.02em}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
.chip{background:var(--panel);border:1px solid var(--line);border-radius:999px;
 padding:4px 12px;font-size:12px;color:var(--muted)}
.chip b{color:var(--text);font-weight:600}
.icon-btn{background:var(--panel);border:1px solid var(--line);color:var(--text);
 border-radius:10px;padding:9px 15px;font-size:13px;cursor:pointer;white-space:nowrap;transition:.15s}
.icon-btn:hover{border-color:var(--accent);color:var(--accent)}

section{display:none;margin-bottom:34px;animation:fade .25s ease}
section.show{display:block}
@keyframes fade{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}
h2.sec{font-size:11.5px;text-transform:uppercase;letter-spacing:.11em;color:var(--muted);
 margin:0 0 14px;font-weight:700}

/* ---- cards ---- */
.grid-kpi{display:grid;grid-template-columns:repeat(auto-fit,minmax(178px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px 18px}
.card .label{font-size:10.5px;text-transform:uppercase;letter-spacing:.09em;color:var(--muted);margin-bottom:7px}
.card .value{font-size:23px;font-weight:750;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.card .sub{font-size:11.5px;color:var(--muted);margin-top:5px}
.pos{color:var(--pos)}.neg{color:var(--neg)}.neu{color:var(--text)}.warn{color:var(--warn)}

.panel{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:20px 22px}
.panel + .panel{margin-top:16px}
.panel h3{margin:0 0 14px;font-size:13.5px;letter-spacing:.02em}
.two{display:grid;grid-template-columns:1.4fr 1fr;gap:16px}
@media(max-width:960px){.two{grid-template-columns:1fr}}

/* ---- donut / kv ---- */
.donut{--w:0;width:124px;height:124px;flex:none;border-radius:50%;display:grid;place-items:center;
 background:conic-gradient(var(--pos) calc(var(--w) * 1%),var(--neg) 0)}
.donut-in{width:88px;height:88px;border-radius:50%;background:var(--panel);display:grid;place-items:center;
 text-align:center;font-size:17px;font-weight:750;font-variant-numeric:tabular-nums}
.donut-in small{display:block;font-size:10px;color:var(--muted);font-weight:500;margin-top:2px}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px}
.kv > div{background:var(--panel-2);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.kv span{display:block;font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
.kv b{font-size:17px;font-variant-numeric:tabular-nums}

/* ---- AI panel ---- */
.ai-top{display:flex;gap:24px;align-items:center;flex-wrap:wrap}
.ring{--v:0;width:128px;height:128px;flex:none;border-radius:50%;display:grid;place-items:center;
 background:conic-gradient(var(--accent) calc(var(--v) * 1%),var(--line) 0)}
.ring-in{width:102px;height:102px;border-radius:50%;background:var(--panel);display:grid;place-items:center;
 text-align:center}
.grade{font-size:31px;font-weight:800;letter-spacing:-.03em;line-height:1}
.grade.g-ap,.grade.g-a{color:var(--pos)}
.grade.g-b{color:#a3e635}
.grade.g-c{color:var(--warn)}
.grade.g-d,.grade.g-f{color:var(--neg)}
html[data-theme="light"] .grade.g-b{color:#65a30d}
.ring-in small{display:block;font-size:11px;color:var(--muted);margin-top:3px}
.bar-line{height:10px;border-radius:6px;background:var(--line);overflow:hidden}
.bar-line i{display:block;height:100%;border-radius:6px;
 background:linear-gradient(90deg,var(--neg),var(--warn),var(--pos))}
.ins-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(255px,1fr));gap:10px}
.ins{background:var(--panel-2);border:1px solid var(--line);border-left:3px solid var(--accent);
 border-radius:10px;padding:12px 14px}
.ins.good{border-left-color:var(--pos)}
.ins.bad{border-left-color:var(--neg)}
.ins.warn{border-left-color:var(--warn)}
.ins.info{border-left-color:var(--accent)}
.ins .t{font-size:13.5px;font-weight:650}
.ins .b{font-size:12.5px;color:var(--muted);margin-top:3px}
.recs{list-style:none;padding:0;margin:0;display:grid;gap:8px}
.recs li{background:var(--panel-2);border:1px solid var(--line);border-radius:10px;
 padding:11px 14px;font-size:13px}
.recs li::before{content:"\2192";color:var(--accent);font-weight:700;margin-right:9px}

/* ---- psychology ---- */
.pgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.pcard{background:var(--panel-2);border:1px solid var(--line);border-radius:12px;padding:14px}
.pcard .pl{font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
.pcard .pv{font-size:22px;font-weight:750;margin-top:6px;font-variant-numeric:tabular-nums}
.pcard .track{height:6px;background:var(--line);border-radius:4px;margin-top:10px;overflow:hidden}
.pcard .track i{display:block;height:100%;border-radius:4px}
.pcard .ps{font-size:11px;color:var(--muted);margin-top:7px}

/* ---- bar charts ---- */
.bars h4{margin:0 0 14px;font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.07em}
.bar-row{display:grid;grid-template-columns:minmax(80px,155px) 1fr 96px;gap:10px;
 align-items:center;margin-bottom:8px;font-size:12.5px}
.bar-label{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar-track{height:14px;background:var(--line);border-radius:5px;overflow:hidden}
.bar-fill{height:100%;border-radius:5px}
.bar-value{text-align:right;font-variant-numeric:tabular-nums;font-weight:650}

/* ---- tables ---- */
.toolbar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:12px}
.toolbar input[type=search],.toolbar select{background:var(--panel);border:1px solid var(--line);
 color:var(--text);border-radius:10px;padding:9px 12px;font-size:13px;outline:none}
.toolbar input[type=search]{flex:1;min-width:200px}
.toolbar input[type=search]:focus,.toolbar select:focus{border-color:var(--accent)}
.summary-line{font-size:12.5px;color:var(--muted);margin:0 0 10px}
.tbl-wrap{overflow:auto;border:1px solid var(--line);border-radius:14px;
 background:var(--panel);max-height:620px}
table{width:100%;border-collapse:collapse;font-size:12.5px;font-variant-numeric:tabular-nums}
th,td{padding:9px 12px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}
th{background:var(--panel-2);color:var(--muted);font-size:10.5px;text-transform:uppercase;
 letter-spacing:.06em;position:sticky;top:0;z-index:1}
th.sortable{cursor:pointer;user-select:none}
th.sortable:hover{color:var(--text)}
th .arw{opacity:.3;margin-left:4px}
th.sorted .arw{opacity:1;color:var(--accent)}
tbody tr:hover{background:var(--panel-2)}
td.r{text-align:right}
td.comment{max-width:240px;overflow:hidden;text-overflow:ellipsis;color:var(--muted)}
.tag{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11px;
 background:var(--line);color:var(--text)}
.tag.p-g{background:rgba(47,212,122,.16);color:var(--pos)}
.tag.p-y{background:rgba(255,200,87,.16);color:var(--warn)}
.tag.p-r{background:rgba(255,93,115,.16);color:var(--neg)}
.empty{display:none;padding:30px;text-align:center;color:var(--muted);font-size:13px}
.chart svg{display:block;width:100%;height:auto}
footer{margin-top:44px;padding-top:16px;border-top:1px solid var(--line);
 font-size:12px;color:var(--muted);text-align:center}

/* ---- responsive ---- */
@media(max-width:860px){
  .app{grid-template-columns:1fr}
  .side{position:static;height:auto;flex-direction:row;align-items:center;
   overflow-x:auto;gap:6px;padding:12px 14px;border-right:0;border-bottom:1px solid var(--line)}
  .brand{padding:0 12px 0 0;border-bottom:0;border-right:1px solid var(--line);margin:0 6px 0 0}
  .brand small{display:none}
  .nav-btn{width:auto;white-space:nowrap;border-left:0;border-bottom:2px solid transparent;margin:0}
  .nav-btn.active{border-left:0;border-bottom-color:var(--accent)}
  .side .foot{display:none}
  .main{padding:20px 16px 60px}
}
@media print{
  .side,.toolbar,#themeBtn{display:none}
  .app{display:block}
  .main{padding:0;max-width:none}
  section{display:block!important;page-break-before:always}
  section:first-of-type{page-break-before:avoid}
  .tbl-wrap{max-height:none;overflow:visible}
}
"""

_JS = r"""
(function(){
  var D = window.__REPORT__ || {trades:[], open:[]};
  var root = document.documentElement;
  function $(id){ return document.getElementById(id); }

  /* ---------- theme ---------- */
  var saved = null;
  try{ saved = localStorage.getItem('wr-theme'); }catch(e){}
  if(saved){ root.setAttribute('data-theme', saved); }
  var tbtn = $('themeBtn');
  function paintBtn(){
    tbtn.textContent = root.getAttribute('data-theme') === 'light' ? 'Dark mode' : 'Light mode';
  }
  paintBtn();
  tbtn.addEventListener('click', function(){
    var next = root.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
    root.setAttribute('data-theme', next);
    try{ localStorage.setItem('wr-theme', next); }catch(e){}
    paintBtn();
  });

  /* ---------- navigation ---------- */
  var navs = [].slice.call(document.querySelectorAll('.nav-btn'));
  navs.forEach(function(b){
    b.addEventListener('click', function(){
      navs.forEach(function(x){ x.classList.remove('active'); });
      b.classList.add('active');
      [].slice.call(document.querySelectorAll('main > section')).forEach(function(s){
        s.classList.toggle('show', s.id === b.getAttribute('data-target'));
      });
      window.scrollTo({top:0, behavior:'smooth'});
    });
  });

  /* ---------- helpers ---------- */
  function esc(s){
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;')
      .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }
  function money(v){ return (v >= 0 ? '+' : '') + Number(v).toFixed(2); }
  function badge(v){
    if(v === null || v === undefined) return '<span class="tag">&mdash;</span>';
    var c = v >= 75 ? 'p-g' : (v >= 50 ? 'p-y' : 'p-r');
    return '<span class="tag ' + c + '">' + v + '</span>';
  }

  /* ---------- trade table ---------- */
  var rows = (D.trades || []).slice();
  var sortKey = 'open', sortDir = 1;
  var NUMERIC = {vol:1, net:1, psych:1};

  var stratSel = $('fStrategy');
  var seen = {};
  rows.forEach(function(r){ if(!seen[r.strategy]){ seen[r.strategy] = 1;
    var o = document.createElement('option'); o.value = r.strategy; o.textContent = r.strategy;
    stratSel.appendChild(o); } });

  function filtered(){
    var q = ($('q').value || '').toLowerCase().trim();
    var st = stratSel.value, res = $('fResult').value;
    return rows.filter(function(r){
      if(st !== 'all' && r.strategy !== st) return false;
      if(res === 'win' && !(r.net > 0)) return false;
      if(res === 'loss' && !(r.net < 0)) return false;
      if(q){
        var hay = (r.symbol + ' ' + r.strategy + ' ' + r.exit + ' ' +
                   r.type + ' ' + r.comment).toLowerCase();
        if(hay.indexOf(q) < 0) return false;
      }
      return true;
    });
  }

  function cmp(a, b){
    var x = a[sortKey], y = b[sortKey];
    if(x === null || x === undefined) x = NUMERIC[sortKey] ? -Infinity : '';
    if(y === null || y === undefined) y = NUMERIC[sortKey] ? -Infinity : '';
    var r = NUMERIC[sortKey] ? (x - y) : String(x).localeCompare(String(y));
    return r * sortDir;
  }

  function render(){
    var list = filtered().sort(cmp);
    var html = '';
    list.forEach(function(r){
      var cls = r.net >= 0 ? 'pos' : 'neg';
      html += '<tr>' +
        '<td>' + esc(r.open) + '</td>' +
        '<td>' + esc(r.close) + '</td>' +
        '<td><b>' + esc(r.symbol) + '</b></td>' +
        '<td>' + esc(r.type) + '</td>' +
        '<td class="r">' + r.vol + '</td>' +
        '<td>' + esc(r.exit) + '</td>' +
        '<td>' + esc(r.duration) + '</td>' +
        '<td><span class="tag">' + esc(r.strategy) + '</span></td>' +
        '<td class="r">' + badge(r.psych) + '</td>' +
        '<td class="r ' + cls + '"><b>' + money(r.net) + '</b></td>' +
        '<td class="comment" title="' + esc(r.comment) + '">' + esc(r.comment) + '</td>' +
        '</tr>';
    });
    $('tbody').innerHTML = html;
    $('emptyMsg').style.display = list.length ? 'none' : 'block';
    var net = list.reduce(function(a, r){ return a + r.net; }, 0);
    var wins = list.filter(function(r){ return r.net > 0; }).length;
    $('tblSummary').textContent = 'Showing ' + list.length + ' of ' + rows.length +
      ' trades · ' + wins + 'W / ' + (list.length - wins) + 'L · net ' + money(net);
  }

  ['q', 'fStrategy', 'fResult'].forEach(function(id){
    $(id).addEventListener('input', render);
    $(id).addEventListener('change', render);
  });

  [].slice.call(document.querySelectorAll('th.sortable')).forEach(function(th){
    th.addEventListener('click', function(){
      var k = th.getAttribute('data-key');
      if(sortKey === k){ sortDir = -sortDir; }
      else { sortKey = k; sortDir = (k === 'open' || k === 'close') ? 1 : -1; }
      [].slice.call(document.querySelectorAll('th.sortable')).forEach(function(x){
        x.classList.remove('sorted');
        x.querySelector('.arw').textContent = '\u2195';
      });
      th.classList.add('sorted');
      th.querySelector('.arw').textContent = sortDir > 0 ? '\u25b2' : '\u25bc';
      render();
    });
  });

  /* ---------- CSV export ---------- */
  $('csvBtn').addEventListener('click', function(){
    var cols = ['open','close','symbol','type','vol','exit','duration',
                'strategy','psych','net','comment'];
    var out = [cols.join(',')];
    filtered().sort(cmp).forEach(function(r){
      out.push(cols.map(function(c){
        var v = r[c] === null || r[c] === undefined ? '' : String(r[c]);
        return /[",\n]/.test(v) ? '"' + v.replace(/"/g,'""') + '"' : v;
      }).join(','));
    });
    var blob = new Blob([out.join('\n')], {type:'text/csv;charset=utf-8'});
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'trades.csv';
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    setTimeout(function(){ URL.revokeObjectURL(a.href); }, 1000);
  });

  render();
})();
"""


def _svg_equity_curve(history, width=880, height=260, pad=42):
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

    def x(i):
        return pad + (i / max(n - 1, 1)) * (width - 2 * pad)

    def y(v):
        return height - pad - ((v - min_v) / rng) * (height - 2 * pad)

    poly = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(points))
    color = "#2fd47a" if points[-1] >= 0 else "#ff5d73"
    area = f"{pad},{y(0):.1f} " + poly + f" {x(n - 1):.1f},{y(0):.1f}"

    grid = ""
    for i in range(1, 4):
        gv = min_v + rng * i / 4
        gy = y(gv)
        grid += (f'<line x1="{pad}" y1="{gy:.1f}" x2="{width - pad}" y2="{gy:.1f}" '
                 f'stroke="var(--line)" stroke-width="1"/>'
                 f'<text x="{pad - 6}" y="{gy + 4:.1f}" fill="var(--muted)" font-size="10" '
                 f'text-anchor="end">{gv:+.0f}</text>')

    first_date = (sorted_t[0]["close_time"] or "")[:10]
    last_date = (sorted_t[-1]["close_time"] or "")[:10]
    mid_date = (sorted_t[n // 2]["close_time"] or "")[:10]
    x_labels = (f'<text x="{pad}" y="{height - 12}" fill="var(--muted)" font-size="10">{esc(first_date)}</text>'
                f'<text x="{width / 2}" y="{height - 12}" fill="var(--muted)" font-size="10" '
                f'text-anchor="middle">{esc(mid_date)}</text>'
                f'<text x="{width - pad}" y="{height - 12}" fill="var(--muted)" font-size="10" '
                f'text-anchor="end">{esc(last_date)}</text>')

    return f'''<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">
  <defs><linearGradient id="eq" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0%" stop-color="{color}" stop-opacity="0.35"/>
    <stop offset="100%" stop-color="{color}" stop-opacity="0"/>
  </linearGradient></defs>
  {grid}
  <line x1="{pad}" y1="{y(0):.1f}" x2="{width - pad}" y2="{y(0):.1f}"
        stroke="var(--muted)" stroke-dasharray="4 4" stroke-width="1"/>
  <polygon points="{area}" fill="url(#eq)"/>
  <polyline points="{poly}" fill="none" stroke="{color}" stroke-width="2.4"
            stroke-linejoin="round" stroke-linecap="round"/>
  <circle cx="{x(n - 1):.1f}" cy="{y(points[-1]):.1f}" r="4" fill="{color}"/>
  <text x="{width - pad}" y="{max(14, y(max_v) - 8):.1f}" fill="var(--muted)" font-size="10.5"
        text-anchor="end">final {points[-1]:+.2f}  ·  peak {max_v:+.2f}</text>
  {x_labels}
</svg>'''


def _bar_rows(items, mode="money"):
    """Render horizontal bar rows. mode='money' colors by sign, 'count' uses accent."""
    if not items:
        return "<p class='muted'>No data.</p>"
    max_abs = max(abs(v) for _, v in items) or 1
    rows_html = []
    for label, value in items:
        pct = abs(value) / max_abs * 100
        if mode == "count":
            color = "var(--accent)"
            txt = str(int(value))
        else:
            color = "var(--pos)" if value >= 0 else "var(--neg)"
            txt = f"{value:+.2f}"
        rows_html.append(
            f'<div class="bar-row">'
            f'<div class="bar-label" title="{esc(label)}">{esc(label)}</div>'
            f'<div class="bar-track"><div class="bar-fill" '
            f'style="width:{pct:.1f}%;background:{color}"></div></div>'
            f'<div class="bar-value" style="color:{color}">{txt}</div></div>')
    return "".join(rows_html)


def _psych_color(score):
    if score is None:
        return "neu"
    if score >= 75:
        return "pos"
    if score >= 50:
        return "neu"
    return "neg"


def _psych_track_color(score):
    if score is None:
        return "var(--muted)"
    if score >= 75:
        return "var(--pos)"
    if score >= 50:
        return "var(--warn)"
    return "var(--neg)"


def _psych_badge_class(score):
    if score is None:
        return "p-y"
    if score >= 75:
        return "p-g"
    if score >= 50:
        return "p-y"
    return "p-r"


def _group_table(groups, title):
    """Static breakdown table (strategies / symbols)."""
    rows = []
    for name, v in sorted(groups.items(), key=lambda x: x[1]["net_profit"], reverse=True):
        if not v["trades"]:
            continue
        cls = "pos" if v["net_profit"] >= 0 else "neg"
        wr_cls = "pos" if v["win_rate"] >= 50 else ("neg" if v["win_rate"] < 40 else "neu")
        rows.append(
            f'<tr><td><b>{esc(name)}</b></td>'
            f'<td class="r">{v["trades"]}</td>'
            f'<td class="r">{v["wins"]}</td>'
            f'<td class="r">{v["losses"]}</td>'
            f'<td class="r {wr_cls}">{v["win_rate"]}%</td>'
            f'<td class="r {cls}"><b>{v["net_profit"]:+.2f}</b></td>'
            f'<td class="r pos">{v["avg_win"]:+.2f}</td>'
            f'<td class="r neg">{v["avg_loss"]:+.2f}</td></tr>')
    if not rows:
        return "<p class='muted'>No data.</p>"
    return f'''<div class="tbl-wrap"><table>
  <thead><tr><th>{esc(title)}</th><th class="r">Trades</th><th class="r">W</th>
    <th class="r">L</th><th class="r">Win %</th><th class="r">Net</th>
    <th class="r">Avg win</th><th class="r">Avg loss</th></tr></thead>
  <tbody>{"".join(rows)}</tbody></table></div>'''


def render_html_report(payload, analysis, filepath=REPORT_HTML):
    acc = payload["account"]
    s = payload["summary"]
    stats = payload["stats"]
    history = payload["history"]
    open_positions = payload.get("open_positions", [])
    o = stats["overall"]

    # ---- derived metrics ----
    n = o["trades"]
    expectancy = (round((o["wins"] / n) * o["avg_win"] + (o["losses"] / n) * o["avg_loss"], 2)
                  if n else 0)
    exp_cls = "pos" if expectancy >= 0 else "neg"
    best = max((t["net_profit"] for t in history), default=0)
    worst = min((t["net_profit"] for t in history), default=0)
    durs = [t["duration_seconds"] for t in history if t.get("duration_seconds")]
    avg_dur = format_duration(sum(durs) / len(durs)) if durs else "—"
    net_cls = "pos" if o["net_profit"] >= 0 else "neg"

    # ---- KPI cards ----
    cards = "".join([
        f'<div class="card"><div class="label">Net result</div>'
        f'<div class="value {net_cls}">{o["net_profit"]:+.2f} {esc(acc["currency"])}</div>'
        f'<div class="sub">Gross {o["gross_profit"]:+.2f} / {o["gross_loss"]:+.2f}</div></div>',

        f'<div class="card"><div class="label">Win rate</div>'
        f'<div class="value neu">{o["win_rate"]}%</div>'
        f'<div class="sub">{o["wins"]} wins · {o["losses"]} losses</div></div>',

        f'<div class="card"><div class="label">Trades</div>'
        f'<div class="value neu">{n}</div>'
        f'<div class="sub">Avg duration {avg_dur}</div></div>',

        f'<div class="card"><div class="label">Profit factor</div>'
        f'<div class="value neu">{esc(analysis["profit_factor"])}</div>'
        f'<div class="sub">Gross profit ÷ gross loss</div></div>',

        f'<div class="card"><div class="label">Risk / reward</div>'
        f'<div class="value neu">{analysis["risk_reward"]}</div>'
        f'<div class="sub">Avg {o["avg_win"]:+.2f} vs {o["avg_loss"]:+.2f}</div></div>',

        f'<div class="card"><div class="label">Expectancy</div>'
        f'<div class="value {exp_cls}">{expectancy:+.2f}</div>'
        f'<div class="sub">Per trade, {esc(acc["currency"])}</div></div>',
    ])

    # ---- insights / recommendations ----
    insights_html = "".join(
        f'<div class="ins {i["type"]}"><div class="t">{esc(i["title"])}</div>'
        f'<div class="b">{esc(i["body"])}</div></div>'
        for i in analysis["insights"]) or '<div class="ins info"><div class="b">No insights.</div></div>'
    recs_html = "".join(f"<li>{esc(r)}</li>" for r in analysis["recommendations"])
    grade_cls = "g-" + analysis["grade"].lower().replace("+", "p")

    # ---- psychology ----
    pa = stats.get("psychology_averages")
    psych_cards = ""
    if pa:
        for label, key in [("Mood", "mood_score"), ("Focus", "focus_score"),
                           ("Stress", "stress_score"), ("Suitability", "suitability_score"),
                           ("Discipline", "discipline_score"), ("Overall", "overall_score")]:
            v = pa[key]
            psych_cards += (
                f'<div class="pcard"><div class="pl">{label}</div>'
                f'<div class="pv {_psych_color(v)}">{v}</div>'
                f'<div class="track"><i style="width:{v}%;background:{_psych_track_color(v)}"></i></div>'
                f'<div class="ps">avg /100</div></div>')
    psych_grid = (f'<div class="pgrid">{psych_cards}</div>' if psych_cards else
                  "<p class='muted'>No psychology data collected.</p>")

    ps = stats["by_psychology"]
    psych_items = sorted([(k.replace("_", " ").title(), v["net_profit"])
                          for k, v in ps.items() if v["trades"] > 0],
                         key=lambda x: x[1], reverse=True)
    psych_rows = []
    for k, v in sorted(ps.items(), key=lambda x: x[1]["net_profit"], reverse=True):
        if not v["trades"]:
            continue
        cls = "pos" if v["net_profit"] >= 0 else "neg"
        psych_rows.append(
            f'<tr><td><b>{esc(k.replace("_", " ").title())}</b></td>'
            f'<td class="r">{v["trades"]}</td>'
            f'<td class="r">{v["win_rate"]}%</td>'
            f'<td class="r {cls}"><b>{v["net_profit"]:+.2f}</b></td></tr>')
    psych_table = (
        f'<div class="tbl-wrap"><table><thead><tr><th>Flag</th><th class="r">Trades</th>'
        f'<th class="r">Win %</th><th class="r">Net</th></tr></thead>'
        f'<tbody>{"".join(psych_rows)}</tbody></table></div>' if psych_rows else "")

    # ---- charts ----
    strat_items = sorted([(k, v["net_profit"]) for k, v in stats["by_strategy"].items()
                          if v["trades"] > 0], key=lambda x: x[1], reverse=True)
    sym_items = sorted([(k, v["net_profit"]) for k, v in stats["by_symbol"].items()
                        if v["trades"] > 0], key=lambda x: x[1], reverse=True)
    exits = defaultdict(int)
    for t in history:
        exits[t.get("exit_reason") or "unknown"] += 1
    exit_items = sorted(exits.items(), key=lambda x: x[1], reverse=True)

    # ---- trade table data (embedded as JSON for the JS table) ----
    table_rows = []
    for t in history:
        p = t.get("psychology")
        table_rows.append({
            "open": t["open_time"] or "", "close": t["close_time"] or "",
            "symbol": t["symbol"], "type": t["type"], "vol": t["volume"],
            "exit": t["exit_reason"], "duration": t["duration"] or "",
            "strategy": t["strategy"] or "Untagged",
            "psych": p["overall_score"] if p else None,
            "net": t["net_profit"], "comment": t["comment"] or "",
        })
    open_rows = [{
        "ticket": p["ticket"], "symbol": p["symbol"], "type": p["type"],
        "volume": p["volume"], "open_time": p["open_time"] or "",
        "open_price": p["open_price"], "sl": p["sl"], "tp": p["tp"],
        "current_price": p["current_price"], "swap": p["swap"],
        "profit": p["profit"], "comment": p["comment"] or "",
    } for p in open_positions]
    data_js = json.dumps({"trades": table_rows, "open": open_rows},
                         ensure_ascii=False, default=str).replace("</", "<\\/")

    # ---- open positions table ----
    if open_rows:
        op_body = "".join(
            f'<tr><td>{esc(p["ticket"])}</td><td><b>{esc(p["symbol"])}</b></td>'
            f'<td>{esc(p["type"])}</td><td class="r">{p["volume"]}</td>'
            f'<td>{esc(p["open_time"])}</td><td class="r">{p["open_price"]}</td>'
            f'<td class="r">{p["current_price"]}</td><td class="r">{p["sl"] or "—"}</td>'
            f'<td class="r">{p["tp"] or "—"}</td>'
            f'<td class="r">{p["swap"]:+.2f}</td>'
            f'<td class="r {"pos" if p["profit"] >= 0 else "neg"}"><b>{p["profit"]:+.2f}</b></td>'
            f'<td class="comment">{esc(p["comment"])}</td></tr>'
            for p in open_rows)
        open_html = f'''<div class="tbl-wrap"><table>
  <thead><tr><th>Ticket</th><th>Symbol</th><th>Type</th><th class="r">Vol</th><th>Open</th>
    <th class="r">Open px</th><th class="r">Current</th><th class="r">SL</th><th class="r">TP</th>
    <th class="r">Swap</th><th class="r">Profit</th><th>Comment</th></tr></thead>
  <tbody>{op_body}</tbody></table></div>'''
    else:
        open_html = "<p class='muted'>No open positions.</p>"

    # ---- navigation ----
    nav_items = [("overview", "▦", "Overview"), ("ai", "✦", "AI analysis"),
                 ("psych", "◍", "Psychology"), ("charts", "▲", "Charts"),
                 ("trades", "≡", "Trades"), ("open", "◌", "Open positions")]
    nav_html = "".join(
        f'<button class="nav-btn{" active" if i == 0 else ""}" '
        f'data-target="{tid}"><i>{ic}</i>{esc(label)}</button>'
        for i, (tid, ic, label) in enumerate(nav_items))

    th_cols = [("open", "Open", ""), ("close", "Close", ""), ("symbol", "Symbol", ""),
               ("type", "Type", ""), ("vol", "Vol", "r"), ("exit", "Exit", ""),
               ("duration", "Dur", ""), ("strategy", "Strategy", ""),
               ("psych", "Psych", "r"), ("net", "Net", "r"), ("comment", "Comment", "")]
    thead_html = "".join(
        f'<th class="sortable{(" " + c) if c else ""}" data-key="{k}">'
        f'{esc(label)}<span class="arw">&#8645;</span></th>'
        for k, label, c in th_cols)

    html = f'''<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trading Report — {esc(acc["login"])}</title>
<style>{_CSS}</style>
<noscript><style>section{{display:block!important}}</style></noscript>
</head>
<body>
<div class="app">
  <aside class="side">
    <div class="brand">
      <div class="logo">W</div>
      <div><b>Winrate Analyse</b><small>AI trading report</small></div>
    </div>
    {nav_html}
    <div class="foot">Generated<br>{esc(payload["exported_at"])}</div>
  </aside>

  <main class="main">
    <div class="topbar">
      <div>
        <h1>Account {esc(acc["login"])}</h1>
        <div class="chips">
          <span class="chip">Server <b>{esc(acc["server"])}</b></span>
          <span class="chip">Balance <b>{acc["balance"]} {esc(acc["currency"])}</b></span>
          <span class="chip">Equity <b>{acc["equity"]} {esc(acc["currency"])}</b></span>
          <span class="chip">Leverage <b>1:{acc["leverage"]}</b></span>
          <span class="chip">Open <b>{s["open_positions"]}</b></span>
        </div>
      </div>
      <button class="icon-btn" id="themeBtn" type="button">Light mode</button>
    </div>

    <section id="overview" class="show">
      <h2 class="sec">Summary</h2>
      <div class="grid-kpi">{cards}</div>
      <div class="two" style="margin-top:16px">
        <div class="panel chart"><h3>Equity curve</h3>{_svg_equity_curve(history)}</div>
        <div class="panel">
          <h3>Win / loss distribution</h3>
          <div style="display:flex;gap:20px;align-items:center;flex-wrap:wrap">
            <div class="donut" style="--w:{o['win_rate']}">
              <div class="donut-in"><div>{o['win_rate']}%<small>{o['wins']}W / {o['losses']}L</small></div></div>
            </div>
            <div class="kv" style="grid-template-columns:1fr 1fr;flex:1;min-width:210px">
              <div><span>Best trade</span><b class="pos">{best:+.2f}</b></div>
              <div><span>Worst trade</span><b class="neg">{worst:+.2f}</b></div>
              <div><span>Avg duration</span><b>{avg_dur}</b></div>
              <div><span>Expectancy</span><b class="{exp_cls}">{expectancy:+.2f}</b></div>
            </div>
          </div>
        </div>
      </div>
    </section>

    <section id="ai">
      <h2 class="sec">AI analysis</h2>
      <div class="panel">
        <div class="ai-top">
          <div class="ring" style="--v:{analysis['score']}">
            <div class="ring-in">
              <div><div class="grade {grade_cls}">{esc(analysis['grade'])}</div>
              <small>{analysis['score']} / 100</small></div>
            </div>
          </div>
          <div style="flex:1;min-width:250px">
            <div class="bar-line"><i style="width:{analysis['score']}%"></i></div>
            <div class="kv" style="margin-top:14px">
              <div><span>Profit factor</span><b>{esc(analysis['profit_factor'])}</b></div>
              <div><span>Risk / reward</span><b>{analysis['risk_reward']}</b></div>
              <div><span>Win rate</span><b>{o['win_rate']}%</b></div>
              <div><span>Expectancy</span><b class="{exp_cls}">{expectancy:+.2f}</b></div>
            </div>
          </div>
        </div>
        <h3 style="margin-top:24px">Insights</h3>
        <div class="ins-grid">{insights_html}</div>
        <h3 style="margin-top:24px">Recommendations</h3>
        <ul class="recs">{recs_html}</ul>
      </div>
    </section>

    <section id="psych">
      <h2 class="sec">Psychology &amp; discipline</h2>
      {psych_grid}
      <div class="two" style="margin-top:16px">
        <div class="panel bars"><h3>Net P/L by psychology flag</h3>{_bar_rows(psych_items)}</div>
        <div class="panel"><h3>Flag breakdown</h3>{psych_table or "<p class='muted'>No data.</p>"}</div>
      </div>
    </section>

    <section id="charts">
      <h2 class="sec">Performance breakdown</h2>
      <div class="two">
        <div class="panel bars"><h3>Net P/L by strategy</h3>{_bar_rows(strat_items)}</div>
        <div class="panel bars"><h3>Net P/L by symbol</h3>{_bar_rows(sym_items)}</div>
      </div>
      <div class="two" style="margin-top:16px">
        <div class="panel bars"><h3>Exit reasons (count)</h3>{_bar_rows(exit_items, mode="count")}</div>
        <div class="panel chart"><h3>Equity curve</h3>{_svg_equity_curve(history)}</div>
      </div>
      <div class="panel" style="margin-top:16px">
        <h3>By strategy</h3>{_group_table(stats["by_strategy"], "Strategy")}
      </div>
      <div class="panel">
        <h3>By symbol</h3>{_group_table(stats["by_symbol"], "Symbol")}
      </div>
    </section>

    <section id="trades">
      <h2 class="sec">Trade history ({len(history)})</h2>
      <div class="toolbar">
        <input type="search" id="q" placeholder="Search symbol, strategy, exit, comment…">
        <select id="fStrategy"><option value="all">All strategies</option></select>
        <select id="fResult">
          <option value="all">All results</option>
          <option value="win">Wins only</option>
          <option value="loss">Losses only</option>
        </select>
        <button class="icon-btn" id="csvBtn" type="button">Export CSV</button>
      </div>
      <p class="summary-line" id="tblSummary"></p>
      <div class="tbl-wrap">
        <table>
          <thead><tr>{thead_html}</tr></thead>
          <tbody id="tbody"></tbody>
        </table>
        <div class="empty" id="emptyMsg">No trades match the current filters.</div>
      </div>
    </section>

    <section id="open">
      <h2 class="sec">Open positions ({len(open_rows)})</h2>
      {open_html}
    </section>

    <footer>Generated by winrate-analyse · {esc(datetime.now(timezone.utc).isoformat())}</footer>
  </main>
</div>
<script>window.__REPORT__={data_js};</script>
<script>{_JS}</script>
</body>
</html>'''

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)
    return filepath


# ==================================================================
# Main export
# ==================================================================
def load_existing_trades(filepath):
    """Load a previously exported trades payload. Returns None if missing/broken."""
    if not os.path.exists(filepath):
        return None
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError):
        return None
    if not isinstance(data, dict) or not data.get("history"):
        return None
    data.setdefault("open_positions", [])
    data.setdefault("stats", compute_stats(data["history"]))
    return data


def _render_saved_report(payload, json_path, html_path):
    """Re-render the HTML report from an already-saved payload (no MT5 needed)."""
    print("\nRunning AI analysis...")
    analysis = ai_analyze(payload)
    render_html_report(payload, analysis, html_path)
    o = payload["stats"]["overall"]
    currency = payload.get("account", {}).get("currency", "")
    print(f"\nUsed data from    : '{json_path}' (not re-exported)")
    print(f"HTML report       → '{html_path}'")
    print(f"  Trades         : {len(payload['history'])}")
    print(f"  Open positions : {len(payload.get('open_positions', []))}")
    print(f"  Net result     : {o['net_profit']} {currency}")
    print(f"  Win rate       : {o['win_rate']}%  ({o['wins']}W / {o['losses']}L)")
    print(f"  AI grade       : {analysis['grade']} ({analysis['score']}/100)")
    if payload["stats"].get("psychology_averages"):
        pa = payload["stats"]["psychology_averages"]
        print(f"  Psychology     : {pa['psychology_score']}/100 "
              f"| Discipline: {pa['discipline_score']}/100")


def export_mt5_trades(json_path=TRADES_DB, html_path=REPORT_HTML, ask_strategies=True):
    # ---- Existing trades.json? Ask before pulling fresh data from MT5 ----
    existing = load_existing_trades(json_path)
    if existing:
        exported_at = str(existing.get("exported_at", "unknown"))[:19].replace("T", " ")
        print(f"\nFound existing trade database '{json_path}' with "
              f"{len(existing['history'])} trade(s) (exported {exported_at}).")
        try:
            answer = input("Re-export fresh data from MetaTrader 5? (y/n): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "y"
        if answer not in ("y", "yes"):
            print("Skipping MT5 export. Re-rendering report from saved data.")
            _render_saved_report(existing, json_path, html_path)
            return

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
        if stats.get("psychology_averages"):
            pa = stats["psychology_averages"]
            print(f"  Psychology     : {pa['psychology_score']}/100 "
                  f"| Discipline: {pa['discipline_score']}/100")
    finally:
        mt5.shutdown()


def main():
    strategy_step()
    export_mt5_trades(ask_strategies=True)


if __name__ == "__main__":
    main()