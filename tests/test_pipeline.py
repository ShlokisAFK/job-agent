import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pi"))
import fetch_jobs  # noqa: E402
import pi_agent  # noqa: E402
from agent import eligibility, picks, rank, tracker  # noqa: E402
from supervisor.supervisor import Supervisor  # noqa: E402


def job(key, title="ML Intern", desc="python pytorch", posted="2026-11-20T10:00:00Z", loc="Bengaluru"):
    return {"key": key, "company": "x", "company_name": "X", "title": title, "location": loc,
            "url": f"https://x/{key}", "posted": posted, "updated": "", "description": desc}


class FetchTests(unittest.TestCase):
    def test_normalize_keeps_real_fields_and_blank_dates(self):
        g = fetch_jobs.normalize("greenhouse", "acme", {"id": 1, "title": "T", "absolute_url": "u",
                                                        "location": {"name": "Pune"}, "content": "&lt;p&gt;Hi&lt;/p&gt;"})
        self.assertEqual((g["key"], g["url"], g["posted"], g["description"]), ("gh:acme:1", "u", "", "Hi"))
        lv = fetch_jobs.normalize("lever", "acme", {"id": "a", "text": "T", "createdAt": 0, "hostedUrl": "u"})
        self.assertEqual(lv["posted"], "")  # missing/zero date stays empty, never invented

    def test_location_filter(self):
        words = ["india", "bengaluru", "remote"]
        self.assertTrue(fetch_jobs.location_ok("Bengaluru, India", words))
        self.assertTrue(fetch_jobs.location_ok("Remote", words))
        self.assertFalse(fetch_jobs.location_ok("Remote - Germany", words))
        self.assertFalse(fetch_jobs.location_ok("Paris", words))

    def test_dedupe_and_one_bad_board(self):
        cfg = {"location_words": ["pune"], "companies": [{"source": "greenhouse", "slug": "a"},
                                                         {"source": "greenhouse", "slug": "broken"}]}

        def fake(source, slug):
            if slug == "broken":
                raise OSError("down")
            return [job("gh:a:1", loc="Pune"), job("gh:a:2", loc="Paris")]
        with tempfile.TemporaryDirectory() as d:
            new, errors = fetch_jobs.run(cfg, Path(d), fetcher=fake)
            self.assertEqual([j["key"] for j in new], ["gh:a:1"])
            self.assertEqual(len(errors), 1)
            again, _ = fetch_jobs.run(cfg, Path(d), fetcher=fake)
            self.assertEqual(again, [])
            self.assertIn("@claude", (Path(d) / "pi_outbox.md").read_text())


class RankTests(unittest.TestCase):
    def test_students_above_seniors_and_negatives_dropped(self):
        jobs = [job("a", "Senior Staff ML Engineer"), job("b", "Machine Learning Intern"),
                job("c", "Sales Director", "sales")]
        keys = [j["key"] for _, j in rank.shortlist(jobs, set())]
        self.assertEqual(keys, ["b"])

    def test_excluded_keys_skipped(self):
        self.assertEqual(rank.shortlist([job("a")], {"a"}), [])


class PicksTests(unittest.TestCase):
    cands = {"a": job("a", desc="mail jobs@acme.com"), "b": job("b")}

    def test_unknown_key_and_invented_email_dropped(self):
        raw = json.dumps({"picks": [{"key": "zzz", "hook": "h"},
                                    {"key": "a", "hook": "h", "contact_email": "jobs@acme.com"},
                                    {"key": "b", "hook": "h", "contact_email": "guess@b.com"}]})
        good, problems = picks.validate(raw, self.cands)
        self.assertEqual([g["key"] for g in good], ["a", "b"])
        self.assertEqual(good[0]["contact_email"], "jobs@acme.com")
        self.assertEqual(good[1]["contact_email"], "")
        self.assertEqual(len(problems), 2)

    def test_bad_json_and_cap(self):
        self.assertEqual(picks.validate("not json", self.cands)[0], [])
        many = {f"k{i}": job(f"k{i}") for i in range(15)}
        raw = json.dumps({"picks": [{"key": k, "hook": "h"} for k in many]})
        self.assertEqual(len(picks.validate(raw, many)[0]), 10)

    def test_digest_uses_real_dates(self):
        rows = [{"id": "1", "company": "X", "title": "T", "location": "Pune", "posted": "", "why": "w",
                 "url": "u", "draft": ""}]
        self.assertIn("date not listed", picks.digest(rows, "2026-11-24"))


