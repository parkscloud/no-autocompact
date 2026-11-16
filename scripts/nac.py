#!/usr/bin/env python3
"""no-autocompact: keep a chosen Claude Code session from being compacted while its user is away.

One file, Python 3.8 or later, standard library only. It serves the plugin's skills and hooks:

    nac.py arm    --session ID --data DIR   the /no-autocompact:on skill
    nac.py disarm --session ID --data DIR   the /no-autocompact:off skill
    nac.py status --session ID --data DIR   the /no-autocompact:status skill
    nac.py hook EVENT --data DIR            a hook, with Claude Code's hook input JSON on stdin;
                                            EVENT is prompt (UserPromptSubmit), stop (Stop),
                                            stop-failure (StopFailure) or precompact (PreCompact)

DESIGN.md §6 explains each rule; the timings come from Claude Code 2.1.325 (DESIGN.md §2).
A hook never fails loudly: on any error it allows whatever Claude Code was about to do and
appends the error to errors.log in the data directory.
"""
import argparse
import contextlib
import datetime
import hashlib
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows: no locking; hooks of one session rarely overlap
    fcntl = None

MARKER = "[no-autocompact keep-warm]"
TICK_PROMPT = MARKER + " Reply with only: ok"

MINUTE = 60.0
DAY = 24 * 60 * MINUTE
# A tick reaches Claude only between 20 and 53 minutes after the last successful turn. Ticks
# come 30 minutes apart, so the window from 20 to 50 minutes always holds one; it restarts the
# cache's hour before Claude Code's idle timer (54 minutes) fires. Past 53 minutes the cache
# may have expired, and re-caching the conversation is left to the user's own return.
PASS_AFTER = 20 * MINUTE
PASS_UNTIL = 53 * MINUTE
# The guard lets an automatic compaction go ahead only inside a turn (DESIGN.md §6.6).
GUARD_TURN_FRESH = 10 * MINUTE
GUARD_INPUT_FRESH = 2 * MINUTE
# Claude Code ends a recurring scheduled task after seven days.
SCHEDULE_RENEW = 6.9 * DAY
# Idle this long without a tick means the schedule isn't running: before its first tick, the
# first firing can come up to 45 minutes after creation (30-minute spacing plus up to 15 of
# jitter); after that, any 30 idle minutes hold a firing.
LOST_GAP_FIRST = 46 * MINUTE
LOST_GAP = 32 * MINUTE
# A stopped schedule is mentioned to the user at most this often, until it's restarted.
LOST_ASK_EVERY = DAY
PRUNE_AFTER = 30 * DAY
TAIL_BYTES = 256 * 1024
SID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

VETO_REASON = "no-autocompact: idle compaction vetoed; this session keeps its full context"
SKIP_ACTIVE = "you're active"
SKIP_COLD = "the cache has already expired; your next message re-reads the conversation once"
SKIP_UNARMED = "this session isn't armed"


# ---------------------------------------------------------------- state


def sessions_dir(data):
    return Path(data) / "sessions"


def state_path(data, sid):
    return sessions_dir(data) / (sid + ".json")


def new_state(sid):
    return {
        "version": 1,
        "session_id": sid,
        "armed": False,
        "armed_at": None,
        "disarmed_at": None,
        "schedule": None,
        "turn_open": False,
        "last_prompt_at": None,
        "last_turn_end": None,
        "last_ok_turn_end": None,
        "last_tick_seen_at": None,
        "last_lost_ask_at": None,
        "ticks": {"passed": 0, "skipped": 0, "last_passed_at": None, "last_skipped_at": None,
                  "last_skip_reason": None},
        "vetoes": {"count": 0, "last_at": None},
        "orphan_ticks": 0,
        "orphan_cleanup_asked": False,
    }


def load(data, sid):
    try:
        st = json.loads(state_path(data, sid).read_text())
    except (OSError, ValueError):
        return None
    base = new_state(sid)
    base.update(st)
    for key in ("ticks", "vetoes"):
        merged = new_state(sid)[key]
        merged.update(st.get(key) or {})
        base[key] = merged
    return base


