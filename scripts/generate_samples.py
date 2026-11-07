"""Generate synthetic (fictional) sample documents for demos and manual testing.

Usage:  python scripts/generate_samples.py samples
The output folder is git-ignored. No real student data is involved.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from pps_ats.devtools.synthetic import generate_all


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", type=Path, nargs="?", default=Path("samples"))
    args = parser.parse_args()
    for path in generate_all(args.directory):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
