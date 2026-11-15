"""Drive a throwaway interactive Claude Code session on a pseudo-terminal, for live checks.

Used by the scripts in tools/. A probe session shares the user's real ~/.claude and
~/.claude.json, so it must not change them: start() confirms only the folder-trust dialog
and cancels any other dialog with Esc, type_line() refuses to type into a dialog that opens
later, and clean_env() keeps the user's preference variables (CLAUDE_CODE_NO_FLICKER and
the like). Callers should still copy ~/.claude/settings.json before a run and compare it
afterwards (see AGENTS.md).

Start probes with a full model ID such as claude-haiku-5-5, never the alias `haiku`: in
Claude Code 2.1.325 a session whose model is the alias `haiku` or `opusplan` never runs idle
compaction (its timer gives up as `prefix_changed`; anthropics/claude-code#103243).
"""
import fcntl
import json
import os
import pty
import re
import signal
import struct
import termios
import threading
import time
from pathlib import Path

ANSI = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[@-_]")

# Variables that tie a process to the session running the probe; inherited, they would mark
# the probe as a nested session. Everything else in the environment is kept.
SESSION_VARS = {"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_CODE_SESSION_ID",
                "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_BRIDGE_SESSION_ID",
                "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_SESSION_ATTENDED",
                "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_SESSION_KIND"}


# Set for every probe unless the caller overrides it. Once a conversation passes a size the
# server picks, Claude Code 2.1.325 may open "We recommend compacting earlier", which offers
# to autocompact at 400K and saves the answer to the user's settings for that model. It isn't
# offered when the window comes from this variable; 1M is Haiku 5.5's own window, so the
# session compacts where it would anyway.
QUIET_ENV = {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1000000"}

# Footers of dialogs that cover the input box, and of the input box itself. Whichever was
# drawn last tells whether a dialog is open now.
DIALOG_HINT = re.compile(r"[Ee]nter (to )?confirm|[Ee]sc not now|[Ee]sc to cancel|↑/↓ choose")
INPUT_HINT = re.compile(r"shift\+tab to cycle|for shortcuts|esc to interrupt")


class DialogOpen(RuntimeError):
    """A dialog covers the input box, so typed text would go to it instead."""


def clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in SESSION_VARS}
    env["TERM"] = "xterm-256color"
    env.update(QUIET_ENV)
    env.update(extra)
    return env


class PtySession:
    """A child process on a pseudo-terminal, with its output collected in the background."""

    def __init__(self, argv, cwd, env, raw_log, cols=160, rows=50):
        self.raw_log = open(str(raw_log), "wb")
        self.buf = bytearray()
        self.lock = threading.Lock()
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(str(cwd))
            os.execvpe(argv[0], argv, env)
        self.pid, self.fd = pid, fd
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        threading.Thread(target=self._read, daemon=True).start()

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

    def dialog_open(self, tail=6000):
        """True when a dialog's footer was drawn after the input box's last footer."""
        screen = self.text()[-tail:]
        dialog = [m.end() for m in DIALOG_HINT.finditer(screen)]
        box = [m.end() for m in INPUT_HINT.finditer(screen)]
        return bool(dialog) and (not box or dialog[-1] > box[-1])

    def _refuse_if_dialog(self, text):
        if self.dialog_open():
            raise DialogOpen("a dialog is open, so %r wasn't sent; the screen ends:\n%s"
                             % (text[:60], self.text()[-1500:]))

    def type_line(self, text):
        """Type a prompt and press Enter, unless a dialog would catch the keys.

        Text typed into a dialog is lost, and an Enter there could confirm a choice that
        changes the user's settings, so this raises DialogOpen instead.
        """
        self._refuse_if_dialog(text)
        self.send(text)
        time.sleep(1.0)
        self._refuse_if_dialog(text)  # one may have opened while typing: no Enter then
        self.send("\r")

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


def start(argv, cwd, raw_log, env=None, timeout=60):
    """Launch Claude Code and wait for its input box. Returns the session, or None."""
    s = PtySession(argv, cwd, env or clean_env(), raw_log)
    mark, trusted = 0, False
    end = time.time() + timeout
    while time.time() < end:
        screen = s.text()[mark:]
        if not trusted and re.search(r"trust this folder", screen, re.I):
            time.sleep(1)
            if re.search(r"❯\s*No", s.text()[mark:]):  # the cursor starts on "No, exit"
                s.send("\x1b[B")
                time.sleep(0.5)
            s.send("\r")
            trusted = True
            time.sleep(2)
            mark = len(s.text())
            continue
        if "Enter to confirm" in screen:
            s.send("\x1b")  # cancel any other dialog, leaving the user's settings alone
            time.sleep(2)
            mark = len(s.text())
            continue
        if re.search(r"shift\+tab to cycle|for shortcuts", screen):
            time.sleep(2)
            if "Enter to confirm" not in s.text()[mark:]:
                return s
            continue
        time.sleep(1)
    s.kill()
    return None


def project_slug(path):
    return re.sub(r"[^A-Za-z0-9-]", "-", str(Path(path).resolve()))


def newest_transcript(cwd, since):
    d = Path.home() / ".claude" / "projects" / project_slug(cwd)
    found = [p for p in d.glob("*.jsonl") if p.stat().st_mtime >= since] if d.is_dir() else []
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def read_jsonl(path):
    rows = []
    try:
        with open(str(path)) as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    except (FileNotFoundError, TypeError):
        pass
    return rows


def tool_uses(rows, name=None):
    """(timestamp, name, input) for each tool call in the transcript rows."""
    out = []
    for r in rows:
        if r.get("type") != "assistant":
            continue
        content = (r.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                if name is None or block.get("name") == name:
                    out.append((r.get("timestamp"), block.get("name"), block.get("input") or {}))
    return out


def wait_for(predicate, timeout, every=1.0):
    end = time.time() + timeout
    while time.time() < end:
        value = predicate()
        if value:
            return value
        time.sleep(every)
    return None
