#!/usr/bin/env python3
"""Live probe: do prompt hooks see scheduled-task prompts, and can they cancel one?

The keep-warm design (DESIGN.md D2) wants a hook to cancel a scheduled keep-warm turn
at no model cost when it isn't needed. In Claude Code 2.1.325 a cron-fired prompt is
queued as a meta prompt (isMeta, skipSlashCommands), and whether the UserPromptSubmit
hooks run for such prompts couldn't be settled by reading the code. This probe runs a
throwaway interactive session and finds out.

What it does:
  1. Makes a fresh directory (default: build/probe-cron-<time>/ in this repo) holding a
     logging hook and a settings file that wires it to UserPromptSubmit and
     UserPromptExpansion. The hook blocks any prompt containing the marker BLOCKME
     (except the typed set-up prompt, which mentions CronCreate).
  2. Starts `claude` in a pseudo-terminal there (default model: haiku, to keep it cheap),
     accepts the folder-trust dialog, and asks it to schedule a once-a-minute task whose
     prompt carries the marker.
  3. Waits for the task to fire, then reports whether the hook saw the fired prompt and
     whether the model answered it anyway.

The session is killed at the end; its scheduled task dies with it. Cost: a handful of
small requests on the chosen model.

Usage: python3 -I tools/probe-cron-hooks.py [--model haiku] [--wait 240] [--dir DIR]
"""
import argparse
import fcntl
import json
import os
import pathlib
import pty
import re
import signal
import struct
import sys
import termios
import threading
import time

HOOK_SOURCE = r'''
import json, pathlib, sys, time
log, event = pathlib.Path(sys.argv[1]), sys.argv[2]
data = json.load(sys.stdin)
with log.open("a") as f:
    f.write(json.dumps({"at": time.time(), "event": event, "data": data}) + "\n")
prompt = data.get("prompt") or ""
if "BLOCKME" in prompt and "CronCreate" not in prompt:
    print(json.dumps({"decision": "block", "reason": "no-autocompact probe: prompt blocked by hook"}))
'''

ANSI = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[@-_]")


