"""``python -m agents.hana.checks --dump``: the statements as JSON.

The output is ``[{name, sql, binds}]`` with sample binds, the file
``scripts/probe_hana_connectivity.py --checks-file`` reads, so the probe can
run every registered statement on a landscape before the toolset does.
"""

from __future__ import annotations

import argparse
import json
import sys

from agents.hana.checks import dump_entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agents.hana.checks")
    parser.add_argument("--dump", action="store_true", help="print the statements as JSON")
    args = parser.parse_args(argv)
    if not args.dump:
        parser.print_usage(sys.stderr)
        return 2
    json.dump(dump_entries(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
