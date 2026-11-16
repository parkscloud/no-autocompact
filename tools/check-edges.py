#!/usr/bin/env python3
"""Live checks 4 and 5 of DESIGN.md §7.2, in throwaway Claude Code sessions (about 10 minutes).

4. Near-limit compaction still runs in an armed session. With CLAUDE_CODE_AUTO_COMPACT_WINDOW
   at its 100K minimum the threshold sits near 67K tokens; the session is armed, then reads
   files until it passes that point mid-turn. The compaction must happen (trigger `auto`), and
   the guard must not veto it.
5a. An interrupt fires neither Stop nor StopFailure, so the turn stays open in the plugin's
   state until the next turn ends (the guard's freshness test covers the gap, DESIGN.md §6.6).
5b. After /clear, a keep-warm schedule from before is either gone, or its firings are skipped
   in the new, unarmed session, Claude mentions /no-autocompact:off once, and that command
   deletes the schedule.

Usage: python3 -I tools/check-edges.py [--model claude-haiku-5-5] [--only 4|5]
Writes build/edges-<time>/result.json.
"""
import argparse
import importlib.util
import json
import random
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ptysession as ps  # noqa: E402

REPO = HERE.parent
_spec = importlib.util.spec_from_file_location("nac", str(REPO / "scripts" / "nac.py"))
nac = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nac)

DATA_ROOT = Path.home() / ".claude" / "plugins" / "data"
WORDS = "amber basil cobalt dune ember fjord garnet harbor indigo juniper kestrel lantern".split()


def state_of(sid):
    for p in DATA_ROOT.glob("*/sessions/%s.json" % sid):
        try:
            return json.loads(p.read_text())
        except ValueError:
            pass
    return None


def states_since(t, exclude):
    out = {}
    for p in DATA_ROOT.glob("*/sessions/*.json"):
        if p.stem in exclude or p.stat().st_mtime < t:
            continue
        try:
            out[p.stem] = json.loads(p.read_text())
        except ValueError:
            pass
    return out


def make_files(d, count, lines=700):
    rng = random.Random(7)
    for i in range(1, count + 1):
        text = "\n".join("Line %d of file %d: %s." % (n, i, " ".join(rng.choice(WORDS) for _ in range(6)))
                         for n in range(1, lines + 1))
        (d / ("file%d.txt" % i)).write_text(text + "\n")


class Session:
    def __init__(self, name, root, model, **env):
        self.dir = root / name
        self.dir.mkdir(parents=True)
        self.name, self.t0 = name, time.time()
        self.s = ps.start(["claude", "--model", model, "--plugin-dir", str(REPO), "-n",
                           "no-autocompact-edges-" + name], self.dir, self.dir / "screen.raw",
                          env=ps.clean_env(**env))
        if self.s is None:
            raise RuntimeError("%s: the input box never appeared" % name)
        self.transcript = None

    def rows(self):
        return ps.read_jsonl(self.transcript) if self.transcript else []

    def find_transcript(self, since):
        self.transcript = ps.wait_for(lambda: ps.newest_transcript(self.dir, since), 60)
        return self.transcript

    def replies(self):
        return sum(1 for r in self.rows() if r.get("type") == "assistant"
                   and '"text"' in json.dumps((r.get("message") or {}).get("content")))

    def ask(self, text, timeout=300, settle=15):
        before = self.replies()
        self.s.type_line(text)
        if self.transcript is None:
            self.find_transcript(self.t0)
        if not ps.wait_for(lambda: self.replies() > before, timeout, every=2):
            raise RuntimeError("%s: no reply to %r within %d s" % (self.name, text[:60], timeout))
        time.sleep(settle)

    @property
    def sid(self):
        return self.transcript.stem if self.transcript else None


def check_near_limit(root, model, res):
    s = Session("near-limit", root, model, CLAUDE_CODE_AUTO_COMPACT_WINDOW="100000")
    try:
        make_files(s.dir, 3)
        s.ask("/no-autocompact:on")
        st = state_of(s.sid) or {}
        res["4_armed"] = st.get("armed") is True
        s.ask("Read file1.txt, file2.txt and file3.txt in this folder in full with the Read tool, "
              "one call each, then reply with only: done", timeout=420)
        rows = s.rows()
        boundaries = [r.get("compactMetadata") or {} for r in rows if r.get("subtype") == "compact_boundary"]
        res["4_compactions"] = boundaries
        blocked = [r for r in rows if "blocked by PreCompact" in json.dumps(r)]
        st = state_of(s.sid) or {}
        res["4_vetoes"] = (st.get("vetoes") or {}).get("count")
        res["4_near_limit_compacted"] = any(b.get("trigger") == "auto" for b in boundaries)
        res["4_not_vetoed"] = not blocked and res["4_vetoes"] == 0
    finally:
        s.s.kill()


