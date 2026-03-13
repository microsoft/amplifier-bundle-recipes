#!/usr/bin/env python3
"""Verification script for recipe test fixtures.

Checks that each fixture file:
1. Exists and is valid YAML
2. Has the expected keys (or intentionally missing keys)

Expected results:
- valid-recipe.yaml: has 'name' and 'steps' keys
- broken-recipe.yaml: has 'naem' instead of 'name', 'stesp' instead of 'steps'
- warnings-recipe.yaml: has 'name' and 'steps' keys
"""

import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML not installed. Run: pip install pyyaml")
    sys.exit(1)

FIXTURES_DIR = Path(__file__).parent


def check_fixture(filename: str) -> dict | None:
    """Parse a fixture file and return its top-level keys."""
    path = FIXTURES_DIR / filename
    if not path.exists():
        print(f"MISSING: {filename}")
        return None

    with open(path) as f:
        data = yaml.safe_load(f)

    has_name = "name" in data
    has_steps = "steps" in data
    has_naem = "naem" in data
    has_stesp = "stesp" in data
    return {
        "name": has_name,
        "steps": has_steps,
        "naem": has_naem,
        "stesp": has_stesp,
        "keys": list(data.keys()),
    }


def main() -> None:
    failures = []

    print("Checking valid-recipe.yaml...")
    result = check_fixture("valid-recipe.yaml")
    if result is None:
        failures.append("valid-recipe.yaml: file missing")
    else:
        if result["name"] and result["steps"]:
            print("  PASS: has name=True and steps=True")
        else:
            msg = f"  FAIL: name={result['name']}, steps={result['steps']}"
            print(msg)
            failures.append(f"valid-recipe.yaml: {msg}")

    print("Checking broken-recipe.yaml...")
    result = check_fixture("broken-recipe.yaml")
    if result is None:
        failures.append("broken-recipe.yaml: file missing")
    else:
        if (
            not result["name"]
            and not result["steps"]
            and result["naem"]
            and result["stesp"]
        ):
            print("  PASS: has name=False (uses 'naem') and steps=False (uses 'stesp')")
        else:
            msg = (
                f"  FAIL: name={result['name']}, steps={result['steps']}, "
                f"naem={result['naem']}, stesp={result['stesp']}"
            )
            print(msg)
            failures.append(f"broken-recipe.yaml: {msg}")

    print("Checking warnings-recipe.yaml...")
    result = check_fixture("warnings-recipe.yaml")
    if result is None:
        failures.append("warnings-recipe.yaml: file missing")
    else:
        if result["name"] and result["steps"]:
            print("  PASS: has name=True and steps=True")
        else:
            msg = f"  FAIL: name={result['name']}, steps={result['steps']}"
            print(msg)
            failures.append(f"warnings-recipe.yaml: {msg}")

    print()
    if failures:
        print(f"RESULT: {len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print("RESULT: All fixtures verified successfully!")
        sys.exit(0)


if __name__ == "__main__":
    main()
