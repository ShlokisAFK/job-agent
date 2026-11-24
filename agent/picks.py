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


def digest(rows, today, extra_lines=()):
    """rows: tracker rows for today's picks. Markdown text for the phone notification."""
    out = [f"Top {len(rows)} jobs for {today}", ""]
    for r in rows:
        link = r["url"] or "(no link listed)"
        out.append(f"#{r['id']} {r['company']}: {r['title']} ({r['location']}, {show_date(r['posted'])})")
        out.append(f"   why: {r['why']}")
        if r.get("draft"):
            out.append(f"   draft ready in Gmail ({r['draft']})")
        out.append(f"   {link}")
    out.append("")
    out.append("Reply in my_updates.md, e.g.  3 sent   7 skip   2 replied - call Friday")
    out.extend(extra_lines)
    return "\n".join(out)
