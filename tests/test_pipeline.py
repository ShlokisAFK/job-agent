import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pi"))
import digger  # noqa: E402
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


class LargeBoardTests(unittest.TestCase):
    WORDS = ["india", "bengaluru", "pune", "remote"]

    def test_workday_facet_found_in_nested_tree(self):
        tree = [{"facetParameter": "locationMainGroup", "values": [
            {"facetParameter": "locationHierarchy1", "descriptor": "Locations", "values": [
                {"descriptor": "Armenia", "id": "a"}, {"descriptor": "India", "id": "ind"}]}]}]
        self.assertEqual(fetch_jobs._find_facet(tree, "India"), ("locationHierarchy1", "ind"))
        self.assertIsNone(fetch_jobs._find_facet(tree, "Narnia"))

    def test_workday_search_uses_india_filter_and_skips_senior_and_non_tech(self):
        calls = []

        def post(url, payload):
            calls.append(payload["appliedFacets"])
            facets = [{"facetParameter": "locationHierarchy1", "values": [{"descriptor": "India", "id": "ind"}]}]
            posts = [{"title": "ML Intern", "externalPath": "/job/India-Pune/ML-Intern_JR1", "locationsText": "2 Locations",
                      "bulletFields": ["JR1"], "postedOn": "Posted Today"},
                     {"title": "Senior ML Engineer", "externalPath": "/job/India-Pune/Sr_JR2", "locationsText": "India, Pune", "bulletFields": ["JR2"]},
                     {"title": "Facilities Coordinator", "externalPath": "/job/India-Pune/Fac_JR3", "locationsText": "India, Pune", "bulletFields": ["JR3"]}]
            return {"total": 3, "jobPostings": posts, "facets": facets}

        jobs = fetch_jobs.workday_jobs("acme/wd5/Site", post=post, pause=lambda s: None, queries=("intern",))
        self.assertEqual([j["title"] for j in jobs], ["ML Intern"])
        self.assertEqual(jobs[0]["key"], "wd:acme:JR1")
        self.assertEqual(jobs[0]["location"], "India, Pune")  # not the useless "2 Locations"
        self.assertEqual(jobs[0]["detail_kind"], "workday")
        self.assertEqual(calls[-1], {"locationHierarchy1": ["ind"]})  # every real query is limited to India

    def test_smartrecruiters_drops_senior_levels(self):
        def getter(url):
            self.assertIn("country=in", url)
            return {"totalFound": 3, "content": [
                {"id": "1", "name": "Software Engineer", "location": {"city": "Pune"}, "experienceLevel": {"id": "entry_level"}, "releasedDate": "2026-11-01"},
                {"id": "2", "name": "Software Engineer", "location": {"city": "Pune"}, "experienceLevel": {"id": "director"}},
                {"id": "3", "name": "Plant Controller", "location": {"city": "Pune"}, "experienceLevel": {"id": "entry_level"}}]}

        jobs = fetch_jobs.smartrecruiters_jobs("Acme", getter=getter, pause=lambda s: None)
        self.assertEqual([j["key"] for j in jobs], ["sr:Acme:1"])
        self.assertEqual(jobs[0]["level"], "entry_level")

    def test_hydrate_fills_description_and_removes_helper_fields(self):
        job = {"description": "", "detail_url": "u", "detail_kind": "smartrecruiters"}
        out = fetch_jobs.hydrate(job, getter=lambda u: {"jobAd": {"sections": {"jobDescription": {"text": "<p>Build ML</p>"}}}})
        self.assertEqual(out["description"], "Build ML")
        self.assertNotIn("detail_url", out)

    def test_run_respects_caps_and_retries_after_rate_limit(self):
        import urllib.error
        cfg = {"location_words": self.WORDS, "detail_caps": {"workday": 2}, "companies": [{"name": "A", "source": "workday", "slug": "x"}]}
        jobs = lambda: [{"key": f"wd:a:{i}", "company": "a", "title": "Engineer", "location": "India, Pune", "url": "u", "posted": "",
                         "updated": "", "description": "", "detail_url": "u", "detail_kind": "workday"} for i in range(4)]
        attempts = {"n": 0}

        def hydrator(job):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise urllib.error.HTTPError("u", 429, "slow down", {}, None)
            job["description"] = "ok"
            job.pop("detail_url"); job.pop("detail_kind")
            return job

        with tempfile.TemporaryDirectory() as d:
            new, errors = fetch_jobs.run(cfg, Path(d), fetcher=lambda s, c: jobs(), hydrator=hydrator, pause=lambda s: None)
            self.assertEqual(len(new), 2)  # cap of 2; the other two wait for the next run
            self.assertEqual(errors, [])
            self.assertTrue(all("detail_url" not in j for j in new))
            again, _ = fetch_jobs.run(cfg, Path(d), fetcher=lambda s, c: jobs(), hydrator=hydrator, pause=lambda s: None)
            self.assertEqual(len(again), 2)  # next run picks up the rest, nothing twice

    def test_persistent_rate_limit_is_reported_calmly_not_as_an_alert(self):
        import urllib.error
        cfg = {"location_words": self.WORDS, "companies": [{"name": "A", "source": "workday", "slug": "x"}]}
        job = [{"key": "wd:a:1", "company": "a", "title": "Engineer", "location": "India, Pune", "url": "u", "posted": "",
                "updated": "", "description": "", "detail_url": "u", "detail_kind": "workday"}]

        def always_429(j):
            raise urllib.error.HTTPError("u", 429, "slow down", {}, None)

        with tempfile.TemporaryDirectory() as d:
            new, errors = fetch_jobs.run(cfg, Path(d), fetcher=lambda s, c: [dict(j) for j in job], hydrator=always_429, pause=lambda s: None)
            self.assertEqual((new, errors), ([], []))
            note = (Path(d) / "pi_outbox.md").read_text()
            self.assertIn("rate limited", note)
            self.assertNotIn("@claude", note)  # must not wake Claude


