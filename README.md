# job-agent

A small multi-agent system that finds internship and early-career roles for me, picks the ones that
actually fit my background, drafts outreach emails and pings my phone with a daily shortlist.

Three agents on two machines, talking through files, with one human in charge:

```mermaid
flowchart LR
    subgraph Pi["Raspberry Pi 5 (always on)"]
        F[collector<br/>fetch_jobs.py, cron every 6h]
        A[Pi agent<br/>local Qwen2 model, every 30 min]
        A -- companies.json --> F
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
    S -- pi_inbox.md --> A
    A -- pi_outbox.md --> S
```

| Agent | Runs on | Job |
|---|---|---|
| **Collector** | Raspberry Pi | Pulls public job-board APIs every 6 hours, keeps new India / remote roles. Keeps working while the laptop is off. |
| **Pi agent** | Raspberry Pi, local LLM | Reads Claude's requests ("add Meesho, drop Druva"), turns them into actions with a small local model (Qwen2 via llama-server), and replies. Code checks every action: a company is added only if a real public board with jobs exists. It can never touch code. |
| **Supervisor** | Laptop | Syncs with the Pi, merges my updates, decides when to wake Claude, checks Claude's output, updates the tracker, drafts emails, sends the digest. |
| **Reviewer (Claude)** | Laptop, headless | Reads my master resume and the shortlist, picks at most 10 real fits, writes a one-line reason and an email hook, keeps its own notes. |
| **Me** | Phone | Reads the digest, sends drafts myself, replies `3 sent`, `7 skip`, `2 replied - call Friday`. |

## Design choices
- **Small reads per wake.** Claude reads a 3 KB candidate profile (not the full resume) and a shortlist of 20 jobs,
  2,000 characters each, so each wake reads roughly 40 KB.
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
- **Hard rules in code, judgment in models.** Eligibility lines like "CGPA 8 and above, 2027 graduates only" are
  caught by code anywhere in the posting, before Claude sees the job. The models decide; code checks.
- **Nothing is sent automatically.** Drafts go to Gmail through a draft-only tool; I press Send.

## Safety rails
- `workspace/STOP`: if this file exists, nothing runs.
- Claude runs with `Read`, `Write`, `Edit` only: no shell, no web, no subagents, inside the workspace folder.
- Hard timeout (10 min) and a daily wake cap (`max_wakes_per_day`, never more than 4). A run with 0 picks still sends a phone message with the reasons and retries on the next unreviewed jobs 3 hours later (`retry_gap_hours`, not after `retry_until`).
- Everything is logged to `workspace/log.jsonl`.
- Claude is told to use only facts marked *Verified* in my resume audit.

## Run it
```
python -m supervisor.supervisor --demo     # sample data, fake reviewer, touches nothing else
python -m unittest discover -s tests       # 18 tests
```
Real setup: copy `config.example.json` to `config.json` (gitignored; set your `profile` there), copy `pi/` to the Pi and add a cron line:
```
15 */6 * * * cd ~/job-agent && python3 fetch_jobs.py ~/job-agent/data >> ~/job-agent/data/cron.log 2>&1
*/30 * * * * cd ~/job-agent && python3 pi_agent.py ~/job-agent/data >> ~/job-agent/data/cron.log 2>&1
```
then schedule the supervisor on the laptop every 30 minutes:
```
schtasks /create /tn job-agent /sc minute /mo 30 /tr "<repo>
un_supervisor.cmd"
```
Subscribe to your `ntfy_topic` in the ntfy app to get the digest.

## Layout
```
pi/fetch_jobs.py          collector (stdlib only)
pi/pi_agent.py            Pi agent: local model + validated actions
pi/companies.json         starting list of boards (the Pi agent edits its own copy on the Pi)
agent/eligibility.py      hard eligibility rules (batch year, CGPA)
agent/rank.py             keyword pre-ranking
agent/picks.py            validates Claude's picks, builds the digest from real data
agent/tracker.py          one tracker: pipeline columns + my columns
agent/notify.py           ntfy push
supervisor/supervisor.py  the orchestrator
supervisor/claude_task.md the reviewer's instructions
demo/                     sample data for --demo
```

## Next
- More boards, and per-company scrapers for companies without a public API.
- Reply-rate stats per company and hook style, once there is a month of data.
