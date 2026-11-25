"""Pi digger: finds job boards that the fixed company list does not know about.

What the local model does (small jobs, every answer checked by code):
  - reads search-result snippets and names companies that hire interns / new grads in India
  - when a careers page shows no job board, picks which link on it leads to the openings
What plain code does:
  - all searching and fetching (Bing HTML search, no API key; public web addresses only)
  - recognises the job-board system on a company's OWN careers page (Greenhouse, Lever, Ashby,
    Workday, SmartRecruiters), then checks the board really exists and has India jobs
  - only then adds it to companies.json, so the normal fetch picks it up

The model never writes files or chooses what gets added. A company name it invents is dropped unless it
appears in the search text, and a board is added only if the live board answers with India jobs.

Run:  python3 digger.py [data_dir] [--discover] [--only "Razorpay,Swiggy"] [--max 12]
"""
import argparse
import base64
import html
import ipaddress
import json
import re
import socket
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fetch_jobs  # noqa: E402

LLM_URL = "http://127.0.0.1:8080/v1/chat/completions"
UA = "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
SEARCHES_PER_DAY = 40
RECHECK_DAYS = {"added": 365, "none": 21, "error": 3}
INDIA_WORDS = ("india", "bengaluru", "bangalore", "mumbai", "pune", "delhi", "gurgaon", "gurugram", "hyderabad",
               "noida", "chennai", "kolkata", "ahmedabad")
DISCOVERY_QUERIES = (
    "software engineer intern 2027 India startup careers",
    "machine learning intern India 2027 hiring",
    "AI engineer new grad India 2027 careers",
    "backend engineer fresher India product company hiring",
    "data science intern India 2027 apply",
)
AGGREGATORS = ("linkedin", "indeed", "naukri", "internshala", "glassdoor", "monster", "foundit", "shine", "wellfound",
               "angellist", "ziprecruiter", "simplyhired", "cutshort", "instahyre", "unstop", "letsintern", "youtube", "quora")
CAREER_WORDS = re.compile(r"(career|jobs|openings|positions|join[- ]us|work[- ]with[- ]us|we.re hiring|opportunit)", re.I)


# ---------- safe web access ----------
def public_url(url):
    """Only http(s) addresses that resolve to public IPs. The model can never point us at the Pi itself or the LAN."""
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    try:
        infos = socket.getaddrinfo(p.hostname, None)
    except OSError:
        return False
    return bool(infos) and all(ipaddress.ip_address(i[4][0]).is_global for i in infos)


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not public_url(newurl):
            raise ValueError("redirect to a non-public address blocked")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def http_text(url, limit=1_500_000):
    if not public_url(url):
        raise ValueError("non-public address blocked")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*", "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.build_opener(_SafeRedirect).open(req, timeout=25) as r:
        return r.geturl(), r.read(limit).decode("utf-8", "ignore")


def _strip(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(s or ""))).strip()


def unbing(link):
    """Bing wraps result links as bing.com/ck/a?...&u=a1<base64 of the real url>."""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
    u = (q.get("u") or [""])[0]
    if u.startswith("a1"):
        try:
            raw = u[2:] + "=" * (-len(u[2:]) % 4)
            return base64.urlsafe_b64decode(raw).decode("utf-8", "ignore")
        except Exception:
            return ""
    return link if "bing.com" not in link else ""