class DiggerTests(unittest.TestCase):
    def test_bing_links_are_decoded_and_search_results_parsed(self):
        import base64
        enc = "a1" + base64.urlsafe_b64encode(b"https://www.razorpay.com/careers/").decode().rstrip("=")
        page = ('<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?!&amp;&amp;p=x&amp;u=' + enc + '&amp;ntb=1">Razorpay Careers</a></h2>'
                '<div class="b_caption"><p>Join us. Open roles in Bengaluru.</p></div></li>')
        res = digger.bing_search("razorpay careers", fetch=lambda url: (url, page))
        self.assertEqual(res, [{"url": "https://www.razorpay.com/careers/", "title": "Razorpay Careers",
                                "snippet": "Join us. Open roles in Bengaluru."}])

    def test_only_public_web_addresses_are_allowed(self):
        for bad in ("http://127.0.0.1:8080/v1/models", "http://192.168.1.5/admin", "http://localhost/", "file:///etc/passwd", "ftp://example.com/x"):
            self.assertFalse(digger.public_url(bad), bad)

    def test_detects_supported_boards_and_ignores_embed_scaffolding(self):
        page = ('<a href="https://job-boards.greenhouse.io/razorpaysoftwareprivatelimited">Jobs</a>'
                '<script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script>'
                '<a href="https://jobs.lever.co/zeta/abc-123">x</a>'
                '<a href="https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/x">y</a>'
                '<a href="https://jobs.smartrecruiters.com/BoschGroup/123">z</a>')
        self.assertEqual(digger.detect_boards(page), [
            ("greenhouse", "razorpaysoftwareprivatelimited"), ("greenhouse", "acme"), ("lever", "zeta"),
            ("smartrecruiters", "BoschGroup"), ("workday", "nvidia/wd5/NVIDIAExternalCareerSite")])

    def test_model_company_names_are_checked_against_the_text(self):
        results = [{"title": "Razorpay hiring ML interns in Bengaluru", "snippet": "Also Swiggy and Naukri list jobs"}]
        ask = lambda s, u, max_tokens=0: {"companies": ["Razorpay", "Swiggy", "Naukri", "Madeup Corp", "x"]}
        self.assertEqual(digger.model_company_names(ask, results), ["Razorpay", "Swiggy"])  # invented / aggregator / too short dropped

    def test_model_link_pick_ignores_bad_indexes(self):
        links = [("https://a.com/x", "Blog"), ("https://a.com/jobs", "Openings")]
        self.assertEqual(digger.model_pick_links(lambda s, u, max_tokens=0: {"pick": [2, 9, "x", 2]}, "A", links), [links[1]])

    def _setup(self, tmp):
        d = Path(tmp)
        cfg = d / "companies.json"
        cfg.write_text(json.dumps({"location_words": ["india"], "companies": [{"name": "Old", "source": "lever", "slug": "old"}]}))
        return d, cfg

    def _fakes(self, page_for):
        search = lambda q: [{"url": "https://www.razorpay.com/careers", "title": "Careers", "snippet": ""}]
        fetch = lambda url: (url, page_for(url))
        ask = lambda s, u, max_tokens=0: {"pick": []}
        return search, fetch, ask

    def test_run_adds_a_verified_board_and_remembers_the_company(self):
        with tempfile.TemporaryDirectory() as tmp:
            d, cfg = self._setup(tmp)
            search, fetch, ask = self._fakes(lambda u: '<a href="https://job-boards.greenhouse.io/razorpaysoftwareprivatelimited">j</a>')
            verify = lambda source, slug: {"total": 40, "india": 25}
            added, notes = digger.run(d, cfg, [], only=["Razorpay"], search=search, fetch=fetch, ask=ask, verify=verify, pause=lambda s: None)
            self.assertEqual(added, ["Razorpay: added (greenhouse/razorpaysoftwareprivatelimited, 40 jobs, 25 in India)"])
            self.assertIn({"name": "Razorpay", "source": "greenhouse", "slug": "razorpaysoftwareprivatelimited"}, json.loads(cfg.read_text())["companies"])
            self.assertIn("digger: Razorpay: added", (d / "pi_outbox.md").read_text())
            self.assertNotIn("@claude", (d / "pi_outbox.md").read_text())
            added2, _ = digger.run(d, cfg, ["Razorpay"], search=search, fetch=fetch, ask=ask, verify=verify, pause=lambda s: None)
            self.assertEqual(added2, [])  # already watched, not checked again

    def test_run_rejects_boards_without_india_jobs_and_reports_missing_boards(self):
        with tempfile.TemporaryDirectory() as tmp:
            d, cfg = self._setup(tmp)
            search, fetch, ask = self._fakes(lambda u: '<a href="https://jobs.lever.co/razorpay">j</a>')
            added, notes = digger.run(d, cfg, [], only=["Razorpay"], search=search, fetch=fetch, ask=ask,
                                      verify=lambda s, sl: {"total": 12, "india": 0}, pause=lambda s: None)
            self.assertEqual(added, [])
            self.assertEqual(len(json.loads(cfg.read_text())["companies"]), 1)  # nothing added
            search, fetch, ask = self._fakes(lambda u: "<html>our own custom careers app</html>")
            _, notes = digger.run(d, cfg, [], only=["Razorpay"], search=search, fetch=fetch, ask=ask, verify=None, pause=lambda s: None)
            self.assertTrue(any("needs a custom scraper" in n for n in notes))

    def test_daily_search_budget_stops_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            d, cfg = self._setup(tmp)
            (d / "digger_state.json").write_text(json.dumps({"companies": {}, "searches": {"date": "2026-11-25", "n": 40}}))
            search, fetch, ask = self._fakes(lambda u: "")
            added, notes = digger.run(d, cfg, [], only=["Razorpay"], now=datetime(2026, 11, 25, 3, 0, tzinfo=__import__("datetime").timezone.utc),
                                      search=search, fetch=fetch, ask=ask, verify=None, pause=lambda s: None)
            self.assertEqual(added, [])
            self.assertTrue(any("budget" in n for n in notes))


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


