"""Push the digest to your phone with ntfy (https://ntfy.sh). No account needed.

The topic name works like a password: anyone who knows it can read the topic,
so it is random and lives only in the gitignored config.json.
"""
import urllib.request

LIMIT = 3900  # ntfy messages are cut at 4096 bytes


def send(topic, title, body, server="https://ntfy.sh"):
    data = body.encode("utf-8")
    if len(data) > LIMIT:
        data = data[:LIMIT].decode("utf-8", "ignore").encode("utf-8") + b"\n... (full list in digest.md)"
    req = urllib.request.Request(f"{server}/{topic}", data=data, method="POST",
                                 headers={"Title": title.encode("ascii", "ignore").decode(), "Tags": "briefcase"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status
