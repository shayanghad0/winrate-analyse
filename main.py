import json
import os
from datetime import datetime, timezone

DATABASE_FILE = "strategies.json"


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


def collect_strategies():
    """Prompt the user for strategies until an empty input is given."""
    strategies = []
    print("Enter your strategies one by one.")
    print("Press ENTER on an empty line to save and exit.\n")

    while True:
        try:
            entry = input(f"Strategy #{len(strategies) + 1}: ").strip()
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

    # Build a case-insensitive lookup of existing strategies
    seen = {s.lower() for s in existing}

    added = []
    skipped = []
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


def main():
    strategies = collect_strategies()

    if not strategies:
        print("\nNo strategies entered. Nothing to save.")
        return

    all_strategies, added, skipped = save_strategies(DATABASE_FILE, strategies)

    if added:
        print(f"\nSaved {len(added)} new strategy(ies) to '{DATABASE_FILE}'.")
        for s in added:
            print(f"  + {s}")

    if skipped:
        print(f"\nSkipped {len(skipped)} duplicate(s):")
        for s in skipped:
            print(f"  - {s}")

    print(f"\nTotal strategies in database: {len(all_strategies)}")


if __name__ == "__main__":
    main()