class PtySession:
    """A child process on a pseudo-terminal, with its output collected in the background."""

    def __init__(self, argv, cwd, env, raw_log, cols=160, rows=50):
        self.raw_log = open(raw_log, "wb")
        self.buf = bytearray()
        self.lock = threading.Lock()
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(cwd)
            os.execvpe(argv[0], argv, env)
        self.pid, self.fd = pid, fd
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        while True:
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                return
            if not data:
                return
            with self.lock:
                self.buf += data
            self.raw_log.write(data)
            self.raw_log.flush()

    def text(self):
        with self.lock:
            return ANSI.sub(b"", bytes(self.buf)).decode("utf-8", "replace")

    def send(self, data):
        os.write(self.fd, data.encode() if isinstance(data, str) else data)

    def wait_text(self, pattern, timeout):
        end = time.time() + timeout
        rx = re.compile(pattern, re.I)
        while time.time() < end:
            if rx.search(self.text()):
                return True
            time.sleep(0.5)
        return False

    def kill(self):
        for sig in (signal.SIGINT, signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(self.pid, sig)
            except ProcessLookupError:
                break
            time.sleep(1.5)
            try:
                if os.waitpid(self.pid, os.WNOHANG) != (0, 0):
                    break
            except ChildProcessError:
                break
        self.raw_log.close()


def read_jsonl(path):
    rows = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    except FileNotFoundError:
        pass
    return rows


def main():
    ap = argparse.ArgumentParser(description="Probe whether prompt hooks see scheduled-task prompts.")
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--wait", type=int, default=240, help="seconds to wait for the task to fire")
    ap.add_argument("--dir", help="probe directory (default: build/probe-cron-<time> in the repo)")
    args = ap.parse_args()

    repo = pathlib.Path(__file__).resolve().parent.parent
    probe = pathlib.Path(args.dir) if args.dir else repo / "build" / time.strftime("probe-cron-%Y%m%dT%H%M%S")
    probe.mkdir(parents=True, exist_ok=False)
    hook, log = probe / "hook.py", probe / "hooks.log"
    hook.write_text(HOOK_SOURCE)
    cmd = lambda event: f"python3 -I '{hook}' '{log}' {event}"
    settings = {
        "hooks": {ev: [{"hooks": [{"type": "command", "command": cmd(ev)}]}]
                  for ev in ("UserPromptSubmit", "UserPromptExpansion")},
        "permissions": {"allow": ["CronCreate", "CronDelete", "CronList"]},
    }
    (probe / "settings.json").write_text(json.dumps(settings, indent=2))

    # Drop only the variables that tie a process to the session running this script; they
    # would mark the probe as a nested session. Keep the user's preference variables
    # (CLAUDE_CODE_NO_FLICKER and the like): without them Claude Code may offer first-run
    # dialogs, and an answer to one is saved to the user's settings. (Lesson of the first
    # run, 2026-11-15: a stray Enter accepted the fullscreen-renderer offer, which wrote
    # "tui": "fullscreen" to ~/.claude/settings.json and had to be removed by hand.)
    session_vars = {"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_CODE_SESSION_ID",
                    "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_BRIDGE_SESSION_ID",
                    "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_SESSION_ATTENDED",
                    "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_SESSION_KIND"}
    env = {k: v for k, v in os.environ.items() if k not in session_vars}
    env["TERM"] = "xterm-256color"
    argv = ["claude", "--model", args.model, "--settings", str(probe / "settings.json"),
            "-n", "no-autocompact-probe"]
    print(f"probe dir: {probe}")
    s = PtySession(argv, str(probe), env, probe / "screen.raw")
    try:
        # Wait for the input box. The folder-trust dialog (shown only for a folder not yet
        # trusted) is the one dialog the probe confirms; any other is cancelled with Esc,
        # which leaves the user's settings unchanged.
        mark, trusted = 0, False
        end = time.time() + 60
        while time.time() < end:
            screen = s.text()[mark:]
            if not trusted and re.search(r"trust this folder", screen, re.I):
                time.sleep(1)
                # The cursor starts on "No, exit"; move to "Yes, I trust this folder".
                if re.search(r"❯\s*No", s.text()[mark:]):
                    s.send("\x1b[B")
                    time.sleep(0.5)
                s.send("\r")
                trusted = True
                print("accepted the folder-trust dialog")
                time.sleep(2)
                mark = len(s.text())
                continue
            if "Enter to confirm" in screen:
                print("an unexpected dialog appeared; cancelling it with Esc")
                s.send("\x1b")
                time.sleep(2)
                mark = len(s.text())
                continue
            if re.search(r"shift\+tab to cycle|for shortcuts", screen):
                time.sleep(2)
                if "Enter to confirm" not in s.text()[mark:]:
                    break
                continue
            time.sleep(1)
        else:
            print("the input box never appeared; giving up")
            return 1
        time.sleep(2)
        s.send("Use the CronCreate tool to schedule a recurring task with the cron expression "
               "'*/1 * * * *' and this exact prompt: BLOCKME probe tick, reply with only the word ok. "
               "Then reply with only the task id.")
        time.sleep(1)
        s.send("\r")
        t0 = time.time()
        transcript = None
        while time.time() - t0 < 60 and transcript is None:
            for rec in read_jsonl(log):
                transcript = rec["data"].get("transcript_path") or transcript
            time.sleep(1)
        print(f"set-up prompt reached the hook: {transcript is not None}; transcript: {transcript}")
        created = None
        while time.time() - t0 < 120 and created is None:
            for row in read_jsonl(transcript or "/nonexistent"):
                msg = row.get("message") or {}
                for block in msg.get("content") or [] if isinstance(msg.get("content"), list) else []:
                    if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "CronCreate":
                        created = row.get("timestamp")
            time.sleep(1)
        print(f"CronCreate called: {created}")
        fired = None
        t1 = time.time()
        while time.time() - t1 < args.wait and fired is None:
            for rec in read_jsonl(log):
                p = rec["data"].get("prompt") or ""
                if "BLOCKME" in p and "CronCreate" not in p:
                    fired = rec
                    break
            time.sleep(2)
        time.sleep(20)  # give an unblocked prompt time to draw an answer
        rows = read_jsonl(transcript or "/nonexistent")
        tick_rows = [r for r in rows if "BLOCKME probe tick" in json.dumps(r) and "CronCreate" not in json.dumps(r)]
        answered = False
        if tick_rows:
            last_tick = max(r.get("timestamp", "") for r in tick_rows)
            answered = any(r.get("type") == "assistant" and r.get("timestamp", "") > last_tick for r in rows)
        result = {
            "hook_saw_fired_prompt": fired is not None,
            "hook_event": fired and fired["event"],
            "hook_input_keys": fired and sorted(fired["data"].keys()),
            "fired_prompt_as_seen_by_hook": fired and fired["data"].get("prompt"),
            "transcript_rows_mentioning_tick": len(tick_rows),
            "tick_row_types": sorted({(r.get("type"), r.get("subtype"), bool(r.get("isMeta"))) for r in tick_rows}, key=str),
            "model_answered_after_tick": answered,
        }
        print(json.dumps(result, indent=2))
        (probe / "result.json").write_text(json.dumps(result, indent=2))
    finally:
        s.kill()
        print("probe session stopped")


if __name__ == "__main__":
    sys.exit(main())
