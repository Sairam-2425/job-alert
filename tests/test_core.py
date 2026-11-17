"""Offline tests - no network, no OpenAI, no SMTP. Run with:  python -m pytest -q   (or python -m unittest)"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import main as app  # noqa: E402
from src.database import JobHistory  # noqa: E402
from src.duplicate_checker import filter_new, job_keys, normalize_url  # noqa: E402
from src.email_sender import EmailDeliveryError, build_digest  # noqa: E402
from src.job_fetcher import Job, extract_experience, is_recent, make_job, parse_date, prefilter  # noqa: E402
from src.job_matcher import MatchResult, categorize, score_job, weighted_score  # noqa: E402
from src.profile_analyzer import ground_profile  # noqa: E402
from src.utils import html_to_text, load_config, redact  # noqa: E402

CONFIG = load_config()
CONFIG["openai"]["request_delay_seconds"] = 0
PROFILE = {"technical_skills": ["Python", "SQL", "AWS"], "preferred_roles": ["Data Engineer"],
           "search_keywords": ["ETL"], "preferred_locations": ["Hyderabad"], "remote_ok": True,
           "total_experience_years": 4}


def mkjob(n=1, **kw):
    d = dict(source="remotive", job_id=str(n), title=f"Data Engineer {n}", company="Acme", location="Remote",
             url=f"https://acme.com/jobs/{n}?utm_source=x", description="Python SQL AWS ETL pipelines. " * 5,
             experience="3-5 years", posted_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    d.update(kw)
    return Job(**d)


def mkmatch(job, score=85):
    return MatchResult(job=job, score=score, category=categorize(score, CONFIG["scoring"]),
                       sub_scores={}, matching_skills=["Python"], missing_skills=["Spark"],
                       summary="A <b>great</b> role.", reason="Strong skill overlap.")


class TestDuplicates(unittest.TestCase):
    def test_normalize_url_strips_tracking_and_slash(self):
        a = normalize_url("http://WWW.Example.com/jobs/1/?utm_source=a&ref=b#top")
        b = normalize_url("https://example.com/jobs/1")
        self.assertEqual(a, b)

    def test_keeps_identifying_params(self):
        self.assertNotEqual(normalize_url("https://x.com/job?id=1"), normalize_url("https://x.com/job?id=2"))

    def test_filter_new_removes_history_and_batch_dupes(self):
        with tempfile.TemporaryDirectory() as d:
            h = JobHistory.load(Path(d) / "seen.json")
            old = mkjob(1)
            h.add(keys=job_keys(old), title=old.title, company=old.company, url=old.url, status="notified", score=90)
            same_url_other_tracking = mkjob(1, url="https://acme.com/jobs/1", job_id="")
            fresh_a, fresh_b_dupe = mkjob(2), mkjob(2)
            out = filter_new([same_url_other_tracking, fresh_a, fresh_b_dupe], h)
            self.assertEqual(out, [fresh_a])

    def test_history_roundtrip_and_prune(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "seen.json"
            h = JobHistory.load(p)
            j = mkjob(7)
            h.add(keys=job_keys(j), title=j.title, company=j.company, url=j.url, status="notified", score=80)
            h.save()
            h2 = JobHistory.load(p)
            self.assertTrue(h2.contains_any(job_keys(j)))
            rec = next(iter(h2.jobs.values()))
            rec["first_seen"] = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
            self.assertEqual(h2.prune(180), 1)
            self.assertFalse(h2.contains_any(job_keys(j)))

    def test_corrupt_history_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "seen.json"
            p.write_text("{not json")
            with self.assertRaises(Exception):
                JobHistory.load(p)


class TestFetcherHelpers(unittest.TestCase):
    def test_experience_extraction(self):
        self.assertEqual(extract_experience("We need 3-5 years of experience in Python"), "3-5 years")
        self.assertEqual(extract_experience("5+ years experience"), "5+ years")
        self.assertEqual(extract_experience("Great team culture"), "Not specified")

    def test_parse_date_variants(self):
        self.assertTrue(parse_date("2026-11-01T10:00:00Z").startswith("2026-11-01"))
        self.assertTrue(parse_date(1793000000000).startswith("2026"))
        self.assertTrue(parse_date("Tue, 03 Nov 2026 10:00:00 GMT").startswith("2026-11-03"))
        self.assertIsNone(parse_date("garbage"))

    def test_make_job_rejects_missing_url_and_cleans_html(self):
        self.assertIsNone(make_job(source="s", job_id=1, title="T", company="C", location="L", url="",
                                   description_html="x", posted=None))
        j = make_job(source="s", job_id=1, title="T", company="C", location="", url="https://a.b/1",
                     description_html="&lt;p&gt;Hello &lt;b&gt;world&lt;/b&gt;&lt;/p&gt;", posted=None)
        self.assertEqual(j.description, "Hello\nworld")
        self.assertEqual(j.location, "Not specified")

    def test_recency(self):
        old = mkjob(posted_at=(datetime.now(timezone.utc) - timedelta(days=30)).isoformat())
        self.assertFalse(is_recent(old, 14))
        self.assertTrue(is_recent(mkjob(posted_at=None), 14))

    def test_prefilter_ranks_and_filters(self):
        good = mkjob(1)
        bad = mkjob(2, title="Barista", description="Make coffee all day long. " * 5)
        out = prefilter([bad, good], PROFILE, min_score=2, limit=10)
        self.assertEqual(out, [good])

    def test_html_to_text_and_redact(self):
        self.assertEqual(html_to_text("<p>a</p><p>b</p>"), "a\nb")
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-secret-123456"}):
            self.assertNotIn("sk-secret", redact("key=sk-secret-123456"))


class TestScoring(unittest.TestCase):
    def test_weighted_score_and_categories(self):
        subs = dict(technical_skills=100, experience=50, education=50, title_relevance=100, location=0)
        self.assertEqual(weighted_score(subs, CONFIG["scoring"]["weights"]), 75)
        s = CONFIG["scoring"]
        self.assertEqual(categorize(80, s), "Highly relevant")
        self.assertEqual(categorize(79, s), "Moderately relevant")
        self.assertEqual(categorize(59, s), "Not relevant")

    def test_score_job_clamps_and_blocks_invented_skills(self):
        fake = {"technical_skills": 120, "experience": 80, "education": 80, "title_relevance": 90, "location": 70,
                "matching_skills": ["python", "Kubernetes"], "missing_skills": ["Spark", "SQL"],
                "summary": "s", "reason": "r"}
        with mock.patch("src.job_matcher.chat_json", return_value=fake):
            m = score_job(object(), "m", PROFILE, mkjob(), CONFIG)
        self.assertEqual(m.sub_scores["technical_skills"], 100)
        self.assertEqual(m.matching_skills, ["Python"])      # Kubernetes not in profile -> dropped
        self.assertEqual(m.missing_skills, ["Spark"])        # SQL is owned -> not "missing"
        self.assertTrue(0 <= m.score <= 100)

    def test_profile_grounding_drops_invented_skills(self):
        p = {"technical_skills": ["Python", "Rust", "Node.js"], "certifications": ["AWS Certified Developer"]}
        out = ground_profile(p, "Experienced in Python and NodeJS. AWS Certified Developer Associate.")
        self.assertEqual(out["technical_skills"], ["Python", "Node.js"])
        self.assertEqual(out["certifications"], ["AWS Certified Developer"])


class TestResumeParser(unittest.TestCase):
    def test_docx_parsing_including_tables(self):
        from docx import Document
        from src.resume_parser import ResumeError, parse_resume
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cv.docx"
            doc = Document()
            doc.add_paragraph("Jane Doe - Data Engineer. " + "Built Python and SQL ETL pipelines on AWS. " * 8)
            t = doc.add_table(rows=1, cols=2)
            t.cell(0, 0).text, t.cell(0, 1).text = "Skills", "Python, SQL, AWS"
            doc.save(path)
            parsed = parse_resume(path)
            self.assertIn("Jane Doe", parsed.text)
            self.assertIn("Python, SQL, AWS", parsed.text)
            self.assertEqual(len(parsed.sha256), 64)
            (Path(d) / "short.docx").write_bytes(path.read_bytes())
            tiny = Document(); tiny.add_paragraph("too short"); tiny.save(Path(d) / "tiny.docx")
            with self.assertRaises(ResumeError):
                parse_resume(Path(d) / "tiny.docx")
            with self.assertRaises(ResumeError):
                parse_resume(Path(d) / "notes.txt")

    def test_pdf_parsing(self):
        try:
            import fitz
        except ImportError:
            self.skipTest("PyMuPDF not installed")
        from src.resume_parser import parse_resume
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cv.pdf"
            doc = fitz.open()
            page = doc.new_page()
            page.insert_textbox(fitz.Rect(50, 50, 550, 750), "Jane Doe Data Engineer. " + "Python SQL AWS ETL pipelines. " * 12)
            doc.save(path)
            self.assertIn("Jane Doe", parse_resume(path).text)


class TestLLMJson(unittest.TestCase):
    def test_extract_json_handles_fences_and_chatter(self):
        from src.llm import LLMError, extract_json
        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(extract_json('Sure! Here you go: {"a": {"b": 2}} Hope it helps'), {"a": {"b": 2}})
        with self.assertRaises(LLMError):
            extract_json("no json here")


RESUME = """JANE DOE
Data Engineer
jane@example.com | Hyderabad, India

