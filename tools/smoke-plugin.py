#!/usr/bin/env python3
"""Smoke test: load this plugin into a throwaway Claude Code session and exercise it.

DESIGN.md §7.1 stage 3, with check 1 of §7.2. In a fresh directory under build/, it starts
`claude --plugin-dir <repo>` on a cheap model, then:
  1. runs /no-autocompact:on and checks the state file, the CronCreate call (cron and exact
     prompt) and the Stop hook's record of the turn's end;
  2. schedules an extra once-a-minute task carrying the tick marker, and checks that the
     plugin's hook skips its firing ("you're active") without a reply from Claude;
  3. runs /compact by hand and checks that it compacts (the guard never touches manual runs);
  4. runs /no-autocompact:status, then /no-autocompact:off, and checks the disarm and the
     CronDelete calls.
It compares ~/.claude/settings.json before and after, and writes result.json beside the
probe's screen log. Usage: python3 -I tools/smoke-plugin.py [--model claude-haiku-5-5]
"""
import argparse
import importlib.util
import json
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


def state_of(sid):
    for p in (Path.home() / ".claude" / "plugins" / "data").glob("*/sessions/%s.json" % sid):
        try:
            return p, json.loads(p.read_text())
        except ValueError:
            pass
    return None, None


def main():
    ap = argparse.ArgumentParser(description="Smoke-test the plugin in a throwaway session.")
    ap.add_argument("--model", default="claude-haiku-5-5")
    args = ap.parse_args()

    probe = REPO / "build" / time.strftime("smoke-%Y%m%dT%H%M%S")
    probe.mkdir(parents=True)
    settings = Path.home() / ".claude" / "settings.json"
    settings_before = settings.read_bytes() if settings.exists() else None
    t0 = time.time()
    res = {"probe_dir": str(probe)}
    argv = ["claude", "--model", args.model, "--plugin-dir", str(REPO), "-n", "no-autocompact-smoke"]
    s = ps.start(argv, probe, probe / "screen.raw")
    if s is None:
        res["error"] = "the input box never appeared"
        print(json.dumps(res, indent=2))
        return 1
    try:
        # 1. Arm.
        s.type_line("/no-autocompact:on")
        transcript = ps.wait_for(lambda: ps.newest_transcript(probe, t0), 60)
        res["transcript"] = str(transcript)
        if transcript is None:
            raise SystemExit("no transcript appeared")
        sid = transcript.stem
        rows = lambda: ps.read_jsonl(transcript)  # noqa: E731
        made = ps.wait_for(lambda: [u for u in ps.tool_uses(rows(), "CronCreate")
                                    if nac.MARKER in (u[2].get("prompt") or "")], 150)
        res["arm_cron_create"] = made[0][2] if made else None
        res["arm_cron_create_exact"] = bool(made) and made[0][2].get("prompt") == nac.TICK_PROMPT \
            and made[0][2].get("cron") == nac.cron_for(sid)
        st = ps.wait_for(lambda: (lambda p_st: p_st[1] if p_st[1] and p_st[1].get("last_ok_turn_end")
                                  else None)(state_of(sid)), 120)
        res["state_file"] = str(state_of(sid)[0])
        res["armed"] = bool(st and st["armed"])
        res["stop_hook_recorded_turn_end"] = bool(st and st.get("last_ok_turn_end"))

        # 2. A quick tick, from an extra once-a-minute task with the marker.
        s.type_line("Use CronCreate to schedule a recurring task with cron '*/1 * * * *' and this "
                    "exact prompt: " + nac.TICK_PROMPT + " -- then reply with only the task id.")
        ps.wait_for(lambda: len(ps.tool_uses(rows(), "CronCreate")) >= 2, 150)
        skipped = ps.wait_for(lambda: (lambda st2: st2 if st2 and st2["ticks"]["skipped"] >= 1
                                       else None)(state_of(sid)[1]), 180, every=3)
        res["tick_skipped"] = bool(skipped)
        res["tick_skip_reason"] = skipped and skipped["ticks"]["last_skip_reason"]
        time.sleep(15)
        all_rows = rows()
        fires = [i for i, r in enumerate(all_rows) if r.get("subtype") == "scheduled_task_fire"]
        replied = False
        if fires:
            replied = any(r.get("type") == "assistant" for r in all_rows[fires[0] + 1:])
        res["tick_fired_rows"] = len(fires)
        res["claude_replied_after_first_tick"] = replied

        # 3. /compact by hand.
        n_before = len(rows())
        s.type_line("/compact")
        boundary = ps.wait_for(lambda: [r for r in rows()[n_before:]
                                        if r.get("subtype") == "compact_boundary"], 240, every=3)
        res["manual_compact_ran"] = bool(boundary)
        res["manual_compact_trigger"] = boundary and (boundary[0].get("compactMetadata") or {}).get("trigger")
        time.sleep(5)

        # 4. Status, then off.
        n_before = len(rows())
        s.type_line("/no-autocompact:status")
        said = ps.wait_for(lambda: [r for r in rows()[n_before:] if r.get("type") == "assistant"
                                    and "text" in json.dumps(r.get("message") or {})], 120)
        res["status_answered"] = bool(said)
        time.sleep(5)
        s.type_line("/no-autocompact:off")
        ps.wait_for(lambda: (lambda st3: st3 if st3 and not st3["armed"] else None)(state_of(sid)[1]), 120)
        ps.wait_for(lambda: len(ps.tool_uses(rows(), "CronDelete")) >= 1, 150)
        time.sleep(20)
        res["disarmed"] = not state_of(sid)[1]["armed"]
        res["cron_deletes"] = len(ps.tool_uses(rows(), "CronDelete"))
        res["final_state"] = state_of(sid)[1]
    finally:
        s.kill()
        now = settings.read_bytes() if settings.exists() else None
        res["settings_json_unchanged"] = now == settings_before
        (probe / "result.json").write_text(json.dumps(res, indent=2))
        print(json.dumps(res, indent=2))
    checks = ["arm_cron_create_exact", "armed", "stop_hook_recorded_turn_end", "tick_skipped",
              "manual_compact_ran", "status_answered", "disarmed", "settings_json_unchanged"]
    failed = [c for c in checks if not res.get(c)] + (["claude_replied_after_first_tick"]
                                                      if res.get("claude_replied_after_first_tick") else [])
    print("FAILED: " + ", ".join(failed) if failed else "ALL CHECKS PASSED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
