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
import judge  # noqa: E402
import fetch_jobs  # noqa: E402
import pi_agent  # noqa: E402
from agent import eligibility, excerpt, notify, picks, rank, tracker  # noqa: E402
from supervisor.supervisor import Supervisor  # noqa: E402


def job(key, title="ML Intern", desc=None, posted="2026-11-20T10:00:00Z", loc="Bengaluru"):
    return {"key": key, "company": "x", "company_name": "X", "title": title, "location": loc,
            "url": f"https://x/{key}", "posted": posted, "updated": "",
            "description": f"python pytorch, team {key}" if desc is None else desc}


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


class FailedBoardNoteTests(unittest.TestCase):
    def note(self, boards, broken):
        def fake(source, slug):
            if int(slug) < broken:
                raise TimeoutError("slow")
            return []
        cfg = {"location_words": ["pune"],
               "companies": [{"source": "greenhouse", "slug": str(i)} for i in range(boards)]}
        with tempfile.TemporaryDirectory() as d:
            fetch_jobs.run(cfg, Path(d), fetcher=fake)
            return (Path(d) / "pi_outbox.md").read_text()

    def test_a_few_failures_among_many_boards_are_noted_without_waking_claude(self):
        note = self.note(boards=40, broken=2)
        self.assertIn("2 board(s) failed", note)
        self.assertNotIn("@claude", note)

    def test_widespread_failures_wake_claude(self):
        self.assertIn("@claude 5 board(s) failed", self.note(boards=40, broken=5))


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


class ExcerptTests(unittest.TestCase):
    INTRO = "About the company. " * 150  # about 2,850 characters of boilerplate
    NEEDS = "Minimum qualifications: Bachelor's degree, 3+ years of experience. Graduating in 2028."

    def test_short_text_is_unchanged(self):
        self.assertEqual(excerpt.excerpt("short posting", 3500), "short posting")

    def test_requirements_far_down_are_kept_with_the_start(self):
        text = "Role: ML Intern. " + self.INTRO + self.NEEDS + " We are an equal employer." * 100
        out = excerpt.excerpt(text, 2000)
        self.assertTrue(out.startswith("Role: ML Intern."))
        self.assertIn(self.NEEDS, out)
        self.assertIn(" [...] ", out)
        self.assertLessEqual(len(out), 2000)

    def test_without_requirement_words_keeps_start_and_end(self):
        out = excerpt.excerpt("START " + "filler " * 800 + "END", 1000)
        self.assertTrue(out.startswith("START"))
        self.assertTrue(out.endswith("END"))
        self.assertLessEqual(len(out), 1000)

    def test_collector_keeps_requirements_past_4000_characters(self):
        raw = {"id": 1, "title": "T", "content": "x " * 3000 + self.NEEDS}
        self.assertIn(self.NEEDS, fetch_jobs.normalize("greenhouse", "acme", raw)["description"])


class LeverTextTests(unittest.TestCase):
    RAW = {"id": "a", "text": "Intern", "hostedUrl": "u", "descriptionPlain": "About the company.",
           "lists": [{"text": "Responsibilities", "content": "<li>Improve HR processes</li>"},
                     {"text": "you should apply if you:", "content": "<li>have 1-2 years of experience</li>"}],
           "additionalPlain": "Only 2028 graduates.", "workplaceType": "onsite",
           "categories": {"commitment": "full time", "department": "People Corp", "team": "People Success"}}

    def test_lever_keeps_every_section_and_the_job_facts(self):
        text = fetch_jobs.normalize("lever", "acme", self.RAW)["description"]
        for part in ("Job type: full time", "Workplace: onsite", "Department: People Corp", "Team: People Success",
                     "About the company.", "Responsibilities: Improve HR processes",
                     "have 1-2 years of experience", "Only 2028 graduates."):
            self.assertIn(part, text)

    def test_lever_posting_with_missing_parts_adds_no_empty_labels(self):
        text = fetch_jobs.normalize("lever", "acme", {"id": "a", "text": "T", "descriptionPlain": "Hello"})["description"]
        self.assertEqual(text, "Hello")


