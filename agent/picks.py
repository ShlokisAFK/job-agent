"""Check Claude's picks against the real job data, then build the digest from that data.

Claude writes only judgment (why, hook, optional contact). Every link, date,
title and location in the digest comes from the job board record, so nothing
in the digest can be made up.
"""
import json
import re

MAX_PICKS = 10
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)


def validate(raw_text, candidates):
    """Returns (good_picks, problems). candidates: dict key -> job."""
    problems = []
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        return [], [f"picks.json is not valid JSON: {e}"]
    picks = data.get("picks", []) if isinstance(data, dict) else []
    good, seen = [], set()
    for p in picks:
        key = (p or {}).get("key", "")
        if key not in candidates:
            problems.append(f"unknown job key {key!r} (not in today's shortlist), dropped")
            continue
        if key in seen:
            continue
        hook = (p.get("hook") or "").strip()
        if not hook:
            problems.append(f"{key}: empty hook, dropped")
            continue
        email = (p.get("contact_email") or "").strip()
        if email and (not EMAIL.match(email) or email.lower() not in candidates[key]["description"].lower()):
            problems.append(f"{key}: email {email!r} is not written in the posting, so it was removed")
            email = ""
        seen.add(key)
        good.append({"key": key, "why": (p.get("why") or "").strip(), "hook": hook,
                     "contact_name": (p.get("contact_name") or "").strip() if email else "",
                     "contact_email": email})
        if len(good) == MAX_PICKS:
            break
    return good, problems


def show_date(posted):
    return f"posted {posted[:10]}" if posted else "date not listed"


def follow_up_lines(due):
    """due: [(tracker row, days waiting)] from tracker.follow_ups."""
    if not due:
        return []
    out = ["Follow up (no answer yet):"]
    out += [f"#{r['id']} {r['company']}: {r['title']} ({r['status']} {days} days ago)" for r, days in due]
    out.append(f"After you follow up, reply  {due[0][0]['id']} followed")
    return out


REPLY_HELP = "Reply from your phone or in my_updates.md, one per line, e.g. 3 sent / 7 skip / 2 replied - call Friday"


def _headline(r):
    return f"{r['title']} ({r['location']}, {show_date(r['posted'])})"


def _details(r):
    out = [f"why: {r['why']}"]
    if r.get("draft"):
        out.append(f"draft ready in Gmail ({r['draft']})")
    out.append(r["url"] or "(no link listed)")
    return out


def digest(rows, today, extra_lines=()):
    """rows: tracker rows for today's picks. The full text, saved as digest.md."""
    out = [f"Top {len(rows)} jobs for {today}", ""]
    for r in rows:
        out.append(f"#{r['id']} {r['company']}: {_headline(r)}")
        out += [f"   {line}" for line in _details(r)]
    out.append("")
    out.append(REPLY_HELP)
    out.extend(extra_lines)
    return "\n".join(out)


def messages(rows, today):
    """Phone notifications as [(title, body)]: a short summary, then one per pick.

    A phone notification shows only the start of a long message, so one message
    with every pick looks like it holds just the first one.
    """
    summary = [f"{len(rows)} jobs for {today}, details follow"]
    for r in rows:
        line = f"#{r['id']} {r['company']}: {r['title']}"
        summary.append(line if len(line) <= 70 else line[:67] + "...")
    summary += ["", REPLY_HELP]
    out = [(f"job-agent: {len(rows)} picks", "\n".join(summary))]
    out += [(f"#{r['id']} {r['company']}", "\n".join([_headline(r), *_details(r)])) for r in rows]
    return out
