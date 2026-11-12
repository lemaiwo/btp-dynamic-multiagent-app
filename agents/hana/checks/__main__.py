"""``python -m agents.hana.checks --dump``: the statements as JSON.

The output is ``[{name, sql, binds}]`` with sample binds, the file
``scripts/probe_hana_connectivity.py --checks-file`` reads, so the probe can
run every registered statement on a landscape before the toolset does.
``--category database,schema`` keeps the checks of those categories only, so
the checks of one phase can be probed on their own; a name that is not a
category ends the command with exit code 2 and is not repeated.
"""

from __future__ import annotations

import argparse
import json
import sys

from agents.hana.checks import CATEGORIES, dump_entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agents.hana.checks")
    parser.add_argument("--dump", action="store_true", help="print the statements as JSON")
    parser.add_argument(
        "--category", default=None,
        help="with --dump: comma-separated categories (" + ", ".join(CATEGORIES) + ")")
    args = parser.parse_args(argv)
    if not args.dump:
        parser.print_usage(sys.stderr)
        return 2
    categories = None
    if args.category is not None:
        categories = args.category.split(",")
        if not set(categories) <= set(CATEGORIES):
            # The value is not repeated: it is whatever was typed.
            sys.stderr.write("--category: not one of " + ", ".join(CATEGORIES) + "\n")
            return 2
    json.dump(dump_entries(categories), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