class InternshipsOnlyTests(unittest.TestCase):
    def keys(self, jobs, **profile):
        return sorted(j["key"] for _, j in rank.shortlist(jobs, set(), profile=profile))

    def jobs(self):
        return [job("title", title="Software Engineer, Intern"), job("long", title="ML Internship 2027"),
                job("lever", title="Data Analyst", desc="Job type: Intern. python"),
                {**job("sr", title="AI Engineer"), "level": "internship"},
                job("full", title="Software Engineer", desc="python. You will mentor interns."),
                job("not", title="Internal Tools Engineer"),
                job("international", title="Software Engineer, International")]

    def test_only_internships_are_shortlisted(self):
        self.assertEqual(self.keys(self.jobs(), internships_only=True), ["lever", "long", "sr", "title"])

    def test_off_by_default(self):
        self.assertIn("full", self.keys(self.jobs()))


class RepeatPostingTests(unittest.TestCase):
    def keys(self, jobs, reviewed=()):
        return sorted(j["key"] for _, j in rank.shortlist(jobs, set(reviewed)))

    def jobs(self):
        same = dict(title="Software Intern", desc="Build tools in python.")
        return [job("a", **same), job("b", **same), job("c", title="Software Intern", desc="Test firmware in python.")]

    def test_the_same_posting_listed_twice_is_shortlisted_once(self):
        self.assertEqual(len(self.keys(self.jobs())), 2)
        self.assertIn("c", self.keys(self.jobs()))

    def test_a_copy_of_a_reviewed_posting_does_not_come_back(self):
        self.assertEqual(self.keys(self.jobs(), reviewed=["b"]), ["c"])


class OracleAndAmazonTests(unittest.TestCase):
    def oracle_get(self, url):
        self.urls.append(url)
        if "recruitingCEJobRequisitionDetails" in url:
            return {"items": [{"ExternalDescriptionStr": "<p>Join us.</p>", "ExternalResponsibilitiesStr": "<li>Build tools</li>",
                               "ExternalQualificationsStr": "<li>Graduating in 2028</li>", "JobSchedule": "Full time",
                               "WorkplaceType": "On-site"}]}
        row = lambda i, title, country, extra=(): {"Id": i, "Title": title, "PrimaryLocationCountry": country,
                                                   "PrimaryLocation": f"City, {country}", "PostedDate": "2026-11-26",
                                                   "secondaryLocations": list(extra)}
        return {"items": [{"TotalJobsCount": 4, "requisitionList": [
            row("1", "Software Intern", "IN"), row("2", "Software Intern", "US"),
            row("3", "Data Intern", "US", [{"CountryCode": "IN", "Name": "Pune, India"}]),
            row("4", "Senior Software Engineer", "IN")]}]}

    def setUp(self):
        self.urls = []

    def test_oracle_keeps_only_india_and_reads_every_section(self):
        jobs = fetch_jobs.oracle_jobs("abc.fa.us2.oraclecloud.com/CX_1", getter=self.oracle_get, pause=lambda s: None)
        self.assertEqual([j["key"] for j in jobs], ["or:abc:1", "or:abc:3"])
        self.assertEqual(jobs[1]["location"], "Pune, India")
        self.assertIn("/sites/CX_1/job/1", jobs[0]["url"])
        self.assertIn("siteNumber=CX_1,keyword=intern,location=India", self.urls[0])
        full = fetch_jobs.hydrate(jobs[0], getter=self.oracle_get)
        for part in ("Workplace: On-site", "Join us.", "Build tools", "Graduating in 2028"):
            self.assertIn(part, full["description"])
        self.assertNotIn("detail_url", full)

    def test_oracle_uses_the_short_description_when_there_is_no_long_one(self):
        reply = {"items": [{"ShortDescriptionStr": "<p>Design AI models.</p>", "ExternalDescriptionStr": ""}]}
        full = fetch_jobs.hydrate({"detail_url": "u", "detail_kind": "oracle"}, getter=lambda u: reply)
        self.assertIn("Design AI models.", full["description"])

    def test_amazon_reads_qualifications_and_marks_interns(self):
        data = {"jobs": [{"id_icims": "9", "title": "Data Scientist Intern", "normalized_location": "Chennai, Tamil Nadu, IND",
                          "job_path": "/en/jobs/9/x", "posted_date": "November 24, 2026", "is_intern": True,
                          "description": "Role.<br/>", "basic_qualifications": "- Python", "preferred_qualifications": "- SQL"}]}
        job_ = fetch_jobs.amazon_jobs("IND", getter=lambda u: data, pause=lambda s: None)[0]
        self.assertEqual((job_["key"], job_["posted"], job_["level"]), ("az:IND:9", "2026-11-24", "internship"))
        self.assertEqual(job_["url"], "https://www.amazon.jobs/en/jobs/9/x")
        self.assertTrue(job_["location"].endswith("India"))
        for part in ("Role.", "Basic qualifications: - Python", "Preferred qualifications: - SQL"):
            self.assertIn(part, job_["description"])

    def test_large_boards_keep_intern_titles_without_a_tech_word(self):
        self.assertTrue(fetch_jobs.worth_reading("2027 Summer Intern"))
        self.assertTrue(fetch_jobs.worth_reading("Software Engineer"))
        self.assertFalse(fetch_jobs.worth_reading("HR Intern"))
        self.assertFalse(fetch_jobs.worth_reading("Senior Software Engineer"))
        self.assertFalse(fetch_jobs.worth_reading("Accountant"))


