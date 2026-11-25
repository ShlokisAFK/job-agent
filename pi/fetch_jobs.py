"""Pi collector: fetch public job boards, keep new jobs.

Boards: Greenhouse, Lever, Ashby (small and mid companies), Workday and SmartRecruiters
(large companies; searched for intern / graduate roles in India on the server side).

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
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
UA = "job-agent/0.1 (personal job search; contact via GitHub)"


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _post(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"User-Agent": UA, "Accept": "application/json",
                                          "Content-Type": "application/json"})
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


# ---------- large-company boards: searched server side, descriptions fetched only for new jobs ----------
WORKDAY_QUERIES = ("intern", "graduate", "new grad", "entry level")
# Titles that are clearly not entry level: skipped before spending a request on their description.
SKIP_TITLE = re.compile(r"\b(senior|sr\.?|staff|principal|director|manager|head|vp|architect|lead)\b", re.I)
# Large companies post every kind of job; only tech titles are worth a description request.
TECH_TITLE = re.compile(r"(engineer|developer|software|data|machine learning|\bml\b|\bai\b|scientist|research|"
                        r"\bsde\b|devops|\bsre\b|cloud|security|backend|front.?end|full.?stack|embedded|firmware|"
                        r"python|java|automation|\btest)", re.I)
# Workday's description endpoint allows about 6 requests a minute, so it is paced; SmartRecruiters is relaxed.
DETAIL_PAUSE = {"workday": 11.0, "smartrecruiters": 1.5}
DETAIL_CAPS = {"workday": 30, "smartrecruiters": 60}  # description requests per board type per run
SR_SKIP_LEVELS = {"mid_senior_level", "director", "executive"}  # SmartRecruiters experienceLevel ids


def _find_facet(nodes, wanted, param=None):
    """Depth-first search of a Workday facet tree for a value called `wanted`. Returns (facetParameter, id)."""
    for n in nodes or []:
        p = n.get("facetParameter") or param
        if p and n.get("id") and n.get("descriptor") == wanted:
            return p, n["id"]
        hit = _find_facet(n.get("values"), wanted, p)
        if hit:
            return hit
    return None


def workday_jobs(slug, post=_post, pause=time.sleep, queries=WORKDAY_QUERIES, max_pages=5):
    """slug = 'tenant/wdN/site'. Keyword search limited to India; descriptions are filled in later."""
    tenant, wd, site = slug.split("/")
    host = f"https://{tenant}.{wd}.myworkdayjobs.com"
    api = f"{host}/wday/cxs/{tenant}/{site}"
    found, applied = {}, None
    for q in queries:
        for page in range(max_pages):
            payload = {"appliedFacets": applied or {}, "limit": 20, "offset": page * 20, "searchText": q}
            data = post(f"{api}/jobs", payload)
            pause(1.5)
            if applied is None:
                hit = _find_facet(data.get("facets"), "India")
                if not hit:
                    raise ValueError("board has no India location filter")
                applied = {hit[0]: [hit[1]]}
                data = post(f"{api}/jobs", {**payload, "appliedFacets": applied})
                pause(1.5)
            for p in data.get("jobPostings", []):
                path = p.get("externalPath")
                if path and path not in found:
                    found[path] = p
            if (page + 1) * 20 >= data.get("total", 0):
                break
    jobs = []
    for path, p in found.items():
        title = p.get("title", "")
        if SKIP_TITLE.search(title) or not TECH_TITLE.search(title):
            continue
        req_id = (p.get("bulletFields") or [path.rsplit("_", 1)[-1]])[0]
        loc = p.get("locationsText", "") or ""
        if "india" not in loc.lower():  # "2 Locations": the India filter already applied; the path has the first city
            parts = path.split("/")
            city = parts[2].replace("-", ", ") if len(parts) > 2 and parts[2].lower().startswith("india") else ""
            loc = city or f"{loc} (India)".strip()
        jobs.append({"key": f"wd:{tenant}:{req_id}", "company": tenant, "title": title, "location": loc,
                     "url": f"{host}/en-US/{site}{path}", "posted": "", "updated": "",
                     "posted_note": p.get("postedOn", ""), "description": "",
                     "detail_url": f"{api}{path}", "detail_kind": "workday"})
    return jobs


def smartrecruiters_jobs(slug, getter=_get, pause=time.sleep, max_offset=500):
    """slug = company identifier. India postings only; entry-level filter uses SmartRecruiters' own experienceLevel."""
    jobs, offset = [], 0
    while offset < max_offset:
        d = getter(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&offset={offset}&country=in")
        pause(0.5)
        for p in d.get("content", []):
            level = (p.get("experienceLevel") or {}).get("id", "")
            name = p.get("name", "")
            if level in SR_SKIP_LEVELS or SKIP_TITLE.search(name) or not TECH_TITLE.search(name):
                continue
            loc = p.get("location") or {}
            jobs.append({"key": f"sr:{slug}:{p['id']}", "company": slug, "title": p.get("name", ""),
                         "location": f"{loc.get('city', '')}, India".strip(", "),
                         "url": f"https://jobs.smartrecruiters.com/{slug}/{p['id']}", "posted": p.get("releasedDate") or "",
                         "updated": "", "level": level, "description": "",
                         "detail_url": f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{p['id']}",
                         "detail_kind": "smartrecruiters"})
        offset += 100
        if offset >= d.get("totalFound", 0):
            break
    return jobs


def hydrate(job, getter=_get):
    """Fill in the description of a job that only has a list entry. Removes the helper fields once it worked."""
    url, kind = job.get("detail_url", ""), job.get("detail_kind", "")
    if not url:
        return job
    d = getter(url)
    if kind == "workday":
        info = d.get("jobPostingInfo") or {}
        job["description"] = _plain(info.get("jobDescription", ""))
        job["posted"] = info.get("startDate") or ""
    elif kind == "smartrecruiters":
        sections = (d.get("jobAd") or {}).get("sections") or {}
        text = " ".join((sections.get(k) or {}).get("text", "")
                        for k in ("jobDescription", "qualifications", "additionalInformation"))
        job["description"] = _plain(text)
    job.pop("detail_url", None)
    job.pop("detail_kind", None)
    return job


def fetch(source, company):
    if source == "workday":
        return workday_jobs(company)
    if source == "smartrecruiters":
        return smartrecruiters_jobs(company)
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


def run(cfg, data_dir, fetcher=fetch, now=None, hydrator=hydrate, pause=time.sleep):
    now = now or datetime.now(timezone.utc)
    data_dir.mkdir(parents=True, exist_ok=True)
    backlog = data_dir / "backlog.jsonl"
    seen = load_keys(backlog)
    words = [w.lower() for w in cfg["location_words"]]
    new, errors, throttled = [], [], []
    budget = {**DETAIL_CAPS, **cfg.get("detail_caps", {})}  # per run; jobs over the cap wait for the next run
    for c in cfg["companies"]:
        blocked = False  # set when this board keeps answering 429; other boards are unaffected
        try:
            for job in fetcher(c["source"], c["slug"]):
                if job["key"] in seen or not location_ok(job["location"], words):
                    continue
                if job.get("detail_url"):
                    kind = job.get("detail_kind", "")
                    if budget.get(kind, 0) <= 0 or blocked:
                        continue  # not saved, so the next run picks it up
                    for attempt in (1, 2):
                        try:
                            job = hydrator(job)
                            budget[kind] -= 1
                            pause(DETAIL_PAUSE.get(kind, 2.0))
                            break
                        except urllib.error.HTTPError as e:
                            if e.code != 429:
                                break
                            if attempt == 1:
                                pause(65)  # one patient retry: the limit is per minute
                            else:
                                blocked = True
                                throttled.append(c["slug"])
                    if job.get("detail_url"):
                        continue  # still has no description: skip it for now
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
    if throttled:
        note += f"; rate limited at {throttled[0]}, the rest continues next run"
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
