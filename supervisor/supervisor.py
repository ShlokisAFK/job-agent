"""Laptop supervisor: syncs with the Pi, decides whether to wake Claude, checks Claude's work,
updates the tracker, makes Gmail drafts and pushes the digest to your phone.

Run it every 30 minutes (Task Scheduler) or by hand:
    python -m supervisor.supervisor            normal run
    python -m supervisor.supervisor --force    wake Claude now (still respects STOP and the daily cap)
    python -m supervisor.supervisor --demo     sample data, no Pi, no Claude, no Gmail, no phone

Safety: a STOP file in the workspace stops everything. Claude gets only Read/Write/Edit in the
workspace (no shell, no web), a hard timeout and a daily wake cap.
"""
import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from agent import notify, picks, rank, tracker  # noqa: E402

WS_FILES = {"state.md": "# State\n(no runs yet)\n", "chat.md": "# Chat\n", "schedule.md": "",
            "my_updates.md": "# Write one line per job: <id> <sent|skip|applied|replied|interview|rejected|offer> [- note]\n",
            "pi_inbox.md": ""}
HARD_WAKE_CAP = 4  # never more than 4 Claude wakes a day, whatever config.json says


class Supervisor:
    def __init__(self, cfg, ws, now=None, runner=None):
        self.cfg, self.ws = cfg, Path(ws)
        self.now = now or datetime.now()
        self.today = self.now.strftime("%Y-%m-%d")
        self.runner = runner or self._run_claude
        self.ws.mkdir(parents=True, exist_ok=True)
        for name, default in WS_FILES.items():
            p = self.ws / name
            if not p.exists():
                p.write_text(default, encoding="utf-8")
        (self.ws / "inbox").mkdir(exist_ok=True)
        (self.ws / "context").mkdir(exist_ok=True)

    # ---------- small helpers ----------
    def log(self, event, **kw):
        with (self.ws / "log.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": self.now.isoformat(timespec="seconds"), "who": "supervisor",
                                "event": event, **kw}, ensure_ascii=False) + "\n")
        print(f"[{event}]", kw if kw else "")

    def chat(self, line):
        with (self.ws / "chat.md").open("a", encoding="utf-8") as f:
            f.write(f"- [supervisor {self.now:%Y-%m-%d %H:%M}] {line}\n")

    def wakes_today(self):
        p = self.ws / "log.jsonl"
        if not p.exists():
            return 0
        return sum(1 for l in p.read_text(encoding="utf-8").splitlines()
                   if '"event": "wake"' in l and l.startswith(f'{{"ts": "{self.today}'))

    def next_wake(self):
        for line in (self.ws / "schedule.md").read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("next_wake:"):
                try:
                    return datetime.fromisoformat(line.split(":", 1)[1].strip())
                except ValueError:
                    self.log("bad_schedule", line=line)
        h, m = map(int, self.cfg.get("daily_time", "09:00").split(":"))
        return self.now.replace(hour=h, minute=m, second=0, microsecond=0)

    def set_next_wake(self, when):
        (self.ws / "schedule.md").write_text(f"next_wake: {when:%Y-%m-%dT%H:%M}\n", encoding="utf-8")

    def wake_cap(self):
        return min(self.cfg.get("max_wakes_per_day", 2), HARD_WAKE_CAP)

    def plan_retry(self):
        """After a run with 0 picks, try again later today on the next unreviewed jobs, within the cap."""
        if self.wakes_today() >= self.wake_cap():
            return None
        when = self.now + timedelta(hours=self.cfg.get("retry_gap_hours", 3))
        h, m = map(int, self.cfg.get("retry_until", "21:00").split(":"))
        if when > self.now.replace(hour=h, minute=m, second=0, microsecond=0):
            return None
        self.set_next_wake(when)
        return when

    # ---------- Pi sync ----------
    def pull_from_pi(self):
        host, remote = self.cfg.get("pi_host"), self.cfg.get("pi_data_dir")
        if not host:
            return
        for name in ("backlog.jsonl", "pi_outbox.md"):
            r = subprocess.run(["scp", "-q", f"{host}:{remote}/{name}", str(self.ws / "inbox" / name)],
                               capture_output=True, text=True, timeout=60)
            if r.returncode:
                self.log("pi_pull_failed", file=name, err=r.stderr.strip()[:200])

    def push_to_pi(self):
        host, remote = self.cfg.get("pi_host"), self.cfg.get("pi_data_dir")
        src = self.ws / "pi_inbox.md"
        if host and src.stat().st_size:
            r = subprocess.run(["scp", "-q", str(src), f"{host}:{remote}/pi_inbox.md"],
                               capture_output=True, text=True, timeout=60)
            if r.returncode:
                self.log("pi_push_failed", err=r.stderr.strip()[:200])

    def backlog(self):
        p = self.ws / "inbox" / "backlog.jsonl"
        if not p.exists():
            return {}
        jobs = {}
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                j = json.loads(line)
                jobs[j["key"]] = j
        return jobs

    def new_pi_notes(self):
        """Pi notes not shown to Claude yet (tracked by line count)."""
        p = self.ws / "inbox" / "pi_outbox.md"
        lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
        mark = self.ws / "pi_notes_seen.txt"
        seen = int(mark.read_text()) if mark.exists() else 0
        return lines[seen:], len(lines)

    # ---------- your updates ----------
    def merge_updates(self):
        p = self.ws / "my_updates.md"
        text = p.read_text(encoding="utf-8")
        ups, bad = tracker.parse_updates(text)
        if not ups and not bad:
            return []
        rows = tracker.read(self.ws / "tracker.csv")
        done, unknown = tracker.apply_updates(rows, ups, self.today)
        tracker.write(self.ws / "tracker.csv", rows)
        for rid, status in done:
            if status == "sent":
                self.outreach_mark_sent(next(r for r in rows if r["id"] == rid))
        with (self.ws / "updates_archive.md").open("a", encoding="utf-8") as f:
            f.write(f"\n## {self.today}\n" + "\n".join(l for l in text.splitlines() if l.strip() and not l.startswith("#")) + "\n")
        p.write_text(WS_FILES["my_updates.md"] + "".join(f"# could not read: {b}\n" for b in bad), encoding="utf-8")
        self.log("updates_merged", done=len(done), unknown=unknown, bad=bad)
        return done

    # ---------- Claude ----------
    def _run_claude(self, prompt):
        exe = shutil.which("claude")
        if not exe:
            raise RuntimeError("claude CLI not found on PATH")
        cmd = [exe, "-p", prompt, "--model", self.cfg.get("claude_model", "sonnet"), "--permission-mode", "acceptEdits",
               "--allowedTools", "Read", "Write", "Edit",
               "--disallowedTools", "Bash", "PowerShell", "WebFetch", "WebSearch", "Agent"]
        r = subprocess.run(cmd, cwd=self.ws, capture_output=True, text=True, encoding="utf-8",
                           timeout=self.cfg.get("claude_timeout_s", 600))
        return r.returncode, (r.stdout or "")[-2000:], (r.stderr or "")[-1000:]

    def prepare_context(self, jobs, short, notes):
        # A compact profile (about 3 KB) is read on every wake instead of the full master resume.
        src = Path(self.cfg.get("profile_md") or self.cfg["master_resume"])
        shutil.copyfile(src, self.ws / "context" / "profile.md")
        (self.ws / "context" / "master_resume.md").unlink(missing_ok=True)
        out = ["# Candidates (pre-ranked by keywords; judge them yourself)", ""]
        for s, j in short:
            out += [f"## {j['key']}", f"{j['company_name']} | {j['title']} | {j['location']} | score {s}",
                    j["description"][:self.cfg.get("chars_per_job", 2000)], ""]
        (self.ws / "candidates.md").write_text("\n".join(out), encoding="utf-8")
        rows = tracker.read(self.ws / "tracker.csv")
        recent = [f"- #{r['id']} {r['company']}: {r['title']} -> {r['status'] or 'no update yet'} {r['notes']}"
                  for r in rows[-40:]]
        (self.ws / "recent_updates.md").write_text("# Recent picks and what the user did\n" + "\n".join(recent) + "\n",
                                                   encoding="utf-8")
        (self.ws / "pi_notes.md").write_text("# New notes from the Pi\n" + "\n".join(notes) + "\n", encoding="utf-8")
        (self.ws / "picks.json").unlink(missing_ok=True)

    def process_picks(self, jobs):
        p = self.ws / "picks.json"
        if not p.exists():
            self.log("no_picks_file")
            return []
        cands = {k: jobs[k] for k in self.shortlist_keys}
        good, problems = picks.validate(p.read_text(encoding="utf-8"), cands)
        for pr in problems:
            self.log("pick_problem", detail=pr)
        rows = tracker.read(self.ws / "tracker.csv")
        new_rows = []
        for g in good:
            j = jobs[g["key"]]
            row = {"id": str(tracker.next_id(rows)), "key": j["key"], "company": j["company_name"],
                   "title": j["title"], "location": j["location"], "url": j["url"], "posted": j["posted"],
                   "picked_on": self.today, "why": g["why"], "hook": g["hook"], "draft": "",
                   "status": "", "updated_on": "", "notes": ""}
            if g["contact_email"]:
                row["draft"] = self.outreach_draft(row, g)
            rows.append(row)
            new_rows.append(row)
        tracker.write(self.ws / "tracker.csv", rows)
        with (self.ws / "reviewed.txt").open("a", encoding="utf-8") as f:
            f.write("\n".join(self.shortlist_keys) + "\n")
        self.log("picks_saved", picked=len(new_rows), problems=len(problems))
        return new_rows

    # ---------- outreach tool (draft-only) ----------
    def _outreach(self, args):
        od = self.cfg.get("outreach_dir")
        if not od or self.cfg.get("demo"):
            return None
        return subprocess.run([sys.executable, "-m", "outreach", *args], cwd=od,
                              capture_output=True, text=True, timeout=180)

    def outreach_draft(self, row, g):
        csv_path = self.ws / "outreach_contacts.csv"
        new = not csv_path.exists()
        with csv_path.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["company", "contact", "email", "role", "hook", "status", "sent date", "follow up date"])
            w.writerow([row["company"], g["contact_name"] or "Hiring Team", g["contact_email"],
                        row["title"], g["hook"], "Ready", "", ""])
        r = self._outreach(["draft", str(csv_path)])
        if r is None:
            return "demo: not drafted"
        self.log("outreach_draft", rc=r.returncode, out=r.stdout[-300:], err=r.stderr[-300:])
        return "drafted" if r.returncode == 0 and "Drafted:" in r.stdout else "draft failed"

    def outreach_mark_sent(self, row):
        if row.get("draft") != "drafted":
            return
        r = self._outreach(["mark_sent", str(self.ws / "outreach_contacts.csv"), row["company"]])
        if r is not None:
            self.log("outreach_mark_sent", rc=r.returncode, out=r.stdout[-200:], err=r.stderr[-200:])

    # ---------- main flow ----------
    def run(self, force=False):
        if (self.ws / "STOP").exists():
            self.log("stopped", reason="STOP file present")
            return "stopped"
        self.pull_from_pi()
        self.merge_updates()
        jobs = self.backlog()
        notes, n_lines = self.new_pi_notes()
        urgent = any("@claude" in n for n in notes)
        due = force or urgent or self.now >= self.next_wake()
        if not due:
            return "not due"
        if self.wakes_today() >= self.wake_cap():
            self.log("cap_reached", wakes=self.wakes_today())
            return "cap"
        excluded = {r["key"] for r in tracker.read(self.ws / "tracker.csv")}
        rv = self.ws / "reviewed.txt"
        if rv.exists():
            excluded |= set(rv.read_text(encoding="utf-8").split())
        short = rank.shortlist(list(jobs.values()), excluded, self.cfg.get("shortlist_size", 30),
                               self.cfg.get("profile", {}))
        if not short and not urgent:
            self.log("nothing_new")
            self.set_next_wake((self.now + timedelta(days=1)).replace(hour=int(self.cfg.get("daily_time", "09:00")[:2]), minute=0))
            return "nothing new"
        self.shortlist_keys = [j["key"] for _, j in short]
        self.prepare_context(jobs, short, notes)
        prompt = (ROOT / "supervisor" / "claude_task.md").read_text(encoding="utf-8")
        self.log("wake", candidates=len(short), urgent=urgent)
        chat_before = len((self.ws / "chat.md").read_text(encoding="utf-8").splitlines())
        try:
            rc, out, err = self.runner(prompt)
            self.log("claude_done", rc=rc, out=out[-500:], err=err[-300:])
        except subprocess.TimeoutExpired:
            self.log("claude_timeout")
            self.chat("Claude hit the time limit; partial work kept, picks checked as usual.")
        (self.ws / "pi_notes_seen.txt").write_text(str(n_lines))
        if self.next_wake() <= self.now:  # Claude did not schedule itself: default to tomorrow
            self.set_next_wake((self.now + timedelta(days=1)).replace(hour=int(self.cfg.get("daily_time", "09:00")[:2]), minute=0))
        new_rows = self.process_picks(jobs)
        if new_rows:
            title = f"job-agent: {len(new_rows)} picks"
            body = picks.digest(new_rows, self.today)
            self.chat(f"picked {len(new_rows)} jobs; digest written")
        else:
            retry = self.plan_retry()
            said = [l[len("- [claude] "):] for l in
                    (self.ws / "chat.md").read_text(encoding="utf-8").splitlines()[chat_before:]
                    if l.startswith("- [claude] ")]
            nxt = (f"Trying the next unreviewed jobs at {retry:%H:%M} (wake {self.wakes_today()} of {self.wake_cap()} today)."
                   if retry else f"No more tries today; next run {self.next_wake():%Y-%m-%d %H:%M}.")
            title = "job-agent: 0 picks"
            body = "\n".join([f"0 picks from {len(short)} jobs on {self.today}.", nxt, "",
                              "Claude said:", *(f"- {s}" for s in said or ["(no notes)"])])
            self.chat(f"picked 0 jobs; {nxt}")
        (self.ws / "digest.md").write_text(body, encoding="utf-8")
        topic = self.cfg.get("ntfy_topic")
        if topic and not self.cfg.get("demo"):
            try:
                notify.send(topic, title, body)
                self.log("notified", n=len(new_rows))
            except Exception as e:
                self.log("notify_failed", err=str(e)[:200])
        self.push_to_pi()
        return f"picked {len(new_rows)}"