class WorkdayCityFilterTests(unittest.TestCase):
    def test_board_with_only_city_filters_uses_the_indian_ones(self):
        sent = []
        facets = [{"facetParameter": "locationMainGroup", "values": [{"facetParameter": "locations", "values": [
            {"descriptor": "Bengaluru, Karnataka", "id": "blr"}, {"descriptor": "IND19-01-Pune", "id": "pun"},
            {"descriptor": "Mysuru, India", "id": "mys"}, {"descriptor": "Indianapolis, Indiana", "id": "usa"},
            {"descriptor": "Berlin, Germany", "id": "ber"}]}]}]

        def post(url, payload):
            sent.append(payload["appliedFacets"])
            return {"total": 1, "facets": facets, "jobPostings": [
                {"externalPath": "/job/Bengaluru/College-Intern_R1", "title": "College Intern", "locationsText": "2 Locations"}]}

        jobs = fetch_jobs.workday_jobs("hpe/wd5/Jobs", post=post, pause=lambda s: None, queries=("intern",))
        self.assertEqual(sent[-1], {"locations": ["blr", "pun", "mys"]})
        self.assertEqual([j["title"] for j in jobs], ["College Intern"])
        self.assertIn("india", jobs[0]["location"].lower())


class WorkdayPagingTests(unittest.TestCase):
    def test_reads_past_page_two_when_later_pages_report_total_zero(self):
        facets = [{"facetParameter": "Location_Country", "values": [{"descriptor": "India", "id": "in"}]}]

        def post(url, payload):  # like the real API: only the first page carries the total
            first = payload["offset"] == 0
            rows = range(payload["offset"], min(payload["offset"] + 20, 76))
            return {"total": 76 if first else 0, "facets": facets, "jobPostings": [
                {"externalPath": f"/job/India-Pune/Software-Intern_R{i}", "title": "Software Intern",
                 "locationsText": "Pune, India"} for i in rows]}

        jobs = fetch_jobs.workday_jobs("a/wd1/b", post=post, pause=lambda s: None, queries=("intern",), max_pages=10)
        self.assertEqual(len(jobs), 76)


