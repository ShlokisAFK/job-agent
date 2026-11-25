"""Cheap pre-ranking so Claude only reads the most promising jobs, not hundreds.

Plain keyword scoring against a profile built from master_resume.md.
It is deliberately simple: Claude makes the real judgment on the shortlist.
"""
import re

from . import eligibility

POSITIVE = {  # term -> weight; matched in title (x3) and description (x1)
    "intern": 6, "internship": 6, "graduate": 4, "new grad": 5, "fresher": 5, "entry level": 4, "university": 3,
    "machine learning": 4, "ml ": 3, "deep learning": 4, "ai ": 2, "llm": 3, "nlp": 2, "computer vision": 3,
    "data scien": 3, "data engineer": 2, "python": 2, "pytorch": 3, "xgboost": 3, "fastapi": 2, "docker": 1,
    "security": 2, "malware": 4, "threat": 2, "research": 2, "software engineer": 5, "backend": 3, "sde": 4,
    "ai engineer": 6, "ml engineer": 6, "machine learning engineer": 6, "applied ": 3, "full stack": 2, "software developer": 4,
}
NEGATIVE = {  # term -> penalty; matched in title only. Seniority and non-engineering titles are big enough to drop a job.
    "senior": 60, "sr.": 60, " sr ": 60, "staff": 60, "principal": 60, "lead": 40, "manager": 40, "director": 60,
    "head of": 60, "vp": 60, "architect": 40, " ii": 40, " iii": 60, " iv": 60,
    "sde2": 40, "sde 2": 40, "sde-2": 40, "sde3": 60, "sde 3": 60, "sde-3": 60,
    "product manage": 40, "user research": 40, "program": 25, "designer": 30,
    "phd": 60, "postdoc": 60, "payroll": 30, "procurement": 30, "consultant": 20, "administrator": 30, "operations": 20, " sap ": 25,
    "sales": 25, "account executive": 25, "marketing": 20, "brand": 15, "recruit": 25, "talent": 25,
    "legal": 25, "finance": 20, "accountant": 25, "hr ": 25, "collections": 25, "customer success": 20,
    "gtm": 20, "strategy": 10,
}
YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:years|yrs)", re.I)


def score(job):
    title = " " + job.get("title", "").lower() + " "
    desc = " " + job.get("description", "").lower() + " "
    s = 0
    for term, w in POSITIVE.items():
        if term in title:
            s += 3 * w
        elif term in desc:
            s += w
    for term, w in NEGATIVE.items():
        if term in title:
            s -= w
    yrs = [int(y) for y in YEARS.findall(desc)]
    if yrs:  # experience asked for: 3+ years is not an entry-level role, so it drops out
        if min(yrs) >= 3:
            s -= 40
        elif max(yrs) >= 5:
            s -= 25
        elif min(yrs) >= 2:
            s -= 12
    return s


def shortlist(jobs, exclude_keys, n=30, profile=None, verdicts=None):
    """Top n by score. Jobs whose posting rules you out (batch year, CGPA) never reach Claude."""
    profile = profile or {}
    verdicts = verdicts or {}  # the Pi model's title check: only an explicit "no" drops a job
    scored = [(score(j), j) for j in jobs
              if j["key"] not in exclude_keys and verdicts.get(j["key"]) != "no" and not eligibility.check(j, profile)]
    scored = [(s, j) for s, j in scored if s > 0]  # negative = clearly not a student role
    scored.sort(key=lambda sj: (sj[0], sj[1].get("posted", "")), reverse=True)
    return scored[:n]
