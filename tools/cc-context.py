#!/usr/bin/env python3
"""Print each distinct stretch of text around a search term in a large file.

Built for reading Claude Code's native binary, whose bundled JavaScript holds
the compaction logic this project depends on. Minified names change with every
release, so search by stable strings instead, for example:

    "Compacted while idle"              the idle-compaction notice
    "idleCompaction"                    the setting, read by the idle timer
    "CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS"
    'hook_event_name:"PreCompact"'      the PreCompact hook runner
    "compaction blocked by PreCompact hook"
    "tengu_idle_compact_fired"          telemetry at each idle-timer firing

Usage:
    python3 -I tools/cc-context.py [--before N] [--after N] <file> <term> [<term> ...]

Each match prints N bytes before and after it (default 600 each). Identical
stretches print once, since the bundle can hold the same source more than once.
Match counts go to stderr. The system grep on some machines is ugrep, which
refuses long context patterns; this script has no such limit.
"""
import argparse
import mmap
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--before", type=int, default=600)
    parser.add_argument("--after", type=int, default=600)
    parser.add_argument("file")
    parser.add_argument("terms", nargs="+")
    args = parser.parse_args()

    seen = set()
    with open(args.file, "rb") as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        for term in args.terms:
            needle = term.encode()
            start = total = printed = 0
            while True:
                i = mm.find(needle, start)
                if i < 0:
                    break
                total += 1
                chunk = mm[max(0, i - args.before):min(len(mm), i + len(needle) + args.after)]
                text = chunk.decode("utf-8", "replace")
                if text not in seen:
                    seen.add(text)
                    printed += 1
                    print(f"=== {term} @ byte {i}")
                    print(text)
                    print()
                start = i + len(needle)
            print(f"### {term}: {total} matches, {printed} printed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