class InternCollectorTests(unittest.TestCase):
    def test_ashby_keeps_the_job_type(self):
        raw = {"id": "1", "title": "Software Engineer", "location": "Pune", "employmentType": "Intern",
               "descriptionPlain": "Build."}
        self.assertTrue(fetch_jobs.is_internship(fetch_jobs.normalize("ashby", "acme", raw)))

    def test_internships_only_saves_and_hydrates_only_interns(self):
        def found(key, title, **extra):
            return {"key": key, "company": "x", "title": title, "location": "Pune, India", "url": "u",
                    "posted": "", "updated": "", "description": "text", **extra}

        asked = []

        def hydrator(j):
            asked.append(j["key"])
            return {k: v for k, v in j.items() if not k.startswith("detail_")}

        listed = [found("a", "ML Intern"), found("b", "Software Engineer"),
                  found("c", "Software Engineer", detail_url="u", detail_kind="workday"),
                  found("d", "College Intern", detail_url="u", detail_kind="workday"),
                  found("e", "AI Engineer", level="internship")]
        cfg = {"internships_only": True, "location_words": ["india"],
               "companies": [{"source": "greenhouse", "slug": "x"}]}
        with tempfile.TemporaryDirectory() as d:
            new, _ = fetch_jobs.run(cfg, Path(d), fetcher=lambda s, c: listed, hydrator=hydrator, pause=lambda s: None)
        self.assertEqual([j["key"] for j in new], ["a", "d", "e"])
        self.assertEqual(asked, ["d"])


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

    def test_claude_is_shown_requirements_from_deep_in_the_posting(self):
        long = "python " * 700 + "Minimum qualifications: 2028 graduates."
        (self.ws / "inbox" / "backlog.jsonl").write_text(json.dumps(job("a", desc=long)) + "\n")
        self.sup().run()
        self.assertIn("Minimum qualifications: 2028 graduates.",
                      (self.ws / "candidates.md").read_text(encoding="utf-8"))

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

    def test_request_the_model_dropped_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "pi_inbox.md").write_text("- [claude 2026-11-25] add company Razorpay; add company Observe.AI; "
                                           "add company Hasura.\n")
            cfg = d / "companies.json"
            cfg.write_text(json.dumps({"location_words": [], "companies": []}))
            ask = lambda m: [{"type": "add_company", "name": "Razorpay"}, {"type": "add_company", "name": "observe.ai"}]
            reply = pi_agent.run(d, cfg, ask=ask, fetcher=self.fake_fetch)[0]
            self.assertIn("Hasura: not handled", reply)
            self.assertEqual(reply.count("not handled"), 1)

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


