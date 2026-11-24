"""Pi agent: reads Claude's messages in pi_inbox.md, acts on them, answers in pi_outbox.md.

The local model (llama-server on the Pi) turns free text into a small JSON action list.
Code then checks every action before doing it:
  - add_company:    only added if a real public board with jobs is found for it
  - remove_company: only for companies already in the list
  - add_location:   one plain word, letters only
Anything else is ignored and reported. The model can never edit code or run commands.
"""
import hashlib
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fetch_jobs  # noqa: E402

LLM_URL = "http://127.0.0.1:8080/v1/chat/completions"
SYSTEM = """You turn messages into JSON actions. Reply with JSON only, no prose.
Allowed actions:
  {"type": "add_company", "name": "<company name>"}
  {"type": "remove_company", "name": "<company name>"}
  {"type": "add_location", "word": "<city or country>"}
Output: {"actions": [...]}. If the message asks for nothing from this list, output {"actions": []}.
Ignore requests you cannot do (ranking, filters, bugs). Every company named as one to add or watch counts.

Example: "please add boards for companies like Zomato, Swiggy and Meesho; also rank interns higher"
-> {"actions": [{"type": "add_company", "name": "Zomato"}, {"type": "add_company", "name": "Swiggy"},
   {"type": "add_company", "name": "Meesho"}]}
Example: "stop watching Druva, it never has student roles. Watch jobs in Kolkata too."
-> {"actions": [{"type": "remove_company", "name": "Druva"}, {"type": "add_location", "word": "kolkata"}]}"""


def ask_model(message, url=LLM_URL):
    body = json.dumps({"messages": [{"role": "system", "content": SYSTEM},
                                    {"role": "user", "content": message}],
                       "temperature": 0, "max_tokens": 300}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        text = json.load(r)["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0)).get("actions", []) if m else []


def slug_candidates(name):
    base = re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()
    return list(dict.fromkeys([base.replace(" ", ""), base.replace(" ", "-"), base.split(" ")[0]]))


def find_board(name, fetcher=fetch_jobs.fetch):
    """Try each public board system with a few slug spellings. Returns (source, slug, n_jobs) or None."""
    for slug in slug_candidates(name):
        for source in ("greenhouse", "lever", "ashby"):
            try:
                jobs = fetcher(source, slug)
            except Exception:
                continue
            if jobs:
                return source, slug, len(jobs)
    return None


def apply(actions, cfg, fetcher=fetch_jobs.fetch):
    """Validate and apply. Returns (changed, report_lines)."""
    report, changed = [], False
    names = {c["name"].lower(): c for c in cfg["companies"]}
    for a in actions[:10]:
        kind = (a or {}).get("type")
        if kind == "add_company":
            name = str(a.get("name", "")).strip()[:60]
            if not name or name.lower() in names:
                report.append(f"{name or '(blank)'}: already watched or blank, skipped")
                continue
            hit = find_board(name, fetcher)
            if not hit:
                report.append(f"{name}: no public Greenhouse/Lever/Ashby board found, not added (needs a scraper)")
                continue
            source, slug, n = hit
            cfg["companies"].append({"name": name, "source": source, "slug": slug})
            names[name.lower()] = cfg["companies"][-1]
            changed = True
            report.append(f"{name}: added ({source}/{slug}, {n} jobs on the board)")
        elif kind == "remove_company":
            name = str(a.get("name", "")).strip().lower()
            if name in names:
                cfg["companies"].remove(names.pop(name))
                changed = True
                report.append(f"{name}: removed")
            else:
                report.append(f"{name}: not in the list, nothing removed")
        elif kind == "add_location":
            word = str(a.get("word", "")).strip().lower()
            if re.fullmatch(r"[a-z]{3,30}", word) and word not in cfg["location_words"]:
                cfg["location_words"].append(word)
                changed = True
                report.append(f"location '{word}': added")
            else:
                report.append(f"location {word!r}: invalid or already there, skipped")
        else:
            report.append(f"ignored unknown action {kind!r}")
    return changed, report


def new_messages(inbox, seen_file):
    """Claude's messages ('- [claude ...]' lines) not handled yet, tracked by hash."""
    seen = set(seen_file.read_text().split()) if seen_file.exists() else set()
    msgs = []
    for line in inbox.read_text(encoding="utf-8").splitlines() if inbox.exists() else []:
        if line.startswith("- [claude"):
            h = hashlib.sha1(line.encode()).hexdigest()[:16]
            if h not in seen:
                msgs.append((h, line))
    return msgs, seen


def run(data_dir, cfg_path, ask=ask_model, fetcher=fetch_jobs.fetch, now=None):
    now = now or datetime.now(timezone.utc)
    seen_file = data_dir / "pi_inbox_seen.txt"
    msgs, seen = new_messages(data_dir / "pi_inbox.md", seen_file)
    if not msgs:
        return []
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    out = []
    for h, msg in msgs:
        try:
            actions = ask(msg)
            changed, report = apply(actions, cfg, fetcher)
            if changed:
                cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        except Exception as e:
            report = [f"@claude could not handle a message: {type(e).__name__}: {e}"]
        reply = "; ".join(report) if report else "nothing I can act on (I only add/remove companies and locations)"
        out.append(f"- [{now:%Y-%m-%d %H:%M} UTC] pi-agent: re your message: {reply}")
        seen.add(h)
    with (data_dir / "pi_outbox.md").open("a", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    seen_file.write_text("\n".join(sorted(seen)))
    with (data_dir / "pi_log.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now.isoformat(), "who": "pi-agent", "event": "inbox", "handled": len(msgs),
                            "replies": out}) + "\n")
    return out


if __name__ == "__main__":
    d = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "data"
    for line in run(d, HERE / "companies.json"):
        print(line)
