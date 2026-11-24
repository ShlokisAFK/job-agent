"""Pi collector: fetch public job boards (Greenhouse, Lever, Ashby), keep new jobs.

Standard library only, so it runs on the Pi with no installs.
Every field comes straight from the board's API. Nothing is guessed: a missing
date stays empty and is shown as "date not listed" later.

Writes (one writer per file):
  data/backlog.jsonl   one job per line, appended, deduplicated by key
  pi_outbox.md         short human-readable notes for the supervisor / Claude
  data/pi_log.jsonl    one event per run
"""
import html
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
UA = "job-agent/0.1 (personal job search; contact via GitHub)"


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _plain(text, limit=4000):
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _iso_ms(ms):
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat() if ms else ""


def normalize(source, company, raw):
    """Map one raw posting to our schema. Returns dict or None."""
    if source == "greenhouse":
        return {"key": f"gh:{company}:{raw['id']}", "company": company, "title": raw.get("title", ""),
                "location": (raw.get("location") or {}).get("name", "") or "",
                "url": raw.get("absolute_url", ""),
                "posted": raw.get("first_published") or "", "updated": raw.get("updated_at") or "",
                "description": _plain(raw.get("content", ""))}
    if source == "lever":
        cats = raw.get("categories") or {}
        return {"key": f"lv:{company}:{raw['id']}", "company": company, "title": raw.get("text", ""),
                "location": cats.get("location", "") or "", "url": raw.get("hostedUrl", ""),
                "posted": _iso_ms(raw.get("createdAt")), "updated": "",
                "description": _plain(raw.get("descriptionPlain") or raw.get("description", ""))}
    if source == "ashby":
        loc = raw.get("location", "") or ""
        if raw.get("isRemote") and "remote" not in loc.lower():
            loc = f"{loc} (Remote)".strip()
        return {"key": f"ab:{company}:{raw['id']}", "company": company, "title": raw.get("title", ""),
                "location": loc, "url": raw.get("jobUrl", ""), "posted": raw.get("publishedAt") or "",
                "updated": "", "description": _plain(raw.get("descriptionPlain") or raw.get("descriptionHtml", ""))}
    raise ValueError(f"unknown source {source}")


def fetch(source, company):
    if source == "greenhouse":
        rows = _get(f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs?content=true")["jobs"]
    elif source == "lever":
        rows = _get(f"https://api.lever.co/v0/postings/{company}?mode=json")
    elif source == "ashby":
        rows = _get(f"https://api.ashbyhq.com/posting-api/job-board/{company}")["jobs"]
    else:
        raise ValueError(source)
    return [normalize(source, company, r) for r in rows]


GLOBAL_REMOTE = {"remote", "remote (global)", "anywhere", "worldwide", "remote - anywhere", "(remote)"}


def location_ok(loc, words):
    """India locations always; 'remote' only when no other country is named ("Remote - Germany" is out)."""
    loc = (loc or "").lower().strip()
    if any(w in loc for w in words if w != "remote"):
        return True
    return loc in GLOBAL_REMOTE


def load_keys(path):
    keys = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                keys.add(json.loads(line)["key"])
    return keys


def run(cfg, data_dir, fetcher=fetch, now=None):
    now = now or datetime.now(timezone.utc)
    data_dir.mkdir(parents=True, exist_ok=True)
    backlog = data_dir / "backlog.jsonl"
    seen = load_keys(backlog)
    words = [w.lower() for w in cfg["location_words"]]
    new, errors = [], []
    for c in cfg["companies"]:
        try:
            for job in fetcher(c["source"], c["slug"]):
                if job["key"] in seen or not location_ok(job["location"], words):
                    continue
                job["company_name"] = c.get("name", c["slug"])
                job["fetched"] = now.isoformat()
                new.append(job)
                seen.add(job["key"])
        except Exception as e:  # one broken board must not stop the others
            errors.append(f"{c['slug']}: {type(e).__name__}: {e}")
    with backlog.open("a", encoding="utf-8") as f:
        for job in new:
            f.write(json.dumps(job, ensure_ascii=False) + "\n")
    event = {"ts": now.isoformat(), "who": "pi", "event": "fetch", "new": len(new), "errors": errors}
    with (data_dir / "pi_log.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")
    note = f"- [{now:%Y-%m-%d %H:%M} UTC] pi: fetched {len(new)} new jobs"
    if errors:
        note += f"; @claude {len(errors)} board(s) failed: " + "; ".join(errors)
    with (data_dir / "pi_outbox.md").open("a", encoding="utf-8") as f:
        f.write(note + "\n")
    return new, errors


def main():
    cfg = json.loads((HERE / "companies.json").read_text(encoding="utf-8"))
    data_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "data"
    new, errors = run(cfg, data_dir)
    print(f"new {len(new)}, errors {len(errors)}")
    for e in errors:
        print("ERROR", e, file=sys.stderr)


if __name__ == "__main__":
    main()