SUMMARY
Data engineer with 4+ years of experience building ETL pipelines.

SKILLS
Python, SQL, PySpark, Airflow, AWS (S3, Lambda), Docker, Git, Power BI

EXPERIENCE
Data Engineer - Acme Corp
Jan 2021 - Present
- Built Kafka and Spark pipelines on AWS

EDUCATION
B.Tech in Computer Science, JNTU, 2020

CERTIFICATIONS
AWS Certified Cloud Practitioner

PROJECTS
Sales Dashboard
- Power BI dashboard on Snowflake
"""


class TestLocalMode(unittest.TestCase):
    def setUp(self):
        from src.local_analyzer import analyze_local
        self.profile = analyze_local(RESUME, {"extra_skills": []})
        self.profile["preferred_locations"] = ["Hyderabad"]
        self.profile["remote_ok"] = True

    def test_profile_extraction(self):
        p = self.profile
        self.assertEqual(p["full_name"], "Jane Doe")
        for skill in ("Python", "SQL", "Apache Spark", "Airflow", "AWS", "Docker", "Power BI", "Kafka"):
            self.assertIn(skill, p["technical_skills"])
        self.assertNotIn("Java", p["technical_skills"])          # not invented / no false positive from JavaScript-style substrings
        self.assertEqual(p["current_title"], "Data Engineer")
        self.assertEqual(p["total_experience_years"], 4.0)        # stated "4+ years"
        self.assertEqual(p["education"][0]["degree"], "Bachelor's")
        self.assertIn("AWS Certified Cloud Practitioner", p["certifications"])
        self.assertIn("Sales Dashboard", [x["name"] for x in p["projects"]])

    def test_years_from_date_ranges(self):
        from src.local_analyzer import estimate_years
        y = estimate_years("Engineer Jan 2020 - Jan 2023", now=datetime(2026, 1, 1))
        self.assertEqual(y, 3.0)

    def test_scoring_good_vs_bad_job(self):
        good = mkjob(1, title="Senior Data Engineer", location="Hyderabad", experience="3-5 years",
                     description="We need Python, SQL, Spark and AWS experience to build ETL pipelines. Bachelor's degree. " * 2)
        bad = mkjob(2, title="Java Backend Developer", location="Berlin", experience="8+ years",
                    description="Java, Spring Boot, Oracle and Kubernetes microservices. Master's degree required. " * 2)
        g = score_job(None, "", self.profile, good, CONFIG)
        b = score_job(None, "", self.profile, bad, CONFIG)
        self.assertGreaterEqual(g.score, 80, g.reason)
        self.assertLess(b.score, 60, b.reason)
        self.assertIn("Python", g.matching_skills)
        self.assertIn("Java", b.missing_skills)

    def test_mode_selection(self):
        with mock.patch.dict(os.environ, {"NVIDIA_API_KEY": "", "OPENAI_API_KEY": ""}):
            self.assertFalse(app.use_ai({"ai": {"mode": "auto"}}))
        with mock.patch.dict(os.environ, {"NVIDIA_API_KEY": "nvapi-abcdef"}):
            self.assertTrue(app.use_ai({"ai": {"mode": "auto"}}))
            self.assertFalse(app.use_ai({"ai": {"mode": "local"}}))


class TestEmail(unittest.TestCase):
    def test_digest_contains_fields_and_escapes_html(self):
        j = mkjob(1, title="<script>alert(1)</script> Engineer")
        subject, html, text = build_digest([mkmatch(j, 85), mkmatch(mkjob(2), 92)])
        self.assertIn("2 new job matches", subject)
        self.assertIn("top 92%", subject)
        self.assertNotIn("<script>", html)
        for needle in ("Acme", "Remote", "3-5 years", "Python", "Spark", "View &amp; apply", "92%"):
            self.assertIn(needle, html)
        self.assertIn("https://acme.com/jobs/2", text)


class TestPipeline(unittest.TestCase):
    """End-to-end with fakes: the key guarantee is 'never mark notified before email succeeds'."""

    def _run(self, send_side_effect):
        d = tempfile.mkdtemp()
        hist = Path(d) / "seen.json"
        jobs = [mkjob(1), mkjob(2)]
        scores = {"Data Engineer 1": 90, "Data Engineer 2": 30}
        sent = []

        def fake_send(batch, **kw):
            if send_side_effect:
                raise send_side_effect
            sent.extend(batch)

        env = {"EMAIL_FROM": "a@x.com", "EMAIL_TO": "b@x.com", "EMAIL_PASSWORD": "pw123456", "OPENAI_API_KEY": "sk-test-123456"}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(app, "get_client", return_value=object()), \
             mock.patch.object(app, "ensure_profile", return_value=PROFILE), \
             mock.patch.object(app, "fetch_all", return_value=(jobs, 0, 1)), \
             mock.patch.object(app, "score_job", side_effect=lambda c, m, p, j, cfg: mkmatch(j, scores[j.title])), \
             mock.patch.object(app, "send_digest", side_effect=fake_send):
            code = app.run(CONFIG, history_path=hist, profile_path=Path(d) / "p.json", resume_dir=Path(d))
        return code, JobHistory.load(hist), sent

    def test_success_marks_notified_and_irrelevant_scored(self):
        code, h, sent = self._run(None)
        self.assertEqual(code, 0)
        self.assertEqual([m.job.title for m in sent], ["Data Engineer 1"])
        statuses = {r["title"]: r["status"] for r in h.jobs.values()}
        self.assertEqual(statuses, {"Data Engineer 1": "notified", "Data Engineer 2": "scored"})

    def test_email_failure_does_not_mark_notified(self):
        code, h, sent = self._run(EmailDeliveryError("boom"))
        self.assertEqual(code, 1)
        statuses = {r["title"]: r["status"] for r in h.jobs.values()}
        self.assertEqual(statuses, {"Data Engineer 2": "scored"})   # job 1 will be retried next run

    def test_second_run_sends_nothing_new(self):
        d = tempfile.mkdtemp()
        hist = Path(d) / "seen.json"
        jobs = [mkjob(1)]
        env = {"OPENAI_API_KEY": "sk-test-123456"}
        send = mock.Mock()
        with mock.patch.dict(os.environ, {"EMAIL_FROM": "a@x.com", "EMAIL_TO": "b@x.com", "EMAIL_PASSWORD": "pw123456", **env}), \
             mock.patch.object(app, "get_client", return_value=object()), \
             mock.patch.object(app, "ensure_profile", return_value=PROFILE), \
             mock.patch.object(app, "fetch_all", return_value=(jobs, 0, 1)), \
             mock.patch.object(app, "score_job", side_effect=lambda c, m, p, j, cfg: mkmatch(j, 95)), \
             mock.patch.object(app, "send_digest", send):
            for _ in range(2):
                app.run(CONFIG, history_path=hist, profile_path=Path(d) / "p.json", resume_dir=Path(d))
        self.assertEqual(send.call_count, 1)


if __name__ == "__main__":
    unittest.main()