class JudgeTests(unittest.TestCase):
    def write_backlog(self, d, jobs):
        (d / "backlog.jsonl").write_text("".join(json.dumps(j) + "\n" for j in jobs), encoding="utf-8")

    def verdicts(self, d):
        return {r["key"]: r["v"] for r in map(json.loads, (d / "verdicts.jsonl").read_text().splitlines())}

    def test_judges_each_job_once_and_asks_once_per_title(self):
        asked = []

        def ask(title):
            asked.append(title)
            return "No." if "Sales" in title else "yes"
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            self.write_backlog(d, [job("a", "ML Intern"), job("b", "ML Intern"), job("c", "Sales Intern")])
            self.assertEqual(judge.run(d, ask=ask)["judged"], 3)
            self.assertEqual(self.verdicts(d), {"a": "yes", "b": "yes", "c": "no"})
            self.assertEqual(sorted(asked), ["ML Intern", "Sales Intern"])
            self.assertEqual(judge.run(d, ask=ask)["judged"], 0)
            self.assertEqual(len(asked), 2)

    def test_newest_first_with_a_cap(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            old, new = job("old", "A"), job("new", "B")
            old["fetched"], new["fetched"] = "2026-11-01", "2026-11-25"
            self.write_backlog(d, [old, new])
            judge.run(d, ask=lambda t: "yes", limit=1)
            self.assertEqual(list(self.verdicts(d)), ["new"])

    def test_unclear_answer_or_dead_model_writes_no_verdict(self):
        def dead(title):
            raise OSError("model down")
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            self.write_backlog(d, [job("a", "A"), job("b", "B")])
            self.assertEqual(judge.run(d, ask=lambda t: "maybe?")["judged"], 0)
            out = judge.run(d, ask=dead)
            self.assertEqual((out["judged"], "model down" in out["error"]), (0, True))
            self.assertEqual((d / "verdicts.jsonl").read_text(), "")


class VerdictFilterTests(unittest.TestCase):
    def test_only_an_explicit_no_drops_a_job(self):
        jobs = [job("a"), job("b"), job("c")]
        keys = {j["key"] for _, j in rank.shortlist(jobs, set(), verdicts={"a": "no", "b": "yes"})}
        self.assertEqual(keys, {"b", "c"})  # c was never judged, so it stays

    def test_supervisor_uses_the_pi_verdicts(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / "inbox").mkdir()
            (ws / "inbox" / "backlog.jsonl").write_text(json.dumps(job("a")) + "\n" + json.dumps(job("b")) + "\n")
            (ws / "inbox" / "verdicts.jsonl").write_text('{"key": "a", "v": "no"}\nbroken line\n')
            (ws / "resume.md").write_text("resume")

            def claude(_p):
                (ws / "picks.json").write_text('{"picks": []}')
                return 0, "", ""
            cfg = {"demo": True, "master_resume": str(ws / "resume.md")}
            Supervisor(cfg, ws, now=datetime(2026, 11, 26, 10, 0), runner=claude).run()
            text = (ws / "candidates.md").read_text()
            self.assertIn("## b", text)
            self.assertNotIn("## a", text)


def row(rid, status, updated, company="Acme"):
    return {"id": rid, "key": f"k{rid}", "company": company, "title": "ML Intern", "status": status, "updated_on": updated}


class FollowUpTests(unittest.TestCase):
    def test_due_after_the_wait_only_for_sent_or_applied(self):
        rows = [row("1", "sent", "2026-11-19"), row("2", "applied", "2026-11-10"), row("3", "sent", "2026-11-20"),
                row("4", "replied", "2026-11-01"), row("5", "followed", "2026-11-01"), row("6", "", "")]
        self.assertEqual([(r["id"], n) for r, n in tracker.follow_ups(rows, "2026-11-26", 7)], [("1", 7), ("2", 16)])

    def test_followed_is_a_valid_update(self):
        self.assertEqual(tracker.parse_updates("1 followed")[0], [("1", "followed", "")])


class FollowUpDigestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        (self.ws / "inbox").mkdir()
        (self.ws / "resume.md").write_text("resume")
        tracker.write(self.ws / "tracker.csv", [row("1", "sent", "2026-11-10")])
        self.cfg = {"master_resume": str(self.ws / "resume.md"), "ntfy_topic": "t", "followup_days": 7}
        self.sent = []

    def tearDown(self):
        self.tmp.cleanup()

    def run_sup(self, day=26, hour=10):
        from unittest import mock

        def claude(_p):
            (self.ws / "picks.json").write_text('{"picks": []}')
            return 0, "", ""
        with mock.patch("supervisor.supervisor.notify.send", side_effect=lambda t, title, body: self.sent.append((title, body))):
            return Supervisor(self.cfg, self.ws, now=datetime(2026, 11, day, hour, 0), runner=claude).run()

    def test_sent_even_when_there_are_no_new_jobs_and_only_once_a_day(self):
        self.assertEqual(self.run_sup(), "nothing new")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("#1 Acme", self.sent[0][1])
        self.assertIn("1 followed", self.sent[0][1])
        self.run_sup(hour=11)
        self.assertEqual(len(self.sent), 1)

    def test_included_in_the_daily_digest(self):
        (self.ws / "inbox" / "backlog.jsonl").write_text(json.dumps(job("a")) + "\n")
        self.run_sup()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Follow up", self.sent[0][1])


class PhoneReplyTests(unittest.TestCase):
    def test_ntfy_poll_returns_only_messages(self):
        stream = ('{"id":"o","event":"open"}\n{"id":"m1","event":"message","message":"3 sent"}\n\nnot json\n'
                  '{"id":"k","event":"keepalive"}\n')
        seen_urls = []

        def get(url):
            seen_urls.append(url)
            return stream
        self.assertEqual(notify.poll("topic1", get=get), [("m1", "3 sent")])
        self.assertEqual(seen_urls, ["https://ntfy.sh/topic1/json?poll=1&since=all"])

    def setup_ws(self, d):
        ws = Path(d)
        tracker.write(ws / "tracker.csv", [row("1", "", ""), row("2", "", "")])
        return ws

    def test_replies_update_the_tracker_once_and_are_confirmed(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            ws = self.setup_ws(d)
            cfg = {"ntfy_topic": "digest", "ntfy_reply_topic": "replies"}
            msgs = [("m1", "1 sent; 2 skip - not for me\nhello there"), ("m2", "9 sent")]
            sent = []
            with mock.patch("supervisor.supervisor.notify.poll", return_value=msgs), \
                    mock.patch("supervisor.supervisor.notify.send", side_effect=lambda *a: sent.append(a)):
                for _ in range(2):  # ntfy keeps old messages, so the second run sees them again
                    Supervisor(cfg, ws, now=datetime(2026, 11, 26, 10, 0)).read_phone_replies()
            rows = tracker.read(ws / "tracker.csv")
            self.assertEqual([(r["status"], r["notes"]) for r in rows], [("sent", ""), ("skip", "not for me")])
            self.assertEqual(len(sent), 1)
            topic, _, body = sent[0]
            self.assertEqual(topic, "digest")
            for part in ("#1 sent", "#2 skip", "hello there", "no job #9"):
                self.assertIn(part, body)

    def test_off_without_a_reply_topic_and_a_failed_poll_is_harmless(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            ws = self.setup_ws(d)
            with mock.patch("supervisor.supervisor.notify.poll", side_effect=OSError("offline")) as p:
                Supervisor({}, ws).read_phone_replies()
                self.assertEqual(p.call_count, 0)
                Supervisor({"ntfy_reply_topic": "r"}, ws).read_phone_replies()
            self.assertIn("phone_poll_failed", (ws / "log.jsonl").read_text())

    def test_reply_topic_must_differ_from_digest_topic(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            ws = self.setup_ws(d)
            with mock.patch("supervisor.supervisor.notify.poll") as p:
                Supervisor({"ntfy_topic": "same", "ntfy_reply_topic": "same"}, ws).read_phone_replies()
            self.assertEqual(p.call_count, 0)


def run_once(ws, jobs):
    """One supervisor run on 2026-11-28 with a Claude that picks nothing."""
    (ws / "inbox").mkdir(exist_ok=True)
    (ws / "inbox" / "backlog.jsonl").write_text("".join(json.dumps(j) + "\n" for j in jobs))
    (ws / "resume.md").write_text("resume")

    def claude(_p):
        (ws / "picks.json").write_text('{"picks": []}')
        return 0, "", ""
    cfg = {"demo": True, "master_resume": str(ws / "resume.md")}
    return Supervisor(cfg, ws, now=datetime(2026, 11, 28, 10, 0), runner=claude).run()


class MaxAgeTests(unittest.TestCase):
    def keys(self, jobs, **kw):
        return sorted(j["key"] for _, j in rank.shortlist(jobs, set(), today="2026-11-28", **kw))

    def test_jobs_posted_more_than_30_days_ago_are_skipped(self):
        jobs = [job("new", posted="2026-11-27"), job("day30", posted="2026-10-29T23:00:00-04:00"),
                job("day31", posted="2026-10-28T10:00:00.000Z"), job("old", posted="2026-04-24T07:53:06.207000+00:00")]
        self.assertEqual(self.keys(jobs), ["day30", "new"])

    def test_a_job_with_no_readable_date_is_kept(self):
        self.assertEqual(self.keys([job("blank", posted=""), job("odd", posted="soon")]), ["blank", "odd"])

    def test_the_limit_can_be_changed_or_turned_off(self):
        jobs = [job("a", posted="2026-11-20"), job("b", posted="2026-04-24")]
        self.assertEqual(self.keys(jobs, max_age_days=5), [])
        self.assertEqual(self.keys(jobs, max_age_days=None), ["a", "b"])

    def test_supervisor_never_shows_claude_an_old_job(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            run_once(ws, [job("fresh", posted="2026-11-20"), job("stale", posted="2026-10-01")])
            text = (ws / "candidates.md").read_text(encoding="utf-8")
            self.assertIn("## fresh", text)
            self.assertNotIn("## stale", text)


class AppliedSkipTests(unittest.TestCase):
    def test_applied_and_every_later_status_count_but_sent_and_skip_do_not(self):
        statuses = ["applied", "followed", "replied", "interview", "rejected", "offer", "sent", "skip", ""]
        rows = [row(str(i), s, "2026-11-01", company=f"co {s}") for i, s in enumerate(statuses)]
        self.assertEqual(tracker.applied_companies(rows),
                         {"coapplied", "cofollowed", "coreplied", "cointerview", "corejected", "cooffer"})

    def test_every_job_from_an_applied_company_is_skipped(self):
        jobs = [dict(job("r"), company="revvity", company_name="revvity"),
                dict(job("i"), company="ibm", company_name="IBM"),
                dict(job("g"), company="gevernova", company_name="GE Vernova"),
                dict(job("n"), company="ibmx", company_name="IBMX Labs")]
        applied = tracker.applied_companies([row("1", "applied", "", company="Revvity"),
                                             row("2", "applied", "", company="IBM"),
                                             row("3", "applied", "", company="GEVernova")])
        self.assertEqual([j["key"] for _, j in rank.shortlist(jobs, set(), applied_companies=applied)], ["n"])

    def test_supervisor_skips_applied_companies_and_applied_jobs(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            jobs = [dict(job("samecompany"), company="revvity", company_name="revvity"),
                    dict(job("samejob"), company="y", company_name="Y"),
                    dict(job("other"), company="z", company_name="Z")]
            tracker.write(ws / "tracker.csv", [row("1", "applied", "2026-11-27", company="Revvity"),
                                               dict(row("2", "applied", "2026-11-27", company="Y"), key="samejob")])
            run_once(ws, jobs)
            text = (ws / "candidates.md").read_text(encoding="utf-8")
            self.assertIn("## other", text)
            self.assertNotIn("## samecompany", text)
            self.assertNotIn("## samejob", text)

    def test_a_company_marked_applied_in_updates_is_skipped_in_the_same_run(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            tracker.write(ws / "tracker.csv", [row("1", "", "", company="X")])
            (ws / "my_updates.md").write_text("1 applied\n")
            self.assertEqual(run_once(ws, [job("a")]), "nothing new")


class SplitNotificationTests(unittest.TestCase):
    def rows(self, n):
        return [{"id": str(i), "company": f"Company{i}", "title": "Software Engineering Intern " * 3,
                 "location": "Bengaluru, India", "posted": "2026-11-20", "why": "w" * 300,
                 "url": "https://example.com/" + "p" * 150, "draft": ""} for i in range(13, 13 + n)]

    def test_summary_names_every_pick_and_stays_short(self):
        title, body = picks.messages(self.rows(10), "2026-11-28")[0]
        self.assertEqual(title, "job-agent: 10 picks")
        for i in range(13, 23):
            self.assertIn(f"#{i} Company{i}", body)
        self.assertNotIn("https://", body)
        self.assertIn("Reply from your phone", body)
        self.assertLess(len(body.encode()), 1200)

    def test_each_pick_gets_its_own_message_under_the_ntfy_limit(self):
        msgs = picks.messages(self.rows(10), "2026-11-28")
        self.assertEqual(len(msgs), 11)
        for i, (title, body) in zip(range(13, 23), msgs[1:]):
            self.assertEqual(title, f"#{i} Company{i}")
            for part in ("Software Engineering Intern", "posted 2026-11-20", "why: www", "https://example.com/"):
                self.assertIn(part, body)
            self.assertLess(len(body.encode()), notify.LIMIT)

    def run_with_picks(self, ws, send):
        from unittest import mock

        def claude(_p):
            (ws / "picks.json").write_text(json.dumps({"picks": [{"key": k, "why": "fits", "hook": "h"}
                                                                 for k in ("a", "b", "c")]}))
            return 0, "", ""
        (ws / "inbox").mkdir()
        (ws / "inbox" / "backlog.jsonl").write_text("".join(json.dumps(job(k)) + "\n" for k in "abc"))
        (ws / "resume.md").write_text("resume")
        tracker.write(ws / "tracker.csv", [row("1", "sent", "2026-11-10")])
        cfg = {"master_resume": str(ws / "resume.md"), "ntfy_topic": "t"}
        with mock.patch("supervisor.supervisor.notify.send", side_effect=send):
            Supervisor(cfg, ws, now=datetime(2026, 11, 26, 10, 0), runner=claude).run()

    def test_supervisor_sends_a_summary_then_one_message_per_pick(self):
        sent = []
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            self.run_with_picks(ws, lambda t, title, body: sent.append((title, body)))
            self.assertEqual(len(sent), 4)
            self.assertTrue(sent[0][1].startswith("Follow up"))
            self.assertEqual(sorted(l for _, b in sent[1:] for l in b.splitlines() if l.startswith("https://")),
                             ["https://x/a", "https://x/b", "https://x/c"])
            full = (ws / "digest.md").read_text(encoding="utf-8")  # the file still holds everything
            for k in "abc":
                self.assertIn(f"https://x/{k}", full)

    def test_one_failed_message_does_not_stop_the_others(self):
        tried = []

        def send(topic, title, body):
            tried.append(title)
            if len(tried) == 2:
                raise OSError("rate limited")
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            self.run_with_picks(ws, send)
            self.assertEqual(len(tried), 4)
            self.assertIn("notify_failed", (ws / "log.jsonl").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
