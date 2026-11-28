"""Push the digest to your phone with ntfy (https://ntfy.sh). No account needed.

The topic name works like a password: anyone who knows it can read the topic,
so it is random and lives only in the gitignored config.json.
"""
import json
import urllib.parse
import urllib.request

LIMIT = 3900  # ntfy messages are cut at 4096 bytes


def send(topic, title, body, server="https://ntfy.sh", click=""):
    """click: web address that tapping the notification opens."""
    data = body.encode("utf-8")
    if len(data) > LIMIT:
        data = data[:LIMIT].decode("utf-8", "ignore").encode("utf-8") + b"\n... (full list in digest.md)"
    headers = {"Title": title.encode("ascii", "ignore").decode(), "Tags": "briefcase"}
    if click.startswith(("https://", "http://")):  # headers must be ASCII, so other characters are percent-encoded
        headers["Click"] = urllib.parse.quote(click, safe=":/?#[]@!$&'()*+,;=%")
    req = urllib.request.Request(f"{server}/{topic}", data=data, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status


def _get_text(url):
    with urllib.request.urlopen(url, timeout=20) as r:
        return r.read(500_000).decode("utf-8", "replace")


def poll(topic, server="https://ntfy.sh", get=_get_text):
    """Messages still cached on the topic (ntfy.sh keeps them about 12 hours). Returns [(id, text)]."""
    out = []
    for line in get(f"{server}/{topic}/json?poll=1&since=all").splitlines():
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            continue
        if m.get("event") == "message":
            out.append((m.get("id", ""), m.get("message", "")))
    return out
