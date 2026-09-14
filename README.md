# COLLEGE TO CAMPUS (C2C)

**Smart Quiz & Assessment Platform** — a beginner-friendly, responsive college quiz application using Flask, SQLite, HTML/CSS, vanilla JavaScript, and ReportLab.

## Requirements

- Python 3.10 or newer (CI uses Python 3.12).
- A modern browser with JavaScript and cookies enabled.
- Flask and ReportLab for the application; pytest for tests. Werkzeug (provided by Flask) handles password hashing.
- Uses MongoDB Atlas as the primary database (PyMongo), while retaining legacy SQLite/PostgreSQL code during migration.

## Installation

Clone this repository and open its directory. If reviewing the implementation before it is merged, check out `feat/c2c-quiz-mvp`.

```sh
python -m venv .venv
```

Activate the environment on macOS/Linux:

```sh
source .venv/bin/activate
```

Or Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

Install dependencies:

```sh
python -m pip install -r requirements.txt
```

### Configure the session secret

Generate a random key (never commit it):

```sh
python -c "import secrets; print(secrets.token_hex(32))"
```

Set `SECRET_KEY` to that output in your shell. On macOS/Linux, generate and set it in one command:

```sh
export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
```

On Windows PowerShell:

```powershell
$env:SECRET_KEY = python -c "import secrets; print(secrets.token_hex(32))"
```

Keep the same key across restarts if you want existing sessions to remain valid. The app refuses to start without a key of at least 32 characters. Configure it through your hosting environment for deployment. `.env` files are ignored by Git, but **not automatically loaded** by this application.

### Initialize the database and run

```sh
python -m flask --app app init-db
python -m flask --app app run
```

Open **http://127.0.0.1:5000**. Initialization creates `instance/c2c.sqlite3`, enables foreign keys, and preserves existing data. The database and its WAL files are excluded from Git.

Optional environment variables:

| Variable | Purpose |
| --- | --- |
| `SECRET_KEY` | Required random session-signing key, at least 32 characters |
| `MONGODB_URI` | Required connection string for MongoDB Atlas |
| `DATABASE_URL`| (Legacy) PostgreSQL connection string |
| `DATABASE_PATH` | (Legacy) SQLite path; default is `instance/c2c.sqlite3` |
| `COOKIE_SECURE` | Set to `1` when served over HTTPS; leave unset for local HTTP |

## Using C2C

### Create and share a quiz

1. Register a creator account with name, email, password, and confirmation.
2. Log in and choose **Create quiz**.
3. Enter a title, optional description, and a time limit of 1–1440 minutes.
4. Add any number of questions. Each requires text, four distinct options, and exactly one correct answer. Use **Add question** or **Remove** as needed.
5. Save. The dashboard shows a cryptographically random, ten-character quiz code and shareable link.
6. Use **Copy link** or copy the displayed URL manually. A quiz is immediately available after saving.
7. **Edit** stays available until the first student starts. Thereafter, questions and duration are immutable so results remain comparable.

### Students join and take the quiz

1. Open the shared link, or choose **Join a quiz** and enter the code.
2. Enter your name and roll number, then start. No student account is required.
3. Navigate between questions, select or clear answers, and monitor the answered count and countdown.
4. Answers save on each change. Wait for **All answers saved** before refreshing or leaving. A refresh preserves saved answers and the original deadline.
5. Submit manually or let the browser submit when time expires. The server independently enforces the deadline; late answers are ignored.
6. See score, total questions, correct/wrong/unanswered counts, percentage, and time taken immediately.

The same browser can resume an existing attempt by joining with the same quiz and roll number. A new browser cannot claim that attempt. Roll numbers are normalized using Unicode NFKC, whitespace removal, and case folding; for example, `CS 001` and `cs001` identify the same student within a quiz. A database uniqueness constraint enforces one attempt, not merely one submit-button click.

### Results, ranking, and PDF

The creator dashboard lists quiz title, code, questions, duration, participant count (including in-progress attempts), creation date, and sharing/results/report actions. Only the owning creator can see or download results.

- Scoring: correct = 1; wrong or unanswered = 0; no negative marking.
- Percentage = `correct / total * 100`, rounded to two decimal places.
- Time taken = server-measured seconds, capped at the quiz duration.
- Ranking sorts score descending, then elapsed time ascending. Exact ties use submission time and attempt ID for stable ordering.
- Results include rank, student name, roll number, score, percentage, correct, wrong, unanswered, elapsed time, and submission timestamp.
- **Download PDF** generates a landscape report in memory with quiz details, results, page numbers, and repeated table headings across pages.
- All dates and submission timestamps are UTC. An expired attempt records the deadline as its effective submission time.

## Project structure

```text
app.py                    Flask factory, routes, sessions, validation, ownership
db.py                     SQLite connection, init-db CLI
schema.sql                Tables, foreign keys, checks, unique constraints, indexes
quiz_service.py           Quiz validation, edits, attempts, grading, deadlines
reports.py                ReportLab report generation
requirements.txt          Python dependencies
.gitlab-ci.yml            Pytest and Python compilation checks
.gitignore                Local environments, secrets, databases, caches
README.md                 This guide
templates/                Jinja pages and shared results table
static/css/style.css      Responsive C2C theme
static/js/common.js       Copy-link and error-page interactions
static/js/quiz_builder.js  Add/remove questions, quiz serialization
static/js/quiz_player.js   Navigation, autosave, countdown, submission
tests/test_app.py         Regression tests and real Flask startup test
instance/                 Local runtime database (not tracked)
```