def check_interrupt_and_clear(root, model, res):
    s = Session("interrupt-clear", root, model)
    try:
        s.ask("/no-autocompact:on")
        old_sid = s.sid
        st = state_of(old_sid) or {}
        res["5_armed"] = st.get("armed") is True
        ok_end_before = st.get("last_ok_turn_end")

        # 5a. Interrupt a turn while its tool runs.
        # Not a bare `sleep`: Claude Code refuses "standalone sleep" commands outright.
        s.s.type_line("Run this exact shell command with the Bash tool: "
                      "python3 -c 'import time; time.sleep(40)' -- then reply with only: done")
        started = ps.wait_for(lambda: ps.tool_uses(s.rows(), "Bash"), 90, every=1)
        res["5a_tool_started"] = bool(started)
        time.sleep(3)
        s.s.send("\x1b")
        time.sleep(12)
        st = state_of(old_sid) or {}
        res["5a_interrupt_marker"] = any("interrupted" in json.dumps(r).lower() for r in s.rows()[-8:])
        res["5a_turn_left_open"] = st.get("turn_open") is True
        res["5a_no_good_end_recorded"] = st.get("last_ok_turn_end") == ok_end_before
        s.ask("Reply with only: ok")
        st = state_of(old_sid) or {}
        res["5a_next_turn_closes_it"] = st.get("turn_open") is False

        # 5b. A schedule from before /clear.
        s.ask("Use CronCreate to schedule a recurring task with cron '*/1 * * * *' and this exact "
              "prompt: " + nac.TICK_PROMPT + " -- then reply with only the task id.")
        t_clear = time.time()
        s.s.type_line("/clear")
        time.sleep(5)
        orphan = ps.wait_for(lambda: {k: v for k, v in states_since(t_clear, {old_sid}).items()
                                      if v.get("orphan_ticks")}, 150, every=3)
        res["5b_ticks_after_clear"] = bool(orphan)
        if not orphan:
            # The schedule may have ended with the conversation; ask Claude Code itself.
            s.ask("Call CronList and reply with only the number of tasks it lists.")
            s.find_transcript(t_clear)
            res["5b_cronlist_after_clear"] = [r for r in s.rows() if r.get("type") == "user"
                                              and "tool_result" in json.dumps(r)][-1:]
            return
        new_sid, new_state = next(iter(orphan.items()))
        res["5b_new_session_unarmed"] = not new_state.get("armed")
        # /clear started a new transcript; follow it, or replies would be counted in the old one.
        s.transcript = Path.home() / ".claude" / "projects" / ps.project_slug(s.dir) / (new_sid + ".jsonl")
        s.ask("What is 3 + 4? Reply with just the number.")
        later = s.rows()
        res["5b_reminder_added"] = any(r.get("type") == "attachment" and "/no-autocompact:off" in json.dumps(r)
                                       for r in later)
        res["5b_tools_in_that_turn"] = [u[1] for u in ps.tool_uses(later) if u[1].startswith("Cron")]
        s.ask("/no-autocompact:off")
        res["5b_cron_deletes"] = len(ps.tool_uses(s.rows(), "CronDelete"))
        before = (state_of(new_sid) or {}).get("orphan_ticks", 0)
        time.sleep(100)
        res["5b_ticks_stopped"] = (state_of(new_sid) or {}).get("orphan_ticks", 0) == before
    finally:
        s.s.kill()


def main():
    ap = argparse.ArgumentParser(description="Live checks 4 and 5.")
    ap.add_argument("--model", default="claude-haiku-5-5")
    ap.add_argument("--only", choices=["4", "5"], help="run just one of the two checks")
    args = ap.parse_args()
    root = REPO / "build" / time.strftime("edges-%Y%m%dT%H%M%S")
    root.mkdir(parents=True)
    settings = Path.home() / ".claude" / "settings.json"
    before = settings.read_bytes() if settings.exists() else None
    res = {"root": str(root)}
    try:
        if args.only != "5":
            check_near_limit(root, args.model, res)
        if args.only != "4":
            check_interrupt_and_clear(root, args.model, res)
    except Exception as e:  # keep what was learned
        res["error"] = repr(e)
    finally:
        res["settings_json_unchanged"] = (settings.read_bytes() if settings.exists() else None) == before
        (root / "result.json").write_text(json.dumps(res, indent=2))
    print(json.dumps(res, indent=2))
    checks = ["settings_json_unchanged"]
    if args.only != "5":
        checks += ["4_armed", "4_near_limit_compacted", "4_not_vetoed"]
    if args.only != "4":
        checks += ["5_armed", "5a_tool_started", "5a_interrupt_marker", "5a_turn_left_open",
                   "5a_no_good_end_recorded", "5a_next_turn_closes_it"]
    if res.get("5b_ticks_after_clear"):
        checks += ["5b_new_session_unarmed", "5b_reminder_added", "5b_ticks_stopped"]
    failed = [c for c in checks if not res.get(c)]
    if res.get("5b_tools_in_that_turn"):
        failed.append("5b_tools_in_that_turn")
    if res.get("5b_ticks_after_clear") and not res.get("5b_cron_deletes"):
        failed.append("5b_cron_deletes")
    if "error" in res:
        failed.append("error")
    print("FAILED: " + ", ".join(failed) if failed else "ALL CHECKS PASSED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