def load_cfg(path):
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"Config not found: {p}. Copy config.example.json to config.json.")
    return json.loads(p.read_text(encoding="utf-8"))


def demo_runner(ws):
    """Stands in for Claude in demo mode: picks the top 3 candidates."""
    def run(_prompt):
        keys = [l[3:].strip() for l in (ws / "candidates.md").read_text(encoding="utf-8").splitlines() if l.startswith("## ")]
        (ws / "picks.json").write_text(json.dumps({"picks": [
            {"key": k, "why": "demo pick", "hook": "your work in this area"} for k in keys[:3]]}), encoding="utf-8")
        return 0, "demo", ""
    return run


def main():
    ap = argparse.ArgumentParser(description="job-agent laptop supervisor")
    ap.add_argument("--config", default=os.environ.get("JOB_AGENT_CONFIG", str(ROOT / "config.json")))
    ap.add_argument("--force", action="store_true", help="wake Claude now (STOP file and daily cap still apply)")
    ap.add_argument("--demo", action="store_true", help="run on sample data with a fake Claude; touches nothing outside demo_ws")
    a = ap.parse_args()
    if a.demo:
        ws = ROOT / "demo_ws"
        shutil.rmtree(ws, ignore_errors=True)
        (ws / "inbox").mkdir(parents=True)
        shutil.copyfile(ROOT / "demo" / "sample_backlog.jsonl", ws / "inbox" / "backlog.jsonl")
        cfg = {"demo": True, "master_resume": str(ROOT / "demo" / "sample_resume.md")}
        sup = Supervisor(cfg, ws, runner=demo_runner(ws))
        print(sup.run(force=True))
        print((ws / "digest.md").read_text(encoding="utf-8"))
        return
    cfg = load_cfg(a.config)
    print(Supervisor(cfg, cfg.get("workspace", ROOT / "workspace")).run(force=a.force))


if __name__ == "__main__":
    main()