def save(data, sid, st):
    path = state_path(data, sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("%s.%d.tmp" % (path.name, os.getpid()))
    tmp.write_text(json.dumps(st, indent=1, sort_keys=True) + "\n")
    os.replace(str(tmp), str(path))


@contextlib.contextmanager
def locked(data, sid):
    """Serialize read-modify-write of one session's state across hook processes."""
    d = sessions_dir(data)
    d.mkdir(parents=True, exist_ok=True)
    with open(str(d / (sid + ".lock")), "a") as f:
        if fcntl is not None:
            fcntl.flock(f, fcntl.LOCK_EX)
        yield


def prune(data, now, keep):
    """Remove state and lock files of sessions untouched for 30 days."""
    d = sessions_dir(data)
    if not d.is_dir():
        return
    for p in d.iterdir():
        if p.name.split(".", 1)[0] == keep:
            continue
        try:
            if now - p.stat().st_mtime > PRUNE_AFTER:
                p.unlink()
        except OSError:
            pass


def cron_for(sid):
    """Two minutes half an hour apart, from 1 to 59, never :00 or :30, spread by session ID."""
    m = 1 + int(hashlib.sha256(sid.encode()).hexdigest(), 16) % 29
    return "%d,%d * * * *" % (m, m + 30)


# ---------------------------------------------------------------- transcript


def parse_ts(value):
    if not isinstance(value, str):
        return None
    s = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def tail_times(path):
    """Read the transcript's last 256 KB and return three times, as epoch seconds or None:
    the newest entry of any kind, the newest user entry (a prompt or a tool result), and the
    newest successful reply (an assistant entry carrying usage, not an API error)."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - TAIL_BYTES))
        chunk = f.read()
    lines = chunk.split(b"\n")
    if size > TAIL_BYTES:
        lines = lines[1:]  # the first line is probably cut
    newest = newest_user = newest_reply = None
    for raw in lines:
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        ts = parse_ts(row.get("timestamp"))
        if ts is None:
            continue
        newest = ts if newest is None else max(newest, ts)
        kind = row.get("type")
        if kind == "user":
            newest_user = ts if newest_user is None else max(newest_user, ts)
        elif kind == "assistant" and not row.get("isApiErrorMessage"):
            msg = row.get("message")
            if isinstance(msg, dict) and msg.get("usage"):
                newest_reply = ts if newest_reply is None else max(newest_reply, ts)
    return newest, newest_user, newest_reply


# ---------------------------------------------------------------- decisions


def tick_skip_reason(st, now, newest_reply):
    """None if a keep-warm tick should reach Claude; otherwise why it is skipped."""
    if st is None or not st.get("armed"):
        return SKIP_UNARMED
    known = [t for t in (st.get("last_ok_turn_end"), newest_reply, st.get("armed_at")) if t]
    if not known:
        return SKIP_ACTIVE
    age = now - max(known)
    if age < PASS_AFTER:
        return SKIP_ACTIVE
    if age > PASS_UNTIL:
        return SKIP_COLD
    return None


def schedule_lost(st, now):
    """True when the session just sat idle long enough for a tick, and none came."""
    sched = st.get("schedule")
    gap_start = st.get("last_turn_end")
    if not sched or gap_start is None:
        return False
    requested = sched.get("requested_at") or 0
    last_tick = st.get("last_tick_seen_at")
    if last_tick is not None and last_tick >= gap_start:
        return False  # a tick came during this idle stretch
    started = last_tick is not None and last_tick >= requested
    gap = now - max(gap_start, requested)
    return gap >= (LOST_GAP if started else LOST_GAP_FIRST)


def in_turn(st, now, newest, newest_user):
    """Whether an automatic compaction is happening inside a turn (DESIGN.md §6.6)."""
    if st.get("turn_open") and newest is not None and now - newest < GUARD_TURN_FRESH:
        return True
    return newest_user is not None and now - newest_user < GUARD_INPUT_FRESH


# ---------------------------------------------------------------- notes for Claude


def create_steps(cron, replace):
    first = ("call CronList and delete, with CronDelete, every task whose prompt starts with "
             '"%s"; then ' % MARKER) if replace else ""
    return ('%scall CronCreate with cron "%s", prompt "%s" and recurring true. Use that exact '
            "prompt: the plugin's hook recognizes it and lets it reach you only while the user "
            "is away." % (first, cron, TICK_PROMPT))


# Upkeep notes ask Claude only to tell the user which command fixes things, never to call a
# tool inside the user's turn: a permission check can refuse such a call, as the auto-mode
# classifier did in the live run of 2026-11-15 (DESIGN.md D10). The commands' own skills
# pre-approve the scheduling tools.


def stopped_note():
    return ("no-autocompact: this session's keep-warm schedule has stopped (Claude Code ends a "
            "recurring task after seven days, and a restart can drop it). Idle compaction is "
            "still vetoed, but the prompt cache can now expire while the user is away. Tell the "
            "user in one short line that /no-autocompact:on restarts the schedule, then carry on "
            "with their message. Don't call any tool for this.")


def orphan_note():
    return ("no-autocompact: a keep-warm schedule from earlier is still firing in this session, "
            "which isn't armed. The plugin skips each firing at no cost, but each one leaves a "
            "notice. Tell the user in one short line that /no-autocompact:off removes the "
            "schedule, then carry on with their message. Don't call any tool for this.")


def schedule_current(st, now):
    """Whether the session's keep-warm schedule should still be running."""
    sched = st.get("schedule")
    return (sched is not None and not sched.get("stopped_at")
            and now - (sched.get("requested_at") or 0) < SCHEDULE_RENEW)


def when(ts, now):
    if not ts:
        return "never"
    ago = now - ts
    if ago < 90:
        rel = "just now"
    elif ago < 90 * MINUTE:
        rel = "%d min ago" % round(ago / MINUTE)
    elif ago < 2 * DAY:
        rel = "%.1f h ago" % (ago / 3600)
    else:
        rel = "%.1f days ago" % (ago / DAY)
    return "%s (%s)" % (time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)), rel)


