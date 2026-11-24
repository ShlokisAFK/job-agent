"""Hard eligibility checks done in code, before Claude sees a job.

Catches lines like "CGPA 8 and above - 2027 graduates only" anywhere in the posting.
Only rules the posting states explicitly; if it says nothing, the job stays in.
"""
import re

YEAR_AFTER = re.compile(r"\b(?:graduating|graduation|batch|class of|passing out|pass[- ]out)\s*(?:year)?\s*(?:in|of|:|-)?\s*"
                        r"(20\d\d)(?:\s*(?:/|-|or|and|&)\s*(20)?(\d\d))?", re.I)
YEAR_BEFORE = re.compile(r"\b(20\d\d)(?:\s*(?:/|-|or|and|&)\s*(20)?(\d\d))?\s*(?:graduates|grads|batch|pass[- ]?outs?)\b", re.I)
CGPA_AFTER = re.compile(r"\b(?:cgpa|gpa)\s*(?:of|:|-|>=|≥|>)?\s*(?:of\s*)?(\d{1,2}(?:\.\d+)?)", re.I)
CGPA_BEFORE = re.compile(r"\b(\d{1,2}(?:\.\d+)?)\s*\+?\s*(?:cgpa|gpa)\b", re.I)


def _years(m):
    first = int(m.group(1))
    ys = {first}
    if m.group(3):
        ys.add(int(f"20{m.group(3)}"))
    return ys


def check(job, profile):
    """Returns '' if eligible (or unknown), else a short reason."""
    text = f"{job.get('title', '')} {job.get('description', '')}"
    years = set()
    for rx in (YEAR_AFTER, YEAR_BEFORE):
        for m in rx.finditer(text):
            years |= _years(m)
    gy = profile.get("grad_year")
    if gy and years and gy not in years:
        return f"only for {'/'.join(map(str, sorted(years)))} graduates (you: {gy})"
    cg = profile.get("cgpa")
    if cg:
        mins = [float(m.group(1)) for rx in (CGPA_AFTER, CGPA_BEFORE) for m in rx.finditer(text)]
        mins = [v for v in mins if 5 <= v <= 10]
        if mins and min(mins) > cg:
            return f"needs CGPA {min(mins):g}+ (you: {cg:g})"
    return ""
