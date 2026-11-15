#!/usr/bin/env python3
"""Live checks 2 and 3 of DESIGN.md §7.2: the idle veto, and keep-warm over two hours.

Three throwaway sessions on a cheap model, each loaded with the plugin (--plugin-dir), started
with CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS=100000, and filled with about 220K tokens by reading
generated files that each hide a code word:
  A  armed, then its keep-warm task deleted: the guard alone must veto idle compaction
  B  not armed: the control, which idle compaction should compact at about 54 minutes
  C  armed with keep-warm: ticks should pass about every 30 minutes, and nothing compacts
Nothing is typed while they sit idle (a keypress within 60 seconds also stops idle
compaction). After 65 minutes A and B are checked and A is asked for a code word; after
--wait minutes (default 125) C is checked and asked too. The machine must stay awake
throughout: idle compaction gives up when its timer fires late.

The model must be a full ID: with the alias `haiku`, idle compaction never runs, so the
control can't compact (see tools/ptysession.py). The run stops at once when a prompt doesn't
reach the transcript, a dialog covers the input box, or arming leaves no state; the first run
(2026-11-15) went on for an hour after all three of its sessions sat behind a dialog.

Usage: python3 -I tools/live-idle-check.py [--model claude-haiku-5-5] [--wait 125]
Writes build/live-idle-<time>/result.json, updated as it goes.
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

WORDS = ("amber basil cobalt dune ember fjord garnet harbor indigo juniper kestrel lantern "
         "meadow nickel orchid pewter quartz russet saffron thistle umber velvet willow yarrow").split()


def make_files(d, rng, count=8, lines=700):
    """Files of varied filler text, each hiding one code word on a known line.

    700 lines (about 24K tokens) fit in one Read call; at 1,000 the model reread each file
    to get past Read's limit, which made the three sessions' sizes differ.
    """
    secrets = {}
    for i in range(1, count + 1):
        word = rng.choice(WORDS) + "-" + str(rng.randint(100, 999))
        secret_line = rng.randint(200, lines - 200)
        out = []
        for n in range(1, lines + 1):
            if n == secret_line:
                out.append("Note %d: the code word for file %d is %s." % (n, i, word))
            else:
                out.append("Line %d of file %d: %s." % (n, i, " ".join(rng.choice(WORDS) for _ in range(6))))
        (d / ("file%d.txt" % i)).write_text("\n".join(out) + "\n")
        secrets[i] = word
    return secrets


def context_tokens(rows):
    for r in reversed(rows):
        u = (r.get("message") or {}).get("usage") if r.get("type") == "assistant" else None
        if u:
            return (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + \
                (u.get("cache_creation_input_tokens") or 0)
    return 0


def state_of(sid):
    for p in (Path.home() / ".claude" / "plugins" / "data").glob("*/sessions/%s.json" % sid):
        try:
            return json.loads(p.read_text())
        except ValueError:
            pass
    return None


class Probe:
    def __init__(self, name, root, model, rng):
        self.name, self.dir = name, root / name
        self.dir.mkdir(parents=True)
        self.secrets = make_files(self.dir, rng)
        self.t0 = time.time()
        env = ps.clean_env(CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS="100000")
        argv = ["claude", "--model", model, "--plugin-dir", str(REPO), "-n", "no-autocompact-live-" + name]
        self.s = ps.start(argv, self.dir, self.dir / "screen.raw", env=env)
        if self.s is None:
            raise RuntimeError("%s: the input box never appeared" % name)
        self.transcript = None

    def rows(self):
        return ps.read_jsonl(self.transcript) if self.transcript else []

    def replies(self):
        return sum(1 for r in self.rows() if r.get("type") == "assistant"
                   and '"text"' in json.dumps((r.get("message") or {}).get("content")))

    def sent(self, key):
        return sum(1 for r in self.rows() if r.get("type") == "user"
                   and key in json.dumps((r.get("message") or {}).get("content"), ensure_ascii=False))

    def ask(self, text, timeout=300):
        """Type a prompt and wait for the turn to settle: a new reply, then 20 quiet seconds.

        Raises when the prompt never reaches the transcript or gets no reply, so a run can't
        carry on as though a step had worked.
        """
        key = text.lstrip("/")[:40]
        before, sent_before = self.replies(), self.sent(key)
        self.s.type_line(text)
        if self.transcript is None:
            self.transcript = ps.wait_for(lambda: ps.newest_transcript(self.dir, self.t0), 60)
        if not ps.wait_for(lambda: self.sent(key) > sent_before, 60, every=2):
            raise RuntimeError("%s: %r never reached the transcript; the screen ends:\n%s"
                               % (self.name, text[:60], self.s.text()[-1500:]))
        if not ps.wait_for(lambda: self.replies() > before, timeout, every=2):
            raise RuntimeError("%s: no reply to %r within %d s" % (self.name, text[:60], timeout))
        last = None
        for _ in range(int(timeout / 5)):
            n = len(self.rows())
            if n == last:
                break
            last = n
            time.sleep(20)

    @property
    def sid(self):
        return self.transcript.stem if self.transcript else None

    def compactions(self):
        return [r.get("timestamp") for r in self.rows() if r.get("subtype") == "compact_boundary"]


def main():
    ap = argparse.ArgumentParser(description="Live idle-veto and keep-warm checks.")
    ap.add_argument("--model", default="claude-haiku-5-5")
    ap.add_argument("--wait", type=float, default=125, help="minutes to watch session C")
    args = ap.parse_args()
    root = REPO / "build" / time.strftime("live-idle-%Y%m%dT%H%M%S")
    root.mkdir(parents=True)
    settings = Path.home() / ".claude" / "settings.json"
    settings_before = settings.read_bytes() if settings.exists() else None
    rng = random.Random(int(time.time()))
    res = {"root": str(root), "started": time.strftime("%Y-%m-%d %H:%M:%S"), "events": []}

    def note(msg):
        res["events"].append("%s %s" % (time.strftime("%H:%M:%S"), msg))
        (root / "result.json").write_text(json.dumps(res, indent=2))
        print(res["events"][-1], flush=True)

    probes = {}
    try:
        for name in "ABC":
            probes[name] = p = Probe(name, root, args.model, rng)
            p.ask("Read each of the files file1.txt to file8.txt in this folder in full with the "
                  "Read tool (one call per file, no offset or limit), then reply with only: done",
                  timeout=600)
            tokens = context_tokens(p.rows())
            note("%s: filled, context %d tokens, session %s" % (name, tokens, p.sid))
            if tokens < 120000:
                raise RuntimeError("%s holds %d tokens, too few for idle compaction's 100K minimum"
                                   % (name, tokens))
        for name in "AC":
            probes[name].ask("/no-autocompact:on")
            armed = (state_of(probes[name].sid) or {}).get("armed")
            note("%s: armed (state armed=%s)" % (name, armed))
            if armed is not True:
                raise RuntimeError("%s: /no-autocompact:on left no armed state" % name)
        probes["A"].ask("Call CronList, delete every task whose prompt starts with "
                        "'[no-autocompact keep-warm]' with CronDelete, then reply with only: deleted")
        deleted = len(ps.tool_uses(probes["A"].rows(), "CronDelete"))
        note("A: keep-warm deleted (CronDelete calls: %d)" % deleted)
        if deleted < 1:
            raise RuntimeError("A: the keep-warm task wasn't deleted")
        idle_from = time.time()
        res["idle_from"] = time.strftime("%H:%M:%S")
        note("all idle; nothing is typed from here")

        def watch(until_min, names):
            while time.time() - idle_from < until_min * 60:
                time.sleep(60)
                for n in names:
                    p = probes[n]
                    st = state_of(p.sid) or {}
                    key = (len(p.compactions()), (st.get("vetoes") or {}).get("count"),
                           (st.get("ticks") or {}).get("passed"), (st.get("ticks") or {}).get("skipped"))
                    if res.get("last_" + n) != list(key):
                        res["last_" + n] = list(key)
                        note("%s at %.0f min: compactions %d, vetoes %s, ticks passed %s, skipped %s"
                             % ((n, (time.time() - idle_from) / 60) + key))

        watch(65, "ABC")
        for n in "AB":
            p = probes[n]
            res[n] = {"compactions": p.compactions(), "state": state_of(p.sid),
                      "context_before_question": context_tokens(p.rows())}
        a = probes["A"]
        a.ask("Without using any tool, from what you read earlier: what is the code word for file 5?")
        answer = json.dumps(a.rows()[-12:])
        res["A"]["recalled_code_word"] = a.secrets[5] in answer
        note("A: asked for file 5's code word; recalled from context: %s" % res["A"]["recalled_code_word"])
        for n in "AB":
            probes[n].s.kill()
        note("A and B stopped")

        watch(args.wait, "C")
        c = probes["C"]
        res["C"] = {"compactions": c.compactions(), "state": state_of(c.sid)}
        c.ask("Without using any tool, from what you read earlier: what is the code word for file 3?")
        res["C"]["recalled_code_word"] = c.secrets[3] in json.dumps(c.rows()[-12:])
        note("C: recalled file 3's code word from context: %s" % res["C"]["recalled_code_word"])
    finally:
        for p in probes.values():
            if p.s is not None:
                p.s.kill()
        now = settings.read_bytes() if settings.exists() else None
        res["settings_json_unchanged"] = now == settings_before
        (root / "result.json").write_text(json.dumps(res, indent=2))

    checks = {
        "B (control) compacted while idle": bool(res.get("B", {}).get("compactions")),
        "A not compacted": not res.get("A", {}).get("compactions"),
        "A vetoed at least once": ((res.get("A", {}).get("state") or {}).get("vetoes") or {}).get("count", 0) >= 1,
        "A kept its context": bool(res.get("A", {}).get("recalled_code_word")),
        "C not compacted": not res.get("C", {}).get("compactions"),
        "C ticks passed at least 3": ((res.get("C", {}).get("state") or {}).get("ticks") or {}).get("passed", 0) >= 3,
        "C kept its context": bool(res.get("C", {}).get("recalled_code_word")),
        "settings.json unchanged": res["settings_json_unchanged"],
    }
    res["checks"] = checks
    (root / "result.json").write_text(json.dumps(res, indent=2))
    for k, v in checks.items():
        print("%s  %s" % ("PASS" if v else "FAIL", k))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
