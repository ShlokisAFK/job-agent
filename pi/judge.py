"""Pi judge: the local model's wide first filter on job titles.

It answers one question per title, "could this be an engineering / data role at student or entry level?".
The laptop drops only an explicit "no"; Claude still judges everything else on the full posting.
Titles only, on purpose: on full postings the 4B model passed senior roles and invented reasons.

Anything unclear (model down, odd answer) gets no verdict, so the job stays in and is retried later.

Writes (one writer per file):
  data/verdicts.jsonl   {"key", "v": "yes"|"no", "title"}, appended
  data/pi_log.jsonl     one event per run

Run:  python3 judge.py [data_dir] [--limit 600]
"""
import argparse
import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
LLM_URL = "http://127.0.0.1:8080/v1/chat/completions"
MAX_SECONDS = 20 * 60  # the rest waits for the next run

# The exact prompt that scored 44/45 on labelled titles (llm/bench.py). Re-run the benchmark if you change it.
SYSTEM = """You screen job titles for a 3rd-year computer science student (graduating 2028) who wants
software / machine learning / AI / data / security ENGINEERING roles.
Answer "yes" only if BOTH are true:
1. The role is engineering or data/ML work (writing code or models). Marketing, sales, design, product
   management, program management, user research, writing, HR, finance, operations, support are NOT.
2. The level is intern, trainee, new grad, entry level, SDE 1, associate, or a plain engineer title with no
   seniority. Senior, Sr, Staff, Principal, Lead, Manager, Director, Architect, II, III, SDE2+ are NOT.
Reply with one word: yes or no."""


def ask_model(title, url=LLM_URL):
    body = json.dumps({"messages": [{"role": "system", "content": SYSTEM},
                                    {"role": "user", "content": "Title: " + title}],
                       "temperature": 0, "max_tokens": 8,
                       "chat_template_kwargs": {"enable_thinking": False}}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)["choices"][0]["message"]["content"]


def read_jsonl(path):
    out = []
    for line in path.read_text(encoding="utf-8").splitlines() if path.exists() else []:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:  # a half-written last line must not stop the run
            continue
    return out


def parse(answer):
    word = answer.strip().lower().rstrip(".!")
    return word if word in ("yes", "no") else ""


def run(data_dir, ask=ask_model, limit=600, clock=time.monotonic, now=None):
    now = now or datetime.now(timezone.utc)
    out_path = data_dir / "verdicts.jsonl"
    done = read_jsonl(out_path)
    judged_keys = {r["key"] for r in done}
    by_title = {r["title"].lower(): r["v"] for r in done if r.get("title")}
    todo = [j for j in read_jsonl(data_dir / "backlog.jsonl") if j["key"] not in judged_keys]
    todo.sort(key=lambda j: j.get("fetched", ""), reverse=True)
    result = {"judged": 0, "no": 0, "unclear": 0, "left": 0, "error": ""}
    start = clock()
    with out_path.open("a", encoding="utf-8") as f:
        for job in todo[:limit]:
            if clock() - start > MAX_SECONDS:
                break
            title = " ".join(job.get("title", "").split())[:200]
            v = by_title.get(title.lower())
            if not v:
                try:
                    v = parse(ask(title))
                except Exception as e:  # model down: stop here, try again next run
                    result["error"] = f"{type(e).__name__}: {e}"[:200]
                    break
                if not v:
                    result["unclear"] += 1
                    continue
                by_title[title.lower()] = v
            f.write(json.dumps({"key": job["key"], "v": v, "title": title}, ensure_ascii=False) + "\n")
            f.flush()
            result["judged"] += 1
            result["no"] += v == "no"
    result["left"] = len(todo) - result["judged"]
    with (data_dir / "pi_log.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now.isoformat(), "who": "judge", "event": "run", **result}) + "\n")
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="title-level job filter with the local model")
    ap.add_argument("data_dir", nargs="?", default=str(HERE / "data"))
    ap.add_argument("--limit", type=int, default=600)
    a = ap.parse_args()
    print(run(Path(a.data_dir), limit=a.limit))
