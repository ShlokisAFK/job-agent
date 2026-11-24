# job-agent

A small multi-agent system that finds internship and early-career roles for me, picks the ones that
actually fit my background, drafts outreach emails and pings my phone with a daily shortlist.

Three agents, three machines' worth of jobs, one human in charge:

```mermaid
flowchart LR
    subgraph Pi["Raspberry Pi 5 (always on)"]
        F[collector<br/>fetch_jobs.py, cron every 6h]
    end
    subgraph Laptop["Laptop (supervisor)"]
        S[supervisor.py<br/>every 30 min]
        C[Claude Code<br/>headless, 10 min max]
        O[outreach tool<br/>Gmail drafts only]
    end
    B[(Greenhouse / Lever / Ashby<br/>public job boards)] --> F
    F -- backlog.jsonl, pi_outbox.md --> S
    S -- candidates.md, state.md --> C
    C -- picks.json, state.md, schedule.md --> S
    S --> O
    S -- digest --> P[my phone<br/>ntfy]
    P -. "3 sent / 7 skip" .-> S
    S -- pi_inbox.md --> F
```

| Agent | Runs on | Job |
|---|---|---|
| **Collector** | Raspberry Pi | Pulls public job-board APIs every 6 hours, keeps new India / remote roles. Keeps working while the laptop is off. |
| **Supervisor** | Laptop | Syncs with the Pi, merges my updates, decides when to wake Claude, checks Claude's output, updates the tracker, drafts emails, sends the digest. |
| **Reviewer (Claude)** | Laptop, headless | Reads my master resume and the shortlist, picks at most 10 real fits, writes a one-line reason and an email hook, keeps its own notes. |
| **Me** | Phone | Reads the digest, sends drafts myself, replies `3 sent`, `7 skip`, `2 replied - call Friday`. |

## Design choices
- **Files are the memory.** Claude starts every session with no memory. `state.md` (its own notes, under 60 lines),
  `chat.md`, `schedule.md` and `log.jsonl` carry everything between runs, so each wake-up reads about a page.
  Every file has one writer, so two machines never corrupt the same file.
- **Claude only judges; data comes from the source.** Claude returns job *keys*, a reason and a hook. Links, titles,
  locations and dates in the digest are filled from the job-board record, so they cannot be made up.
  A job with no date shows "date not listed". A contact email is kept only if it is written in the posting.
- **APIs before scraping.** Greenhouse, Lever and Ashby publish job boards as JSON, with real timestamps and no
  layout breakage. Scrapers are for later, one company at a time, after checking its terms.
- **Cheap first, smart second.** A keyword ranker cuts hundreds of jobs to 30; Claude reviews only those.
  Claude is woken once a day (or early if the Pi flags a problem with `@claude`), capped per day.
- **Nothing is sent automatically.** Drafts go to Gmail through a draft-only tool; I press Send.

## Safety rails
- `workspace/STOP`: if this file exists, nothing runs.
- Claude runs with `Read`, `Write`, `Edit` only: no shell, no web, no subagents, inside the workspace folder.
- Hard timeout (10 min) and a daily wake cap (`max_wakes_per_day`).
- Everything is logged to `workspace/log.jsonl`.
- Claude is told to use only facts marked *Verified* in my resume audit.

## Run it
```
python -m supervisor.supervisor --demo     # sample data, fake reviewer, touches nothing else
python -m unittest discover -s tests       # 13 tests
```
Real setup: copy `config.example.json` to `config.json` (gitignored), copy `pi/` to the Pi and add a cron line:
```
15 */6 * * * cd ~/job-agent && python3 fetch_jobs.py ~/job-agent/data >> ~/job-agent/data/cron.log 2>&1
```
then schedule the supervisor on the laptop every 30 minutes:
```
schtasks /create /tn job-agent /sc minute /mo 30 /tr "<repo>un_supervisor.cmd"
```
Subscribe to your `ntfy_topic` in the ntfy app to get the digest.

## Layout
```
pi/fetch_jobs.py          collector (stdlib only)
pi/companies.json         boards to watch
agent/rank.py             keyword pre-ranking
agent/picks.py            validates Claude's picks, builds the digest from real data
agent/tracker.py          one tracker: pipeline columns + my columns
agent/notify.py           ntfy push
supervisor/supervisor.py  the orchestrator
supervisor/claude_task.md the reviewer's instructions
demo/                     sample data for --demo
```

## Next
- A small local model on the Pi to read `pi_inbox.md` and act on the reviewer's requests (add a board, tune filters).
- More boards, and per-company scrapers for companies without a public API.
- Reply-rate stats per company and hook style, once there is a month of data.
