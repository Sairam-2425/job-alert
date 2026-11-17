# AI Job Alert System

Finds job openings that match your resume, scores them with OpenAI, filters out jobs you've already been told about,
and emails you an HTML digest - all on **GitHub Actions every 6 hours**. No server, no n8n/Zapier/Make, and **no
automatic applications**.

```
resume (PDF/DOCX) -> parse -> OpenAI profile -> fetch jobs -> validate/recency -> de-duplicate
      -> keyword prefilter -> OpenAI scoring (0-100) -> Gmail digest -> mark as notified -> commit history
```

## 1. Files and what they do

| File | Purpose |
|---|---|
| `.github/workflows/job_alert.yml` | Cron (every 6h) + manual run. Python 3.11, installs deps, runs the app with retries, commits history back (even on failure). `concurrency` prevents overlapping runs; permissions limited to `contents: write`. |
| `src/main.py` | Orchestrates the pipeline. Exit codes: `0` ok, `1` transient failure (workflow retries), `3` config error (no retry). **Marks jobs notified only after the email is delivered.** |
| `src/resume_parser.py` | PDF (PyMuPDF) and DOCX (python-docx, incl. tables) text extraction; rejects scanned/empty files; hashes the file. |
| `src/profile_analyzer.py` | Sends resume text to the LLM (`prompts/resume_prompt.txt`), then **grounds** the result: skills/certifications that don't appear in your resume text are dropped. Re-analyzes only when the resume file changes. |
| `src/job_fetcher.py` | Source adapters (Remotive, Arbeitnow, Adzuna, Greenhouse, Lever, RSS, generic authorized JSON API), date/experience extraction, validity + recency checks, keyword prefilter. One failing source never stops the run. |
| `src/local_analyzer.py`, `src/local_matcher.py`, `src/skills_catalog.py` | No-AI mode: dictionary/heuristic resume analysis and rule-based job scoring. |
| `src/job_matcher.py` | OpenAI returns 5 sub-scores + skills + reasoning (`prompts/matching_prompt.txt`); the final 0-100 score is a **weighted average computed in code** from `config.json`. |
| `src/duplicate_checker.py` | Normalizes URLs (strips tracking params/fragments/`www`/trailing slash), plus source job-ID and title/company/location fingerprint keys. A job is a duplicate if *any* key matches. |
| `src/database.py` | `data/seen_jobs.json` store with atomic writes (temp file + `os.replace`), retention pruning, refuses to run on a corrupt file (so it can't re-spam old jobs). |
| `src/email_sender.py` | Builds the HTML + plain-text digest and sends via Gmail SMTP (SSL 465) with retry; clear error for bad App Password. |
| `src/llm.py`, `src/utils.py` | Shared OpenAI JSON helper, JSON logging with secret redaction, retry/backoff, HTML cleanup, config loader. |
| `config.json` | Scoring weights/thresholds, sources, limits, preferences overrides. |
| `data/candidate_profile.json` | Structured profile (generated). |
| `data/seen_jobs.json` | Job history (generated, committed by the workflow). |
| `prompts/*.txt` | The two AI prompts - edit freely. |
| `tests/test_core.py` | Offline tests (dedupe, history, scoring, grounding, email, resume parsing, full pipeline incl. email-failure behaviour). |

## 2. Setup (about 10 minutes)

1. **Create a private GitHub repository** and push this folder to it (the history file and resume live in the repo).
2. **Add your resume** to `resume/` (`.pdf` or `.docx`). *Prefer to keep it off GitHub?* See "Keeping your resume private" below.
3. **Create the secrets** (only the 3 email ones are required - the AI key is optional, see "Running without any AI key") (Repo -> Settings -> Secrets and variables -> Actions -> New repository secret):

   | Secret | Value |
   |---|---|
   | `NVIDIA_API_KEY` | **Optional.** Free key from build.nvidia.com (starts with `nvapi-`). Leave it unset to run in no-AI local mode. |
   | `EMAIL_FROM` | Your **secondary** Gmail address (sender). |
   | `EMAIL_TO` | Your **primary** email address (receiver). |
   | `EMAIL_PASSWORD` | A Gmail **App Password** for the secondary account (below) - *not* your normal password. |

   Optional repository **variable** `OPENAI_MODEL` to override the model in `config.json` (default `meta/llama-3.3-70b-instruct` on NVIDIA). The AI layer speaks the OpenAI-compatible protocol, so to use OpenAI instead set `openai.base_url` to `""` in `config.json`, a model like `gpt-4o-mini`, and store the key as `OPENAI_API_KEY` (and swap it into the workflow `env:`).
4. **Allow the workflow to push history**: Settings -> Actions -> General -> Workflow permissions -> *Read and write permissions* (the workflow also declares `permissions: contents: write`). If you protect your branch, allow the Actions bot to push or the history commit will fail.
5. **Edit `config.json`**: set `preferences.locations` / `roles` if the resume doesn't state them, and enable the sources you want (section 4).
6. **Test**: Actions tab -> *AI Job Alert* -> *Run workflow* with **dry_run = true**. Open the run's `digest-preview` artifact to see the email. Then run once with dry_run = false.

### Gmail SMTP configuration
1. Sign in to the **secondary** Google account -> Security -> turn on **2-Step Verification**.
2. Go to <https://myaccount.google.com/apppasswords>, create an app password named "job-alert", copy the 16 characters (spaces optional).
3. Store it as `EMAIL_PASSWORD`. Settings used: `smtp.gmail.com`, port `465` (SSL), login = `EMAIL_FROM`.
4. First email may land in spam - mark it "Not spam" in your primary inbox / add the sender to contacts.

### Keeping your resume private (Option B)
Run locally once: `pip install -r requirements.txt`, `export NVIDIA_API_KEY=nvapi-...`, put the resume in `resume/`, run
`python -m src.main --dry-run`. This writes `data/candidate_profile.json`. Uncomment the `resume/*.pdf|docx` lines in
`.gitignore`, commit only the profile JSON. With no resume in the repo, the workflow reuses the saved profile.

### Running without any AI key (local mode)
`config.json -> ai.mode`: `"auto"` (default) uses the AI only when `NVIDIA_API_KEY`/`OPENAI_API_KEY` is set, `"local"` never uses AI, `"llm"` requires a key.
With no key the app runs fully offline on rules:
- **Resume**: skills come from a built-in dictionary (`src/skills_catalog.py`, ~200 skills; add yours in `preferences.extra_skills`), plus heuristics for name, job titles, years of experience (stated, or from date ranges), education level, certifications and projects.
- **Matching**: the same 5 sub-scores and weights, from rules: share of the job's skills you have, title word overlap with your roles, experience range fit, degree requirement, location/remote fit. The email's "why this score" lists each rule's result.
- **Set your target roles and locations in `config.json -> preferences`** (e.g. `"roles": ["Data Engineer", "Analytics Engineer"]`, `"locations": ["Hyderabad", "Bengaluru"]`) - local mode can't infer them as well as an AI.
- **Limits**: it only recognizes skills in the dictionary, can't read nuance ("nice to have" vs "required"), and misses synonyms. Expect more false positives/negatives than AI mode; tune `notify_min_score` (try 65-75). Scoring and email logic are otherwise identical, and you can add a key later without changing anything.

## 3. Resume parsing & profile
`resume_parser.py` extracts text (PDF -> PyMuPDF with reading-order sorting; DOCX -> paragraphs + tables). The text goes to
OpenAI with `prompts/resume_prompt.txt` (JSON mode, temperature 0). The profile contains name, title, summary, years of
experience, skills, experience, education, projects, certifications, preferred roles/locations and search keywords.
**Anti-invention safeguards:** the prompt forbids inference, and code removes any skill/certification not supported by the
resume text. `config.json -> preferences` overrides roles/locations when you want to search differently from your resume.
A scanned (image-only) PDF is rejected with a clear error - export a text PDF or use DOCX.

## 4. Job sources & API integration guide
All sources are authorized, public APIs/feeds. **The app never scrapes behind logins, solves CAPTCHAs, or evades blocks.**

| Source (`config.json -> sources`) | Setup |
|---|---|
| `remotive`, `arbeitnow` | Enabled by default, no key. Remote/international jobs. Remotive asks for sparse polling; the app makes one request per run. |
| `adzuna` | Free key at developer.adzuna.com. Set `enabled: true`, `country` (e.g. `in`, `us`, `gb`), optional `where` (city). Add secrets `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` and uncomment the two matching lines in the workflow `env:`. Good for India listings. |
| `greenhouse` | Public company boards: `"boards": ["stripe", "airbnb"]` (the slug in `boards.greenhouse.io/<slug>`). |
| `lever` | `"companies": ["<slug>"]` from `jobs.lever.co/<slug>`. |
| `rss` | Any job RSS/Atom feed you are allowed to use: add `{ "name": "...", "url": "..." }`. |
| `custom_json` | Generic adapter for **any JSON job API you are authorized to call** (partner/ATS APIs). Map fields with dotted paths; use `${MY_TOKEN}` in headers and add that secret to the workflow `env:` block. See the example in `config.json`. |

**LinkedIn, Naukri, Indeed:** none offers an open public job-search API, and their terms prohibit scraping, so they are
*not* built in. If you have authorized access (a partner API, an official feed, or email/RSS alerts they provide), connect it
through `custom_json` or `rss`. To add a bespoke source, write `fetch_xxx(cfg, queries) -> list[Job]` in `job_fetcher.py`
and register it in `SOURCES`.

## 5. Matching & scoring
- Sub-scores (0-100): technical skills, experience, education, title relevance, location - from the AI, using the rubric in `prompts/matching_prompt.txt`.
- Final score = weighted average with `scoring.weights` (default 40/20/10/20/10), computed in code.
- Categories: **80-100 Highly relevant**, **60-79 Moderately relevant**, **<60 Not relevant** (`highly_relevant_min`, `moderately_relevant_min`).
- `notify_min_score` (default 60) decides what is emailed. Set it to 80 to receive only highly relevant jobs.
- Each email card shows the score, matching/missing skills, summary, and a **"why this score"** explanation.
- "Matching skills" are verified against your profile; the AI cannot credit you with skills you don't have.
- Cost control: jobs pass a free keyword prefilter first, only `max_jobs_to_score` (default 40) are scored per run, and already-scored jobs are remembered.

## 6. Duplicate detection & failure safety
- `data/seen_jobs.json` is committed back to the repo after every run (`if: always()`), so it survives separate runs and failures.
- Jobs are matched on normalized URL, `source:job_id`, and a title|company|location fingerprint (also catches the same job via two sources).
- **A job is recorded as `notified` only after SMTP confirms delivery.** If email fails, the run exits 1, the job stays un-notified, and the next run (or the workflow's automatic second attempt) retries it. Jobs scored below the threshold are recorded as `scored` so you don't pay to re-score them.
- AI failures on one job skip that job (retried next run); 5 consecutive failures abort scoring.
- `concurrency` prevents two runs from racing on the file. Entries older than `history.retention_days` (180) are pruned.

## 7. Testing
```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m pytest -q                                      # offline unit + pipeline tests

export NVIDIA_API_KEY=nvapi-...                           # real end-to-end, no email, no history changes
python -m src.main --dry-run                             # preview at out/digest_preview.html

export EMAIL_FROM=... EMAIL_TO=... EMAIL_PASSWORD=...    # real run (sends email, updates history)
python -m src.main
```

## 8. Deployment
Push to GitHub -> add secrets -> run the workflow manually once -> done. The schedule `0 */6 * * *` runs at 00:00, 06:00,
12:00, 18:00 **UTC** (05:30, 11:30, 17:30, 23:30 IST); edit the cron line to change it. Scheduled runs can be delayed a
few minutes under GitHub load. GitHub may pause schedules after 60 days without repository activity (re-enable in the
Actions tab); the bot's history commits normally count as activity. Typical run time is 1-3 minutes, well inside free-tier minutes.
Secrets are only passed to the run step as environment variables, are never echoed, and log output is redacted.

## 9. Troubleshooting
| Symptom | Fix |
|---|---|
| `Missing environment variables / GitHub Secrets` | Add the secret with the exact name (case-sensitive). |
| `Gmail rejected the login` | Use an App Password from the secondary account, with 2-Step Verification on. |
| `No resume ... and no saved profile` | Add a PDF/DOCX to `resume/` or commit `data/candidate_profile.json`. |
| Push step fails (403) | Settings -> Actions -> General -> Workflow permissions -> Read and write. |
| No emails arrive | Normal when nothing new matches. Lower `notify_min_score` / widen `preferences`, enable more sources, check spam. |
| Too many mediocre jobs | Raise `notify_min_score`, or tune `weights` and the prompt. |
| 401 / 404 from the AI API | Check `NVIDIA_API_KEY` and that the model name exists on build.nvidia.com. |
| 429 rate-limit messages | Normal on the free tier; the app waits and retries. Raise `request_delay_seconds` or lower `max_jobs_to_score` in `config.json`. |
#   a i - j o b - a l e r t  
 