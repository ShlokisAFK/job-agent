"""The one tracker. Pipeline columns are written by code/Claude; your columns by your updates."""
import csv
import os
import re
import tempfile
from datetime import date
from pathlib import Path

PIPE_COLS = ["id", "key", "company", "title", "location", "url", "posted", "picked_on", "why", "hook", "draft"]
YOUR_COLS = ["status", "updated_on", "notes"]
COLUMNS = PIPE_COLS + YOUR_COLS
STATUSES = {"sent", "skip", "applied", "followed", "replied", "interview", "rejected", "offer"}
WAITING = {"sent", "applied"}  # no answer yet; any later update (including "followed") ends the reminder


def read(path):
    path = Path(path)
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write(path, rows):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def next_id(rows):
    return max((int(r["id"]) for r in rows if r.get("id", "").isdigit()), default=0) + 1


LINE = re.compile(r"^\s*(\d+)\s+([a-z]+)\s*(?:[-:]\s*(.*))?$", re.I)


def parse_updates(text):
    """'3 sent' / '7 skip' / '2 replied - interview Friday'. Returns (updates, bad_lines)."""
    ups, bad = [], []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = LINE.match(s)
        if not m or m.group(2).lower() not in STATUSES:
            bad.append(s)
            continue
        ups.append((m.group(1), m.group(2).lower(), (m.group(3) or "").strip()))
    return ups, bad


def apply_updates(rows, updates, today):
    by_id = {r["id"]: r for r in rows}
    done, unknown = [], []
    for rid, status, note in updates:
        r = by_id.get(rid)
        if r is None:
            unknown.append(rid)
            continue
        r["status"], r["updated_on"] = status, today
        if note:
            r["notes"] = (r.get("notes", "") + " | " if r.get("notes") else "") + note
        done.append((rid, status))
    return done, unknown


def follow_ups(rows, today, after_days):
    """Rows still waiting for an answer after `after_days`. Returns [(row, days_waiting)]."""
    out = []
    for r in rows:
        if r.get("status") not in WAITING:
            continue
        try:
            days = (date.fromisoformat(today) - date.fromisoformat(r.get("updated_on", ""))).days
        except ValueError:
            continue
        if days >= after_days:
            out.append((r, days))
    return out