# ---------------------------------------------------------------- skill commands


def cmd_arm(data, sid, now):
    with locked(data, sid):
        st = load(data, sid) or new_state(sid)
        was_armed = bool(st["armed"])
        had_schedule = st.get("schedule") is not None
        current = schedule_current(st, now)
        # A schedule may already exist: an old or stopped one being replaced, or one left over
        # from before /clear whose ticks this session has seen. Clear it before creating another.
        replace = had_schedule or st.get("orphan_ticks", 0) > 0
        st["armed"] = True
        if not was_armed:
            st["armed_at"] = now
            st["disarmed_at"] = None
        st["turn_open"] = True  # this runs inside the arming turn
        st["last_prompt_at"] = now
        st["orphan_ticks"] = 0
        st["orphan_cleanup_asked"] = False
        if not (was_armed and current):
            st["schedule"] = {"cron": cron_for(sid), "requested_at": now}
            st["last_lost_ask_at"] = None  # remind again promptly if this one stops too
        save(data, sid, st)
        prune(data, now, keep=sid)
    if was_armed and current:
        return ("no-autocompact was already on in this session, and its keep-warm schedule "
                "(set up %s) is still current. Tell the user in one line that no-autocompact "
                "is on. Do nothing else." % when(st["schedule"]["requested_at"], now))
    return ("no-autocompact is now on for this session: the plugin's hooks veto idle compaction "
            "here.\nFinish arming it, then stop:\n"
            "1. " + create_steps(st["schedule"]["cron"], replace=replace) + "\n"
            "2. Tell the user in one line: no-autocompact is on; idle compaction is vetoed, and "
            "a keep-warm turn runs every 30 minutes while they're away (Claude Code ends the "
            "schedule after seven days; the veto stays).\nDo nothing else.")


