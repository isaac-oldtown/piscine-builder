import sys

from . import LEVELS, parse_line


def main():
    counts = {}
    skipped = 0
    for line in sys.stdin:
        if not line.strip():
            continue
        entry = parse_line(line)
        if entry is None:
            skipped += 1
            continue
        counts[entry["level"]] = counts.get(entry["level"], 0) + 1
    for level in LEVELS:
        if level in counts:
            print(level, counts[level])
    if skipped:
        print("skipped %d malformed line(s)" % skipped, file=sys.stderr)
        return 1
    return 0


sys.exit(main())