class TrackerTests(unittest.TestCase):
    def test_parse_and_apply(self):
        ups, bad = tracker.parse_updates("# header\n3 sent\n7 SKIP\n2 replied - call Friday\nhello\n9 maybe")
        self.assertEqual(ups, [("3", "sent", ""), ("7", "skip", ""), ("2", "replied", "call Friday")])
        self.assertEqual(bad, ["hello", "9 maybe"])
        rows = [{"id": "2", "status": "", "notes": ""}, {"id": "3", "status": "", "notes": ""}]
        done, unknown = tracker.apply_updates(rows, ups, "2026-11-24")
        self.assertEqual(rows[0]["status"], "replied")
        self.assertEqual(rows[0]["notes"], "call Friday")
        self.assertEqual(unknown, ["7"])


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        (self.ws / "inbox").mkdir()
        (self.ws / "inbox" / "backlog.jsonl").write_text(json.dumps(job("a")) + "\n" + json.dumps(job("b")) + "\n")
        resume = self.ws / "resume.md"
        resume.write_text("resume")
        self.cfg = {"demo": True, "master_resume": str(resume), "max_wakes_per_day": 1}
        self.calls = 0

    def tearDown(self):
        self.tmp.cleanup()

    def fake_claude(self, _prompt):
        self.calls += 1
        (self.ws / "picks.json").write_text(json.dumps({"picks": [{"key": "a", "why": "fits", "hook": "h"}]}))
        return 0, "", ""

    def sup(self, hour=10):
        return Supervisor(self.cfg, self.ws, now=datetime(2026, 11, 24, hour, 0), runner=self.fake_claude)

    def test_stop_file_blocks_everything(self):
        (self.ws / "STOP").write_text("")
        self.assertEqual(self.sup().run(force=True), "stopped")
        self.assertEqual(self.calls, 0)

    def test_not_due_before_daily_time(self):
        self.assertEqual(self.sup(hour=7).run(), "not due")

    def test_wake_picks_and_daily_cap(self):
        self.assertEqual(self.sup().run(), "picked 1")
        rows = tracker.read(self.ws / "tracker.csv")
        self.assertEqual((rows[0]["id"], rows[0]["url"]), ("1", "https://x/a"))  # link from data, not Claude
        self.assertIn("next_wake: 2026-11-25T09:00", (self.ws / "schedule.md").read_text())
        self.assertEqual(self.sup().run(force=True), "cap")
        self.assertEqual(self.calls, 1)

    def test_reviewed_jobs_not_shown_again(self):
        self.sup().run()
        self.cfg["max_wakes_per_day"] = 5
        # a was picked, b was shown and not picked: neither comes back
        self.assertEqual(self.sup().run(force=True), "nothing new")
        self.assertEqual(self.calls, 1)


class ZeroPickRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        (self.ws / "inbox").mkdir()
        (self.ws / "inbox" / "backlog.jsonl").write_text(
            "".join(json.dumps(job(k)) + "\n" for k in "abcdefgh"))
        resume = self.ws / "resume.md"
        resume.write_text("resume")
        self.cfg = {"demo": True, "master_resume": str(resume), "max_wakes_per_day": 9, "shortlist_size": 1}
        self.calls = 0

    def tearDown(self):
        self.tmp.cleanup()

    def no_picks(self, _prompt):
        self.calls += 1
        (self.ws / "picks.json").write_text('{"picks": []}')
        with (self.ws / "chat.md").open("a") as f:
            f.write("- [claude] all senior roles\n")
        return 0, "", ""

    def run_at(self, hour, minute=15):
        return Supervisor(self.cfg, self.ws, now=datetime(2026, 11, 25, hour, minute), runner=self.no_picks).run()

    def test_zero_picks_retries_then_stops_at_hard_cap(self):
        self.assertEqual(self.run_at(9), "picked 0")
        self.assertIn("next_wake: 2026-11-25T12:15", (self.ws / "schedule.md").read_text())
        digest = (self.ws / "digest.md").read_text()
        self.assertIn("all senior roles", digest)
        self.assertIn("12:15", digest)
        self.assertEqual(self.run_at(11), "not due")
        for h in (12, 15, 18):
            self.assertEqual(self.run_at(h), "picked 0")
        self.assertEqual(self.calls, 4)  # config says 9, the hard cap is 4
        self.assertIn("next_wake: 2026-11-26T09:00", (self.ws / "schedule.md").read_text())
        self.assertIn("No more tries today", (self.ws / "digest.md").read_text())

    def test_no_retry_late_in_the_day(self):
        self.run_at(19)
        self.assertIn("next_wake: 2026-11-26T09:00", (self.ws / "schedule.md").read_text())


class EligibilityTests(unittest.TestCase):
    P = {"grad_year": 2028, "cgpa": 7.74}

    def check(self, text):
        return eligibility.check({"title": "", "description": text}, self.P)

    def test_rules_out(self):
        self.assertIn("2027", self.check("Minimum eligibility criteria - CGPA 8 and above - 2027 graduates only"))
        self.assertIn("CGPA", self.check("Need 8.5+ CGPA. 2028 batch."))

    def test_keeps(self):
        for t in ("2027/28 graduates welcome", "graduating in 2028", "7.5+ CGPA", "no rules at all",
                  "class of 2027 or 2028"):
            self.assertEqual(self.check(t), "", t)

    def test_shortlist_drops_ineligible(self):
        jobs = [job("a", desc="python CGPA 8 and above, 2027 graduates"), job("b")]
        keys = [j["key"] for _, j in rank.shortlist(jobs, set(), profile=self.P)]
        self.assertEqual(keys, ["b"])


class PiAgentTests(unittest.TestCase):
    def fake_fetch(self, source, slug):
        if (source, slug) == ("lever", "razorpay"):
            return [job("lv:razorpay:1")]
        raise OSError("404")

    def test_add_only_verified_boards(self):
        cfg = {"location_words": ["india"], "companies": [{"name": "Druva", "source": "greenhouse", "slug": "druva"}]}
        actions = [{"type": "add_company", "name": "Razorpay"}, {"type": "add_company", "name": "Nope Inc"},
                   {"type": "remove_company", "name": "Druva"}, {"type": "run_shell", "cmd": "rm -rf /"},
                   {"type": "add_location", "word": "pune; rm"}]
        changed, report = pi_agent.apply(actions, cfg, fetcher=self.fake_fetch)
        self.assertTrue(changed)
        self.assertEqual(cfg["companies"], [{"name": "Razorpay", "source": "lever", "slug": "razorpay"}])
        self.assertEqual(cfg["location_words"], ["india"])
        self.assertTrue(any("not added" in r for r in report))
        self.assertTrue(any("ignored unknown action" in r for r in report))

    def test_messages_handled_once(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "pi_inbox.md").write_text("- [claude 2026-11-24] add company Razorpay\nnot a message\n")
            cfg = d / "companies.json"
            cfg.write_text(json.dumps({"location_words": [], "companies": []}))
            ask = lambda m: [{"type": "add_company", "name": "Razorpay"}]
            out = pi_agent.run(d, cfg, ask=ask, fetcher=self.fake_fetch)
            self.assertEqual(len(out), 1)
            self.assertIn("added", out[0])
            self.assertEqual(pi_agent.run(d, cfg, ask=ask, fetcher=self.fake_fetch), [])
            self.assertIn("pi-agent", (d / "pi_outbox.md").read_text())


if __name__ == "__main__":
    unittest.main()
