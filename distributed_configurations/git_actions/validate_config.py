#!/usr/bin/env python3

from pathlib import Path
import sys


REQUIRED_PATHS = [
    "config",
]


REQUIRED_FILES = [
    "config/core/roots.yml",
    "config/core/templates.yml",
]


def fail(message):
    print(f"VALIDATION FAILED: {message}")
    sys.exit(1)


def main():

    print("Validating FPT configuration...")

    for path in REQUIRED_PATHS:

        if not Path(path).exists():
            fail(
                f"Required directory is missing: {path}"
            )

    for path in REQUIRED_FILES:

        file_path = Path(path)

        if not file_path.exists():
            fail(
                f"Required file is missing: {path}"
            )

        if file_path.stat().st_size == 0:
            fail(
                f"Required file is empty: {path}"
            )

    print("Configuration validation passed.")


if __name__ == "__main__":
    main()