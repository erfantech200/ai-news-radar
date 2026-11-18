"""Daily AI news radar. Usage:
    python main.py              # full run, sends to Telegram
    python main.py --dry-run    # print the report instead of sending
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from radar.pipeline import run


def main() -> int:
    parser = argparse.ArgumentParser(description="AI news radar")
    parser.add_argument("--config", default=str(Path(__file__).parent / "config.yaml"))
    parser.add_argument("--dry-run", action="store_true", help="print instead of sending")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run(Path(args.config), dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