def bing_search(query, fetch=http_text):
    _, t = fetch("https://www.bing.com/search?q=" + urllib.parse.quote(query) + "&setlang=en&cc=in")
    out = []
    for block in re.findall(r'<li class="b_algo".*?</li>', t, re.S):
        m = re.search(r'<h2[^>]*><a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        url = unbing(html.unescape(m.group(1)))
        if not url.startswith("http"):
            continue
        sn = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        out.append({"url": url, "title": _strip(m.group(2)), "snippet": _strip(sn.group(1)) if sn else ""})
    return out


# ---------- recognising a job board on a page ----------
BOARD_PATTERNS = (
    ("greenhouse", re.compile(r"(?:boards|job-boards)(?:-api)?\.greenhouse\.io/(?:v1/boards/)?([A-Za-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board(?:/js)?\?for=([A-Za-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"(?:jobs|api)\.lever\.co/(?:v0/postings/)?([A-Za-z0-9_-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)", re.I)),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)", re.I)),
)
WORKDAY = re.compile(r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Za-z]{2}/)?([A-Za-z0-9_-]+)", re.I)
NOT_A_SLUG = {"embed", "v1", "boards", "jobs", "api", "static", "assets", "js", "css", "img", "images", "wday", "job", "apply"}


def detect_boards(text):
    """[(source, slug)] for every supported job board referenced in the page, in order of appearance."""
    found = []
    for source, rx in BOARD_PATTERNS:
        for m in rx.finditer(text):
            slug = urllib.parse.unquote(m.group(1))
            if slug.lower() not in NOT_A_SLUG and (source, slug) not in found:
                found.append((source, slug))
    for m in WORKDAY.finditer(text):
        tenant, wd, site = m.group(1), m.group(2).lower(), m.group(3)
        if site.lower() not in NOT_A_SLUG and tenant.lower() != "wd":
            item = ("workday", f"{tenant}/{wd}/{site}")
            if item not in found:
                found.append(item)
    return found


def job_links(text, base, keyword=True):
    """Same-site links; with keyword=True only those whose text or address suggests open positions."""
    host = (urllib.parse.urlparse(base).hostname or "").removeprefix("www.")
    out, seen = [], set()
    for m in re.finditer(r'<a[^>]+href="([^"#][^"]*)"[^>]*>(.*?)</a>', text, re.S | re.I):
        url = urllib.parse.urljoin(base, html.unescape(m.group(1)))
        label = _strip(m.group(2))[:80]
        h = (urllib.parse.urlparse(url).hostname or "").removeprefix("www.")
        if url.startswith("http") and url not in seen and (not keyword or CAREER_WORDS.search(label + " " + url)) and (h == host or h.endswith("." + host) or host.endswith("." + h)):
            seen.add(url)
            out.append((url, label))
    return out[:40]


# ---------- the local model ----------
def ask_json(system, user, url=LLM_URL, max_tokens=300):
    body = json.dumps({"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                       "temperature": 0, "max_tokens": max_tokens}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        text = json.load(r)["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0)) if m else {}


def model_pick_links(ask, company, links):
    """Pick up to 2 links that probably lead to the open positions. Indexes are validated."""
    if not links:
        return []
    listing = "\n".join(f"{i + 1}. {label or '(no text)'} -> {u}" for i, (u, label) in enumerate(links))
    out = ask('You help find the job openings page of a company website. Reply with JSON only: {"pick": [numbers]}. '
              'Choose at most 2 links most likely to list OPEN JOB POSITIONS (not culture, benefits or blog pages).',
              f"Company: {company}\nLinks:\n{listing}", max_tokens=40)
    picks = []
    for n in (out.get("pick") or [])[:2]:
        if isinstance(n, int) and 1 <= n <= len(links) and links[n - 1] not in picks:
            picks.append(links[n - 1])
    return picks


def model_company_names(ask, results):
    """Company names hiring interns/new grads in India, taken from search snippets. Each name must appear in the text."""
    text = "\n".join(f"{i + 1}. {r['title']} | {r['snippet']}" for i, r in enumerate(results[:10]))
    out = ask('Read the search results. Reply with JSON only: {"companies": ["Name", ...]}. List companies that are hiring '
              'software, machine learning or AI interns or new graduates in India. Only company names that appear in the '
              'text. Never list job sites or aggregators (LinkedIn, Indeed, Naukri, Internshala, Glassdoor...).', text, max_tokens=200)
    names, low = [], text.lower()
    for n in out.get("companies") or []:
        n = str(n).strip()
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 .&'-]{1,38}", n) and n.lower() in low and not any(a in n.lower() for a in AGGREGATORS) and n not in names:
            names.append(n)
    return names[:8]


# ---------- finding and checking one company's board ----------
def _token(company):
    t = re.sub(r"[^a-z0-9]", "", company.lower().split()[0] if company.split() else "")
    return t if len(t) >= 3 else re.sub(r"[^a-z0-9]", "", company.lower())


def own_site_results(company, results):
    """Search results that really are the company's own website (host contains its name)."""
    tok = _token(company)
    return [r for r in results if tok and tok in (urllib.parse.urlparse(r["url"]).hostname or "").lower().replace("-", "")]


def verify_board(source, slug, fetch=fetch_jobs.fetch):
    jobs = fetch(source, slug)
    india = sum(1 for j in jobs if any(w in (j.get("location") or "").lower() for w in INDIA_WORDS))
    return {"total": len(jobs), "india": india}


def registrable(host):
    """razorpay.com from payroll.razorpay.com; example.co.in from jobs.example.co.in."""
    labels = (host or "").lower().removeprefix("www.").split(".")
    if len(labels) >= 3 and labels[-2] in {"co", "com", "org", "net", "gov", "ac"} and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def candidate_pages(company, own):
    """Where to look, best first: careers-looking search results, the homepage, then the usual careers addresses."""
    urls = [r["url"] for r in own if CAREER_WORDS.search(r["url"] + " " + r["title"])]
    tok = _token(company)
    bases = [registrable(urllib.parse.urlparse(r["url"]).hostname) for r in own][:1] or ([f"{tok}.{t}" for t in ("com", "in", "io", "ai")] if tok else [])
    for base in bases:
        urls += [f"https://{base}/", f"https://{base}/careers", f"https://{base}/jobs", f"https://careers.{base}/", f"https://jobs.{base}/"]
    urls += [r["url"] for r in own[:2]]
    return list(dict.fromkeys(urls))


def find_boards(company, search, fetch, ask, max_fetches=8):
    """Returns (boards, trail). boards = [(source, slug)] seen on the company's own pages."""
    trail, fetched = [], 0
    own = own_site_results(company, search(f"{company} careers jobs India"))
    for url in candidate_pages(company, own):
        if fetched >= max_fetches:
            break
        try:
            final, text = fetch(url)
        except Exception as e:
            trail.append(f"{url[:60]}: {type(e).__name__}")
            continue
        fetched += 1
        trail.append(f"read {final[:70]}")
        boards = detect_boards(text)
        if boards:
            return boards, trail
        follow = job_links(text, final)[:2]
        if not follow:  # no obvious careers link: let the model pick from the page's links
            follow = model_pick_links(ask, company, job_links(text, final, keyword=False)[:40])
        for link, _label in follow:
            if fetched >= max_fetches:
                break
            try:
                f2, t2 = fetch(link)
            except Exception:
                continue
            fetched += 1
            trail.append(f"read {f2[:70]}")
            boards = detect_boards(t2)
            if boards:
                return boards, trail
    return [], trail


# ---------- state, budget, main loop ----------
def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


class Budget:
    """At most SEARCHES_PER_DAY searches a day, spaced out, so this stays a polite low-volume client."""
    def __init__(self, state, now, pause=time.sleep):
        self.state, self.today, self.pause = state, now.strftime("%Y-%m-%d"), pause
        if state.get("searches", {}).get("date") != self.today:
            state["searches"] = {"date": self.today, "n": 0}

    def search(self, query, fn):
        if self.state["searches"]["n"] >= SEARCHES_PER_DAY:
            raise RuntimeError("daily search budget used up")
        self.state["searches"]["n"] += 1
        self.pause(4)
        return fn(query)


def due(state, name, now):
    rec = state.get("companies", {}).get(name.lower())
    if not rec:
        return True
    return now - datetime.fromisoformat(rec["checked"]) > timedelta(days=RECHECK_DAYS.get(rec["status"], 7))


def run(data_dir, cfg_path, seeds, discover=False, only=None, max_companies=12, now=None, search=bing_search,
        fetch=http_text, ask=ask_json, verify=verify_board, pause=time.sleep):
    now = now or datetime.now(timezone.utc)
    state_path = data_dir / "digger_state.json"
    state = load_json(state_path, {"companies": {}, "searches": {}})
    budget = Budget(state, now, pause)
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    have_slugs = {(c["source"], c["slug"].lower()) for c in cfg["companies"]}
    have_names = {c["name"].lower() for c in cfg["companies"]}
    notes, added, todo = [], [], []
    names = list(only or [])
    try:
        if discover and not only:
            try:
                for q in DISCOVERY_QUERIES:
                    results = budget.search(q, search)
                    for n in model_company_names(ask, results):
                        if n.lower() not in have_names and n not in names:
                            names.append(n)
            except RuntimeError as e:
                notes.append(str(e))
        if not only:
            names += [s for s in seeds if s not in names]
        todo = [n for n in names if n.lower() not in have_names and (only or due(state, n, now))][:max_companies]
        for name in todo:
            rec = {"checked": now.isoformat(), "status": "none", "detail": ""}
            try:
                boards, trail = find_boards(name, lambda q: budget.search(q, search), fetch, ask)
                rec["detail"] = "; ".join(trail[-3:])[:300]
                for source, slug in boards:
                    if (source, slug.lower()) in have_slugs:
                        rec.update(status="added", detail=f"already watched as {source}/{slug}")
                        break
                    try:
                        v = verify(source, slug)
                    except Exception as e:
                        rec["detail"] += f" | {source}/{slug}: {type(e).__name__}"
                        continue
                    if v["total"] > 0 and (v["india"] > 0 or source == "workday"):
                        cfg["companies"].append({"name": name, "source": source, "slug": slug})
                        have_slugs.add((source, slug.lower()))
                        save_json(cfg_path, cfg)
                        rec.update(status="added", detail=f"{source}/{slug}, {v['total']} jobs, {v['india']} in India")
                        added.append(f"{name}: added ({rec['detail']})")
                        break
                    rec["detail"] += f" | {source}/{slug}: {v['total']} jobs, none in India"
                if rec["status"] == "none":
                    notes.append(f"{name}: no supported job board found on its site (needs a custom scraper)")
            except RuntimeError as e:  # budget used up: stop cleanly and finish tomorrow
                notes.append(str(e))
                break
            except Exception as e:
                rec.update(status="error", detail=f"{type(e).__name__}: {e}"[:200])
                notes.append(f"{name}: error {rec['detail']}")
            state.setdefault("companies", {})[name.lower()] = rec
    finally:
        save_json(state_path, state)
    lines = added + notes
    if lines:
        with (data_dir / "pi_outbox.md").open("a", encoding="utf-8") as f:
            f.write(f"- [{now:%Y-%m-%d %H:%M} UTC] digger: " + "; ".join(lines) + "\n")
    with (data_dir / "pi_log.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now.isoformat(), "who": "digger", "event": "run", "checked": len(todo),
                            "added": len(added), "searches_today": state["searches"]["n"]}) + "\n")
    return added, notes


def main():
    ap = argparse.ArgumentParser(description="find job boards the company list does not know")
    ap.add_argument("data_dir", nargs="?", default=str(HERE / "data"))
    ap.add_argument("--discover", action="store_true", help="also ask the model to name companies from search results")
    ap.add_argument("--only", default="", help="comma-separated company names to check now")
    ap.add_argument("--max", type=int, default=12)
    a = ap.parse_args()
    seeds_file = HERE / "digger_seed.txt"
    seeds = [l.strip() for l in seeds_file.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")] if seeds_file.exists() else []
    only = [n.strip() for n in a.only.split(",") if n.strip()] or None
    added, notes = run(Path(a.data_dir), HERE / "companies.json", seeds, discover=a.discover, only=only, max_companies=a.max)
    for line in added + notes:
        print(line)


if __name__ == "__main__":
    main()
