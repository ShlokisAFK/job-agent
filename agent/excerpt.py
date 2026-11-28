"""Cut a long posting down for the reviewer without losing the requirements.

The first characters of a posting are usually about the company; degree, batch year, CGPA and
experience rules sit further down. So keep the start (what the role is) plus the stretch that
mentions the most requirement terms.
"""
import re

SIGNALS = re.compile(r"qualifications?|requirements?|eligib\w*|\bdegree\b|bachelor|master'?s|b\.?\s?tech|"
                     r"\b[cs]?gpa\b|graduat\w*|\bbatch\b|\byears?\b[^.]{0,30}?experience|experience[^.]{0,20}?\byears?\b|"
                     r"who you are|what you(?:'|’)?ll need|what you need|what we(?:'|’)?re looking for|"
                     r"must[- ]have|about you|currently (?:pursuing|enrolled)", re.I)
GAP = " [...] "
LEAD = 150  # characters kept in front of the first requirement term, so its heading is not cut


def excerpt(text, limit):
    """At most `limit` characters: the start of the posting and its densest requirements stretch."""
    if len(text) <= limit:
        return text
    head = limit * 35 // 100
    room = limit - head - len(GAP)
    found = [(m.start(), m.group(0).lower()) for m in SIGNALS.finditer(text) if m.start() >= head]
    if not found:
        return text[:head] + GAP + text[-room:]

    def score(start):  # different terms count first, then how often they appear
        inside = [word for pos, word in found if start <= pos < start + room]
        return len(set(inside)), len(inside)

    starts = [max(head, min(pos - LEAD, len(text) - room)) for pos, _ in found]
    best = max(starts, key=score)  # ties go to the earliest stretch
    if best == head:
        return text[:limit]  # the requirements follow straight on: no gap needed
    return text[:head] + GAP + text[best:best + room]
