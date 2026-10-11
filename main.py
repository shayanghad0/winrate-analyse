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
    """Save strategies to the JSON database, appending to existing ones."""
    existing = load_existing_strategies(filepath)
    combined = existing + new_strategies

    payload = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "count": len(combined),
        "strategies": combined,
    }

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=4, ensure_ascii=False)

    return combined


def main():
    strategies = collect_strategies()

    if not strategies:
        print("\nNo strategies entered. Nothing to save.")
        return

    all_strategies = save_strategies(DATABASE_FILE, strategies)

    print(f"\nSaved {len(strategies)} new strategy(ies) to '{DATABASE_FILE}'.")
    print(f"Total strategies in database: {len(all_strategies)}")


if __name__ == "__main__":
    main()