def cmd_disarm(data, sid, now):
    with locked(data, sid):
        st = load(data, sid)
        if st is None:
            return ("no-autocompact wasn't on in this session. Tell the user so in one line. "
                    "Do nothing else.")
        was_armed = bool(st["armed"])
        st["armed"] = False
        st["disarmed_at"] = now if was_armed else st.get("disarmed_at")
        st["schedule"] = None
        st["orphan_cleanup_asked"] = True  # the steps below clean up any schedule
        save(data, sid, st)
    head = ("no-autocompact is now off for this session; idle compaction works as Claude Code "
            "intends again." if was_armed else "no-autocompact wasn't on in this session.")
    return (head + "\nFinish, then stop:\n"
            '1. Delete the keep-warm schedule: call CronList and delete, with CronDelete, every '
            'task whose prompt starts with "%s" (if you still have the ID from CronCreate, '
            "delete that directly).\n"
            "2. Tell the user in one line that no-autocompact is off.\nDo nothing else." % MARKER)


def cmd_status(data, sid, now):
    st = load(data, sid)
    if st is None:
        lines = ["no-autocompact has never been on in this session."]
    else:
        lines = []
        if st["armed"]:
            lines.append("Armed since %s." % when(st["armed_at"], now))
        else:
            lines.append("Off%s." % (" since " + when(st["disarmed_at"], now)
                                     if st.get("disarmed_at") else ""))
        sched = st.get("schedule")
        if sched and sched.get("stopped_at"):
            lines.append("Keep-warm schedule: stopped (noticed %s); /no-autocompact:on restarts "
                         "it. Idle compaction is still vetoed." % when(sched["stopped_at"], now))
        elif sched:
            lines.append("Keep-warm schedule: %s, set up %s; Claude Code ends it after seven "
                         "days, and the plugin then reminds you to restart it."
                         % (sched["cron"], when(sched.get("requested_at"), now)))
        t = st["ticks"]
        lines.append("Keep-warm ticks: %d passed (last %s), %d skipped (last %s%s)."
                     % (t["passed"], when(t["last_passed_at"], now), t["skipped"],
                        when(t["last_skipped_at"], now),
                        ": " + t["last_skip_reason"] if t.get("last_skip_reason") else ""))
        v = st["vetoes"]
        lines.append("Idle compactions vetoed: %d (last %s)." % (v["count"], when(v["last_at"], now)))
        lines.append("Last turn ended %s." % when(st.get("last_turn_end"), now))
    return "\n".join(lines) + "\n\nRelay this status to the user in a few short lines. Do nothing else."


# ---------------------------------------------------------------- hooks


def hook_prompt(data, inp, now):
    sid = inp["session_id"]
    prompt = inp.get("prompt") or ""
    if prompt.lstrip().startswith(MARKER):
        newest_reply = None
        path = inp.get("transcript_path")
        if path:
            try:
                newest_reply = tail_times(path)[2]
            except OSError:
                pass
        with locked(data, sid):
            st = load(data, sid) or new_state(sid)
            st["turn_open"] = False  # a scheduled prompt fires only between turns
            st["last_tick_seen_at"] = now
            sched = st.get("schedule")
            if (st["armed"] and sched and sched.get("stopped_at")
                    and now - (sched.get("requested_at") or 0) < SCHEDULE_RENEW):
                del sched["stopped_at"]  # a tick came after all, so the schedule still runs
            reason = tick_skip_reason(st, now, newest_reply)
            if reason is None:
                st["ticks"]["passed"] += 1
                st["ticks"]["last_passed_at"] = now
                st["turn_open"] = True
                st["last_prompt_at"] = now
                save(data, sid, st)
                return None  # no output: the tick goes on to Claude
            st["ticks"]["skipped"] += 1
            st["ticks"]["last_skipped_at"] = now
            st["ticks"]["last_skip_reason"] = reason
            if not st["armed"]:
                st["orphan_ticks"] = st.get("orphan_ticks", 0) + 1
            save(data, sid, st)
        return {"decision": "block",
                "reason": "no-autocompact: keep-warm skipped (%s)" % reason,
                "hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                       "suppressOriginalPrompt": True}}
    # Any other prompt: typed, a background subagent's report, a cross-session message.
    with locked(data, sid):
        st = load(data, sid)
        if st is None:
            return None
        notes = []
        if st["armed"]:
            sched = st.get("schedule")
            if sched is not None and not sched.get("stopped_at") and (
                    now - (sched.get("requested_at") or 0) >= SCHEDULE_RENEW
                    or schedule_lost(st, now)):
                sched["stopped_at"] = now
            stopped = sched is None or bool(sched.get("stopped_at"))
            if stopped and now - (st.get("last_lost_ask_at") or 0) > LOST_ASK_EVERY:
                st["last_lost_ask_at"] = now
                notes.append(stopped_note())
        elif st.get("orphan_ticks") and not st.get("orphan_cleanup_asked"):
            st["orphan_cleanup_asked"] = True
            notes.append(orphan_note())
        st["turn_open"] = True
        st["last_prompt_at"] = now
        save(data, sid, st)
    if notes:
        return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                       "additionalContext": "\n\n".join(notes)}}
    return None