The application deliberately uses a few small Python modules, parameterized SQL, Jinja templates, and plain DOM APIs instead of additional frameworks.

## Database

| Table | Stores |
| --- | --- |
| `creators` | Name, unique normalized email, password hash, creation time |
| `quizzes` | Creator, unique random code, title, description, duration, creation time |
| `questions` | Quiz, order, text, four options, correct option |
| `attempts` | Quiz, browser token, student identity, normalized roll, start/deadline, stored result |
| `answers` | Attempt/question and selected option |

Foreign keys and composite keys prevent answers from referencing a question in a different quiz. `UNIQUE(quiz_id, normalized_roll)` prevents duplicate attempts. SQLite write transactions serialize joins, edits, answer updates, and final grading. Repeated submissions return the existing result without changing it.

## Routes

| Methods | Route | Purpose |
| --- | --- | --- |
| GET | `/` | Home |
| GET, POST | `/register`, `/login` | Creator accounts |
| POST | `/logout` | End creator session |
| GET | `/dashboard` | Owner's quizzes |
| GET, POST | `/quizzes/new` | Create quiz |
| GET, POST | `/quizzes/<id>/edit` | Edit before first attempt |
| GET, POST | `/join`, `/join/<code>` | Student entry |
| GET | `/attempts/<id>` | Resume/take quiz |
| POST | `/attempts/<id>/save` | Save answers as JSON |
| POST | `/attempts/<id>/submit` | Grade and finalize |
| GET | `/attempts/<id>/result` | Student's own result |
| GET | `/quizzes/<id>/results` | Creator results |
| GET | `/quizzes/<id>/leaderboard` | Creator rankings |
| GET | `/quizzes/<id>/pdf` | Creator PDF download |

POST requests require the session's CSRF token (hidden form field or `X-CSRF-Token` JSON request header). Student quiz HTML explicitly selects only question text/options, never the answer key. Secrets and browser tokens are not exposed in result tables or reports.

## Testing

```sh
python -m pytest -q
```

Tests create independent temporary SQLite databases and random session keys. They cover registration/login/logout, password hashing, validation, quiz codes, quiz creation/edit locking, joining, normalization, saved-answer refresh, grading/percentage/unanswered questions, duplicate constraints, repeated submissions, exact deadlines, abandoned attempts, invalid answers, student and creator authorization, CSRF, ranking, empty and multipage PDF reports, non-destructive initialization, and HTTP startup.

The startup test launches `python -m flask --app app run` on a temporary localhost port, requests the login page, and terminates the server. It verifies actual startup rather than only importing the app.

CI installs dependencies, compiles Python sources, runs the same tests, and publishes a JUnit report. Test pass/fail results must be read from the actual job, not inferred from the existence of tests.

## Local Wi-Fi access

Run on the host computer:

```sh
python -m flask --app app run --host=0.0.0.0
```

Find that computer's LAN IPv4 address using `ipconfig` (Windows) or your network settings. Students on the same trusted Wi-Fi can open `http://YOUR_LAN_IP:5000`. Allow TCP port 5000 through the computer's firewall only on the trusted local network. Campus Wi-Fi client isolation may block device-to-device access.

Open your creator dashboard through that LAN address before copying a link: share URLs use the current request host. A `127.0.0.1` or `localhost` link does not work from someone else's device. Never enable debug mode on a shared network. Keep the host awake and connected for the whole assessment.

## Deployment preparation and limitations

- Flask's built-in server is for development/local demonstrations, not production. Use a maintained WSGI server with the `app:create_app()` factory behind HTTPS. Configure the session secret in the hosting environment and set `COOKIE_SECURE=1`.
- Initialize the database on deployment, put SQLite on persistent local storage, and use its backup API for consistent backups. Do not put the database on ephemeral or shared network storage. SQLite is suitable for an MVP/small classroom, not a multi-instance high-concurrency deployment.
- Configure the proxy to preserve the intended public host for sharing. Review allowed hosts, trusted proxy configuration, deployment logging, request limits, and operational monitoring before public use.
- Roll numbers are self-declared and do not verify identity. There is no college SSO, email verification, password recovery, account administration, or login rate limiting in this MVP. Add those before an untrusted public rollout.
- Sessions require cookies. Clearing cookies or changing the secret loses student access to a reserved attempt; the creator still retains its result. Avoid simultaneous students sharing a browser session and simultaneous tabs editing the same attempt.
- The browser auto-submits while it is running. For closed/offline browsers, the server finalizes expired attempts when the student resumes or the creator opens results, rankings, or PDF. There is no background scheduler. Deadline enforcement still rejects late changes independently of this finalization timing.
- A disconnected browser cannot deliver answers. Only answers received before the deadline count; failed saves show a warning. Keep the page open and restore connectivity. Navigation does not reset the timer.
- The builder has no fixed question-count limit; requests have an 8 MiB safety limit. Extremely large quizzes and reports should be load-tested before classroom use.
- PDF reports use ReportLab's built-in Helvetica font. Some non-Latin scripts are not supported by that font; configure an appropriate embedded font before using multilingual PDF reports.
- Browser interactions and responsive styling require a manual desktop/mobile acceptance check; pytest does not automate a browser.

## Verification status

The repository includes runnable regression and startup tests. This implementation session has no local shell access; do not treat these checks as passed until a CI or local test run confirms them. If no pipeline starts automatically, select the feature branch when creating a pipeline in GitLab's Build > Pipelines page, or run `python -m pytest -q` locally.
