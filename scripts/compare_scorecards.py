#!/usr/bin/env python
"""Diff two scorecards so a change's effect is a number, not an impression.

    python scripts/compare_scorecards.py eval/baseline_scorecard.json var/new.json

Exits non-zero if any metric regressed, so it can gate a merge once the two
lanes are landing changes daily.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

# Metrics where a larger number is better. Anything not listed is treated as a
# rate that should fall (overclaims, misses).
HIGHER_IS_BETTER = {
    "profile_inferred_rate",
    "modality_recall",
    "layer_recall",
    "pairing_recall",
    "deposition_precision",
}


def _fmt(value: float | None) -> str:
    return "  n/a" if value is None else f"{value:.4f}"


def _delta(before: float | None, after: float | None, higher_better: bool) -> tuple[str, bool]:
    if before is None or after is None:
        return "", False
    change = after - before
    if abs(change) < 1e-9:
        return "     =", False
    improved = change > 0 if higher_better else change < 0
    return f"{change:+.4f} {'better' if improved else 'WORSE'}", not improved


def compare(before: dict[str, Any], after: dict[str, Any]) -> int:
    regressions = 0
    print("inference")
    for key in sorted(set(before["inference"]) | set(after["inference"])):
        old, new = before["inference"].get(key), after["inference"].get(key)
        if not isinstance(old, (int, float)) and not isinstance(new, (int, float)):
            # List-valued diagnostics: report membership changes, not a delta.
            fixed = sorted(set(old or []) - set(new or []))
            broke = sorted(set(new or []) - set(old or []))
            if fixed or broke:
                print(f"  {key}")
                for item in fixed:
                    print(f"    resolved: {item}")
                for item in broke:
                    print(f"    NEW:      {item}")
                    regressions += 1
            continue
        text, regressed = _delta(old, new, key in HIGHER_IS_BETTER)
        regressions += int(regressed)
        print(f"  {key:28} {_fmt(old)} -> {_fmt(new)}  {text}")

    print("\nquestions")
    after_by_id = {item["id"]: item for item in after["questions"]}
    for old_q in before["questions"]:
        new_q = after_by_id.get(old_q["id"])
        if new_q is None:
            print(f"  {old_q['id']}: missing from the new scorecard")
            continue
        print(f"  {old_q['id']}")
        text, regressed = _delta(
            old_q["deposition_precision"], new_q["deposition_precision"], True
        )
        regressions += int(regressed)
        print(
            f"    deposition_precision      "
            f"{_fmt(old_q['deposition_precision'])} -> "
            f"{_fmt(new_q['deposition_precision'])}  {text}"
        )
        for field in ("decisions", "modality_gate"):
            changes = [
                f"{key}: {old_q[field].get(key, 0)} -> {new_q[field].get(key, 0)}"
                for key in sorted(set(old_q[field]) | set(new_q[field]))
                if old_q[field].get(key, 0) != new_q[field].get(key, 0)
            ]
            if changes:
                print(f"    {field:25} {'; '.join(changes)}")
        missing = set(new_q["expected_recall_missing"]) - set(
            old_q["expected_recall_missing"]
        )
        if missing:
            print(f"    NEWLY UNRECALLED: {sorted(missing)}")
            regressions += len(missing)
        # An absolute check, not a delta: a record the question forbids is a
        # regression even if the baseline also admitted it. This is the one
        # place where matching the baseline is not good enough.
        forbidden = sorted(new_q.get("forbidden_passing_modality_gate", []))
        if forbidden:
            print(f"    FORBIDDEN, YET ADMITTED: {forbidden}")
            regressions += len(forbidden)

    print(f"\n{regressions} regression(s)")
    return 1 if regressions else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    args = parser.parse_args()
    return compare(
        json.loads(args.before.read_text(encoding="utf-8")),
        json.loads(args.after.read_text(encoding="utf-8")),
    )


if __name__ == "__main__":
    raise SystemExit(main())