class PiSyncFailureTests(unittest.TestCase):
    def test_slow_pi_connection_does_not_stop_the_run(self):
        import subprocess
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / "inbox").mkdir()
            (ws / "inbox" / "backlog.jsonl").write_text(json.dumps(job("a")) + "\n")
            resume = ws / "resume.md"
            resume.write_text("resume")
            cfg = {"master_resume": str(resume), "pi_host": "pi", "pi_data_dir": "/x", "max_wakes_per_day": 2}
            calls = []

            def claude(_p):
                calls.append(1)
                (ws / "picks.json").write_text('{"picks": []}')
                return 0, "", ""

            def slow_scp(*a, **k):
                raise subprocess.TimeoutExpired(a[0], 60)

            with mock.patch("supervisor.supervisor.subprocess.run", side_effect=slow_scp):
                sup = Supervisor(cfg, ws, now=datetime(2026, 11, 25, 10, 0), runner=claude)
                self.assertEqual(sup.run(), "picked 0")  # kept going on the data it already had
            self.assertEqual(len(calls), 1)
            self.assertIn("pi_sync_failed", (ws / "log.jsonl").read_text())


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

    def test_degree_deadline_rules_out_earlier_batches_only(self):
        stripe = "A Bachelor's or Master's degree in computer science or a directly related field, obtained by summer 2027, or equivalent work experience"
        self.assertIn("2027", self.check(stripe))
        self.assertIn("2027", self.check("You must graduate by 2027."))
        for ok in ("degree obtained by summer 2029", "You must graduate by end of 2028", "Please apply by 2027 to be considered",
                   "Complete the online assessment by 2027-01-15"):
            self.assertEqual(self.check(ok), "", ok)

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
