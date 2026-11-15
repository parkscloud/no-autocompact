"""Unit tests for scripts/nac.py and scripts/hook.sh (DESIGN.md §6, §7.1 stage 2).

Run from the repo root:  python3 -I -m unittest discover -s tests -v
Standard library only. Time is passed in explicitly, so no test waits.
"""
import datetime
import importlib.util
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("nac", str(ROOT / "scripts" / "nac.py"))
nac = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nac)

SID = "11111111-2222-3333-4444-555555555555"
MIN = 60.0
T0 = 1_800_000_000.0  # an arbitrary moment


def iso(ts):
    return (datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


def user_row(ts, text="hello"):
    return {"type": "user", "timestamp": iso(ts), "message": {"role": "user", "content": text}}


def reply_row(ts, error=False):
    row = {"type": "assistant", "timestamp": iso(ts),
           "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}}
    if error:
        row["isApiErrorMessage"] = True
    else:
        row["message"]["usage"] = {"input_tokens": 2, "cache_read_input_tokens": 400000}
    return row


def system_row(ts, subtype):
    return {"type": "system", "subtype": subtype, "timestamp": iso(ts), "content": "x"}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.data = str(self.dir / "data")
        self.transcript = self.dir / "transcript.jsonl"
        self.transcript.write_text("")

    def tearDown(self):
        self._tmp.cleanup()

    def rows(self, *rows):
        self.transcript.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def hook(self, event, now, **fields):
        inp = {"session_id": SID, "transcript_path": str(self.transcript), "cwd": str(self.dir)}
        inp.update(fields)
        out = nac.run_hook(event, self.data, json.dumps(inp), now)
        return json.loads(out) if out else None

    def tick(self, now):
        return self.hook("prompt", now, prompt=nac.TICK_PROMPT)

    def typed(self, now, text="carry on"):
        return self.hook("prompt", now, prompt=text)

    def stop(self, now):
        return self.hook("stop", now)

    def precompact(self, now, trigger="auto"):
        return self.hook("precompact", now, trigger=trigger, custom_instructions=None)

    def state(self):
        return nac.load(self.data, SID)

    def arm_and_settle(self, now=T0):
        """Arm, then end the arming turn the way the Stop hook would."""
        nac.cmd_arm(self.data, SID, now)
        self.stop(now + 5)


class CronTests(Base):
    def test_two_minutes_half_an_hour_apart_avoiding_the_hour_and_half_hour(self):
        for i in range(500):
            first, second = nac.cron_for("session-%d" % i).split(" ")[0].split(",")
            m = int(first)
            self.assertTrue(1 <= m <= 29, m)
            self.assertEqual(int(second), m + 30)

    def test_stable_for_a_session(self):
        self.assertEqual(nac.cron_for(SID), nac.cron_for(SID))
        self.assertTrue(nac.cron_for(SID).endswith(" * * * *"))


class ArmTests(Base):
    def test_first_arming_asks_for_one_cron_create(self):
        out = nac.cmd_arm(self.data, SID, T0)
        self.assertIn("CronCreate", out)
        self.assertIn(nac.TICK_PROMPT, out)
        self.assertIn(nac.cron_for(SID), out)
        self.assertNotIn("CronList", out)
        st = self.state()
        self.assertTrue(st["armed"])
        self.assertEqual(st["armed_at"], T0)
        self.assertEqual(st["schedule"]["cron"], nac.cron_for(SID))
        self.assertTrue(st["turn_open"])

    def test_arming_again_with_a_current_schedule_asks_for_nothing(self):
        nac.cmd_arm(self.data, SID, T0)
        out = nac.cmd_arm(self.data, SID, T0 + 3600)
        self.assertIn("already on", out)
        self.assertNotIn("CronCreate", out)
        self.assertEqual(self.state()["armed_at"], T0)

    def test_arming_again_after_seven_days_replaces_the_schedule(self):
        nac.cmd_arm(self.data, SID, T0)
        out = nac.cmd_arm(self.data, SID, T0 + 7 * 86400)
        self.assertIn("CronList", out)
        self.assertIn("CronCreate", out)

    def test_arming_after_a_leftover_schedule_fired_clears_it_first(self):
        self.tick(T0)  # a tick in a session that was never armed
        out = nac.cmd_arm(self.data, SID, T0 + 60)
        self.assertIn("CronList", out)
        self.assertEqual(self.state()["orphan_ticks"], 0)

    def test_prunes_month_old_sessions_but_keeps_this_one(self):
        old = Path(self.data, "sessions", "old-session.json")
        old.parent.mkdir(parents=True)
        old.write_text("{}")
        long_ago = time.time() - 40 * 86400
        os.utime(str(old), (long_ago, long_ago))
        nac.cmd_arm(self.data, SID, time.time())
        self.assertFalse(old.exists())
        self.assertTrue(nac.state_path(self.data, SID).exists())


class TickTests(Base):
    def test_skipped_while_the_user_is_active(self):
        self.arm_and_settle()
        out = self.tick(T0 + 10 * MIN)
        self.assertEqual(out["decision"], "block")
        self.assertIn(nac.SKIP_ACTIVE, out["reason"])
        self.assertTrue(out["hookSpecificOutput"]["suppressOriginalPrompt"])
        self.assertEqual(self.state()["ticks"]["skipped"], 1)

    def test_passes_between_20_and_53_minutes_after_the_last_turn(self):
        self.arm_and_settle()
        self.assertIsNone(self.tick(T0 + 25 * MIN))
        st = self.state()
        self.assertEqual(st["ticks"]["passed"], 1)
        self.assertTrue(st["turn_open"])

    def test_skipped_once_the_cache_has_likely_expired(self):
        self.arm_and_settle()
        out = self.tick(T0 + 55 * MIN)
        self.assertIn(nac.SKIP_COLD, out["reason"])

    def test_ticks_chain_every_half_hour_while_the_user_is_away(self):
        self.arm_and_settle()
        t = T0 + 22 * MIN
        for _ in range(6):  # three hours away
            self.assertIsNone(self.tick(t))
            self.stop(t + 4)  # the short tick turn ends
            t += 30 * MIN
        self.assertEqual(self.state()["ticks"]["passed"], 6)

    def test_a_successful_reply_in_the_transcript_counts_as_activity(self):
        self.arm_and_settle()
        self.rows(reply_row(T0 + 30 * MIN))  # a turn whose Stop hook never ran
        out = self.tick(T0 + 40 * MIN)
        self.assertIn(nac.SKIP_ACTIVE, out["reason"])

    def test_an_api_error_in_the_transcript_does_not(self):
        self.arm_and_settle()
        self.rows(reply_row(T0 + 30 * MIN, error=True))
        self.assertIsNone(self.tick(T0 + 40 * MIN))

    def test_a_failed_turn_does_not_reset_the_cache_clock(self):
        self.arm_and_settle()
        self.assertIsNone(self.tick(T0 + 25 * MIN))
        self.hook("stop-failure", T0 + 25.5 * MIN, error="rate_limit")
        st = self.state()
        self.assertFalse(st["turn_open"])
        self.assertEqual(st["last_ok_turn_end"], T0 + 5)
        out = self.tick(T0 + 55 * MIN)  # 55 minutes after the last good turn
        self.assertIn(nac.SKIP_COLD, out["reason"])

    def test_a_tick_closes_a_turn_left_open_by_an_interrupt(self):
        self.arm_and_settle()
        self.typed(T0 + 10 * MIN)  # opens a turn; Esc means no Stop hook
        self.assertTrue(self.state()["turn_open"])
        self.tick(T0 + 12 * MIN)
        self.assertFalse(self.state()["turn_open"])

    def test_a_tick_in_a_session_that_is_not_armed_is_skipped_and_noted(self):
        out = self.tick(T0)
        self.assertIn(nac.SKIP_UNARMED, out["reason"])
        self.assertEqual(self.state()["orphan_ticks"], 1)

    def test_a_disarmed_session_skips_its_ticks(self):
        self.arm_and_settle()
        nac.cmd_disarm(self.data, SID, T0 + MIN)
        out = self.tick(T0 + 25 * MIN)
        self.assertIn(nac.SKIP_UNARMED, out["reason"])


class PromptTests(Base):
    def test_a_typed_prompt_opens_a_turn_and_adds_nothing(self):
        self.arm_and_settle()
        self.assertIsNone(self.typed(T0 + MIN))
        self.assertTrue(self.state()["turn_open"])

    def test_a_typed_prompt_in_a_never_armed_session_leaves_no_state(self):
        self.assertIsNone(self.typed(T0))
        self.assertIsNone(self.state())

    def test_asks_for_a_new_schedule_after_seven_days(self):
        self.arm_and_settle()
        out = self.typed(T0 + 7 * 86400)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("CronCreate", ctx)
        self.assertIn("CronList", ctx)
        self.assertIsNone(self.typed(T0 + 7 * 86400 + MIN))  # asked once

    def test_notices_a_schedule_that_never_ticked(self):
        self.arm_and_settle()
        out = self.typed(T0 + 50 * MIN)  # idle 50 minutes, no tick seen
        self.assertIn("CronCreate", out["hookSpecificOutput"]["additionalContext"])

    def test_waits_for_the_first_tick_before_calling_it_lost(self):
        self.arm_and_settle()
        self.assertIsNone(self.typed(T0 + 40 * MIN))  # the first tick can take 45 minutes

    def test_not_lost_when_a_tick_came_during_the_idle_stretch(self):
        self.arm_and_settle()
        self.tick(T0 + 10 * MIN)  # skipped, but seen
        self.stop(T0 + 11 * MIN)
        self.tick(T0 + 40 * MIN)  # passes
        self.stop(T0 + 41 * MIN)
        self.tick(T0 + 70 * MIN)  # passes
        self.assertIsNone(self.typed(T0 + 75 * MIN))

    def test_asks_about_a_lost_schedule_at_most_once_a_day(self):
        self.arm_and_settle()
        self.assertIsNotNone(self.typed(T0 + 50 * MIN))
        self.stop(T0 + 51 * MIN)
        self.assertIsNone(self.typed(T0 + 120 * MIN))
        self.stop(T0 + 121 * MIN)
        self.assertIsNotNone(self.typed(T0 + 26 * 3600))

    def test_asks_once_to_delete_a_leftover_schedule(self):
        self.tick(T0)
        out = self.typed(T0 + MIN)
        self.assertIn("CronDelete", out["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.typed(T0 + 2 * MIN))


class TurnEndTests(Base):
    def test_stop_closes_the_turn_and_marks_a_good_end(self):
        self.arm_and_settle()
        self.typed(T0 + MIN)
        self.stop(T0 + 2 * MIN)
        st = self.state()
        self.assertFalse(st["turn_open"])
        self.assertEqual(st["last_ok_turn_end"], T0 + 2 * MIN)
        self.assertEqual(st["last_turn_end"], T0 + 2 * MIN)

    def test_stop_in_a_never_armed_session_does_nothing(self):
        self.assertIsNone(self.stop(T0))
        self.assertIsNone(self.state())


class GuardTests(Base):
    def test_vetoes_an_idle_run(self):
        self.arm_and_settle()
        self.rows(user_row(T0), reply_row(T0 + 4), system_row(T0 + 5, "turn_duration"))
        out = self.precompact(T0 + 54 * MIN)
        self.assertEqual(out["decision"], "block")
        self.assertEqual(out["reason"], nac.VETO_REASON)
        self.assertEqual(self.state()["vetoes"]["count"], 1)

    def test_allows_a_run_inside_a_turn(self):
        self.arm_and_settle()
        self.typed(T0 + 60 * MIN)
        self.rows(user_row(T0 + 60 * MIN), reply_row(T0 + 61 * MIN))
        self.assertIsNone(self.precompact(T0 + 61.5 * MIN))

    def test_allows_a_run_right_after_fresh_input_even_without_an_open_turn(self):
        self.arm_and_settle()
        self.rows(reply_row(T0 + 4), user_row(T0 + 60 * MIN, "a goal check-in, say"))
        self.assertIsNone(self.precompact(T0 + 60 * MIN + 30))

    def test_vetoes_when_the_open_turn_is_stale(self):
        self.arm_and_settle()
        self.typed(T0 + MIN)  # interrupted: no Stop hook
        self.rows(user_row(T0 + MIN), reply_row(T0 + 2 * MIN))
        self.assertIsNotNone(self.precompact(T0 + 56 * MIN))

    def test_fresh_rows_from_a_skipped_tick_do_not_count_as_a_turn(self):
        self.arm_and_settle()
        self.rows(reply_row(T0 + 4), system_row(T0 + 53 * MIN, "scheduled_task_fire"),
                  system_row(T0 + 53 * MIN, "informational"))
        self.assertIsNotNone(self.precompact(T0 + 54 * MIN))

    def test_never_touches_manual_compaction(self):
        self.arm_and_settle()
        self.rows(reply_row(T0 + 4))
        self.assertIsNone(self.precompact(T0 + 54 * MIN, trigger="manual"))

    def test_leaves_sessions_that_are_not_armed_alone(self):
        self.rows(reply_row(T0))
        self.assertIsNone(self.precompact(T0 + 54 * MIN))
        nac.cmd_arm(self.data, SID, T0)
        nac.cmd_disarm(self.data, SID, T0 + 1)
        self.assertIsNone(self.precompact(T0 + 54 * MIN))

    def test_lets_compaction_go_ahead_when_it_cannot_tell(self):
        self.arm_and_settle()
        self.transcript.unlink()
        self.assertIsNone(self.precompact(T0 + 54 * MIN))
        self.transcript.write_text("not json\n{\"type\": \"user\"}\n")
        self.assertIsNone(self.precompact(T0 + 54 * MIN))


class TranscriptTests(Base):
    def test_reads_only_the_tail_of_a_large_transcript(self):
        filler = json.dumps({"type": "user", "timestamp": iso(T0), "pad": "x" * 2000}) + "\n"
        with open(str(self.transcript), "w") as f:
            for _ in range(400):  # about 800 KB
                f.write(filler)
            f.write(json.dumps(reply_row(T0 + 60)) + "\n")
        newest, newest_user, newest_reply = nac.tail_times(str(self.transcript))
        self.assertEqual(newest, T0 + 60)
        self.assertEqual(newest_user, T0)
        self.assertEqual(newest_reply, T0 + 60)

    def test_parses_timestamps(self):
        self.assertEqual(nac.parse_ts("2027-01-15T08:00:00.000Z"),
                         datetime.datetime(2027, 1, 15, 8, tzinfo=datetime.timezone.utc).timestamp())
        self.assertEqual(nac.parse_ts("2027-01-15T03:00:00-05:00"), nac.parse_ts("2027-01-15T08:00:00Z"))
        self.assertIsNone(nac.parse_ts("yesterday"))
        self.assertIsNone(nac.parse_ts(None))


class CommandTests(Base):
    def test_disarm_asks_claude_to_delete_the_schedule(self):
        self.arm_and_settle()
        out = nac.cmd_disarm(self.data, SID, T0 + MIN)
        self.assertIn("now off", out)
        self.assertIn("CronDelete", out)
        st = self.state()
        self.assertFalse(st["armed"])
        self.assertIsNone(st["schedule"])

    def test_disarm_in_a_never_armed_session(self):
        self.assertIn("wasn't on", nac.cmd_disarm(self.data, SID, T0))

    def test_status_reports_what_happened(self):
        self.arm_and_settle()
        self.tick(T0 + 25 * MIN)
        self.stop(T0 + 26 * MIN)
        self.tick(T0 + 30 * MIN)
        self.rows(reply_row(T0 + 26 * MIN))
        self.precompact(T0 + 90 * MIN)
        out = nac.cmd_status(self.data, SID, T0 + 91 * MIN)
        self.assertIn("Armed since", out)
        self.assertIn("1 passed", out)
        self.assertIn("1 skipped", out)
        self.assertIn("vetoed: 1", out)
        self.assertIn("Relay this status", out)

    def test_status_in_a_never_armed_session(self):
        self.assertIn("never been on", nac.cmd_status(self.data, SID, T0))


class RobustnessTests(Base):
    def test_bad_input_is_ignored(self):
        self.assertEqual(nac.run_hook("prompt", self.data, "not json", T0), "")
        bad = json.dumps({"session_id": "../../etc/passwd", "prompt": nac.TICK_PROMPT})
        self.assertEqual(nac.run_hook("prompt", self.data, bad, T0), "")
        self.assertEqual(nac.run_hook("no-such-event", self.data, json.dumps({"session_id": SID}), T0), "")

    def test_an_internal_error_fails_open_and_is_logged(self):
        nac.cmd_arm(self.data, SID, T0)
        st_file = nac.state_path(self.data, SID)
        st_file.write_text(json.dumps({"armed": True, "ticks": "not a dict"}))
        out = nac.run_hook("prompt", self.data, json.dumps({"session_id": SID, "prompt": nac.TICK_PROMPT}), T0)
        self.assertEqual(out, "")
        self.assertTrue(Path(self.data, "errors.log").exists())


class GateTests(Base):
    """scripts/hook.sh, run under bash with a stand-in python3 that records being started."""

    def setUp(self):
        super().setUp()
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self.started = self.dir / "python-started"
        fake = self.bin / "python3"
        fake.write_text("#!/bin/sh\necho started >> '%s'\ncat > /dev/null\n" % self.started)
        fake.chmod(0o755)

    def gate(self, event, payload, fake_python=True):
        env = dict(os.environ, CLAUDE_PLUGIN_DATA=self.data)
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        if fake_python:
            env["PATH"] = "%s:%s" % (self.bin, env.get("PATH", ""))
        return subprocess.run(["bash", str(ROOT / "scripts" / "hook.sh"), event],
                              input=json.dumps(payload), capture_output=True, text=True, env=env,
                              timeout=30)

    def test_a_never_armed_session_never_starts_python(self):
        for event, payload in (("prompt", {"session_id": SID, "prompt": "hi"}),
                               ("stop", {"session_id": SID}),
                               ("precompact", {"session_id": SID, "trigger": "auto"})):
            r = self.gate(event, payload)
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stdout, "")
        self.assertFalse(self.started.exists())

    def test_a_tick_always_reaches_python(self):
        self.gate("prompt", {"session_id": SID, "prompt": nac.TICK_PROMPT})
        self.assertTrue(self.started.exists())

    def test_an_armed_session_reaches_python(self):
        nac.cmd_arm(self.data, SID, T0)
        self.gate("stop", {"session_id": SID})
        self.assertTrue(self.started.exists())

    def test_end_to_end_with_the_real_python(self):
        nac.cmd_arm(self.data, SID, time.time())
        r = self.gate("prompt", {"session_id": SID, "prompt": nac.TICK_PROMPT,
                                 "transcript_path": str(self.transcript)}, fake_python=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["decision"], "block")
        self.assertIn(nac.SKIP_ACTIVE, out["reason"])


if __name__ == "__main__":
    unittest.main()