def hook_turn_end(data, inp, now, ok):
    sid = inp["session_id"]
    with locked(data, sid):
        st = load(data, sid)
        if st is None:
            return None
        st["turn_open"] = False
        st["last_turn_end"] = now
        if ok:  # a failed request doesn't refresh the cache
            st["last_ok_turn_end"] = now
        save(data, sid, st)
    return None


def hook_precompact(data, inp, now):
    if inp.get("trigger") != "auto":
        return None
    sid = inp["session_id"]
    st = load(data, sid)
    if st is None or not st["armed"]:
        return None
    try:
        newest, newest_user, _ = tail_times(inp["transcript_path"])
    except (OSError, KeyError, TypeError):
        return None  # can't tell: let it go ahead (DESIGN.md §6.6)
    if newest is None or in_turn(st, now, newest, newest_user):
        return None
    with locked(data, sid):
        st = load(data, sid) or st
        st["vetoes"]["count"] += 1
        st["vetoes"]["last_at"] = now
        save(data, sid, st)
    return {"decision": "block", "reason": VETO_REASON}


HOOKS = {
    "prompt": hook_prompt,
    "stop": lambda data, inp, now: hook_turn_end(data, inp, now, ok=True),
    "stop-failure": lambda data, inp, now: hook_turn_end(data, inp, now, ok=False),
    "precompact": hook_precompact,
}


def run_hook(event, data, raw, now):
    """Return the JSON text to print, or an empty string. Never raises."""
    try:
        inp = json.loads(raw)
        sid = inp.get("session_id") if isinstance(inp, dict) else None
        if not isinstance(sid, str) or not SID_RE.match(sid) or event not in HOOKS:
            return ""
        out = HOOKS[event](data, inp, now)
        return json.dumps(out) if out else ""
    except Exception:  # fail open, but leave a trace
        try:
            Path(data).mkdir(parents=True, exist_ok=True)
            with open(str(Path(data) / "errors.log"), "a") as f:
                f.write("%s %s\n%s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), event,
                                         traceback.format_exc()))
        except OSError:
            pass
        return ""


def main(argv=None):
    ap = argparse.ArgumentParser(prog="nac.py", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("arm", "disarm", "status"):
        p = sub.add_parser(name)
        p.add_argument("--session", required=True)
        p.add_argument("--data", required=True)
    p = sub.add_parser("hook")
    p.add_argument("event", choices=sorted(HOOKS))
    p.add_argument("--data", required=True)
    args = ap.parse_args(argv)
    now = time.time()
    if args.cmd == "hook":
        out = run_hook(args.event, args.data, sys.stdin.read(), now)
        if out:
            print(out)
        return 0
    if not SID_RE.match(args.session):
        print("no-autocompact couldn't read this session's ID (%r). Tell the user so in one line."
              % args.session)
        return 0
    fn = {"arm": cmd_arm, "disarm": cmd_disarm, "status": cmd_status}[args.cmd]
    print(fn(args.data, args.session, now))
    return 0


if __name__ == "__main__":
    sys.exit(main())
