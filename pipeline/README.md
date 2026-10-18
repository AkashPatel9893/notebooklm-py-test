# NCERT → NotebookLM batch pipeline

Drives the chapters listed in a Google Sheet through NotebookLM: uploads each
chapter PDF, generates study artifacts, downloads everything into a tidy
`Class / Subject / Chapter` folder tree, and mirrors that tree to Google Drive.
Fully resumable — interrupt and re-run any time; finished work is skipped,
failures are retried.

## Setup

```bash
# from the repo root
python3 -m venv .venv && source .venv/bin/activate
pip install -e .                          # the local notebooklm-py
pip install "notebooklm-py[browser]"      # browser login support
python -m playwright install chromium
pip install -r pipeline/requirements.txt  # Drive, .env and MongoDB libraries

cp pipeline/.env.example pipeline/.env    # then fill it in — see "Environment file"
```

## Environment file (`.env`)

All secrets and per-machine settings live in **`pipeline/.env`**. The committed
**`pipeline/.env.example`** is the template: every variable, documented, with no
values.

### Create it

```bash
cd pipeline
cp .env.example .env
chmod 600 .env          # readable only by you — it holds passwords
```

Then open `pipeline/.env` in your editor and fill in the values below. The file
is loaded automatically by `run.py` — no `export`/`source` needed. A variable
already set in your shell wins over the file (handy for one-off overrides:
`DRIVE_ROOT_FOLDER=Test python run.py drive`).

### Variables

| Variable | Required | What it is | Where to get it |
|----------|----------|------------|-----------------|
| `GOOGLE_DRIVE_CLIENT_ID` | for Drive | OAuth client ID, ends in `.apps.googleusercontent.com` | Cloud Console → Credentials → your *Desktop app* client ([steps](#2-google-drive)) |
| `GOOGLE_DRIVE_CLIENT_SECRET` | for Drive | OAuth client secret, starts with `GOCSPX-` | same dialog as the client ID |
| `GOOGLE_DRIVE_REFRESH_TOKEN` | no | lets a machine without a browser sign in to Drive | `refresh_token` in `.drive_token.json` after `drive-login` on your laptop |
| `DRIVE_ROOT_FOLDER` | no | top-level folder in *My Drive* that mirrors `output/` (default `Lernoverse NCERT`) | your choice |
| `MONGODB_URI` | for MongoDB | connection string of the phoenix database | `MONGODB_URI` in `Lernoverse/phoenix/.env` |
| `MONGODB_DB` | no | database name, if `MONGODB_URI` doesn't end in `/<database>` | the part after the last `/` in phoenix's URI |

Format rules (standard `.env`): one `NAME=value` per line, no spaces around
`=`, quotes optional (`MONGODB_URI="mongodb+srv://..."` works), `#` starts a
comment. Values with spaces don't need quotes (`DRIVE_ROOT_FOLDER=Lernoverse NCERT`).

A filled-in file looks like:

```bash
GOOGLE_DRIVE_CLIENT_ID=1234567890-abc123.apps.googleusercontent.com
GOOGLE_DRIVE_CLIENT_SECRET=GOCSPX-xxxxxxxxxxxxxxxxxxxx
DRIVE_ROOT_FOLDER=Lernoverse NCERT

MONGODB_URI=mongodb+srv://<user>:<password>@<cluster>.mongodb.net/<database>
MONGODB_DB=<database>
```

### Check it

```bash
python run.py check
```

Prints every variable (secrets shown only as *set / missing*, never the value)
and tests each connection — NotebookLM, Google Drive and MongoDB — without
changing anything:

```
 variable                     value
 GOOGLE_DRIVE_CLIENT_ID       1234567890-abc123.apps.googleusercontent.com
 GOOGLE_DRIVE_CLIENT_SECRET   set (35 chars, hidden)
 MONGODB_URI                  set (73 chars, hidden)
 MONGODB_DB                   hadwing

 service        result
 NotebookLM     ✓ ok  (Pro plan)
 Google Drive   ✓ ok  (you@gmail.com, folder "Lernoverse NCERT")
 MongoDB        ✓ ok  (database "hadwing")
```

Anything marked ✗ says what to fix. Common ones:

| Message | Fix |
|---------|-----|
| `.env not found` | `cp .env.example .env` inside `pipeline/` |
| `set GOOGLE_DRIVE_CLIENT_ID and …` | fill in both Drive variables, then `python run.py drive-login` |
| `not signed in to Drive` / `sign-in expired` | `python run.py drive-login` |
| `invalid_client` | client ID/secret typo, or the client isn't type *Desktop app* |
| `access_denied` during sign-in | add your Google account under *Test users* on the consent screen |
| MongoDB `ServerSelectionTimeoutError` | your IP isn't allowed in Atlas → *Network Access*, or no internet |
| MongoDB `bad auth` / `Authentication failed` | wrong user/password in `MONGODB_URI` — re-copy it from phoenix |
| NotebookLM auth / CSRF error | `notebooklm login` |

### Moving to a new machine

1. Clone the repo and do [Setup](#setup).
2. `cp pipeline/.env.example pipeline/.env` and fill it in — copy the values
   from your current machine's `.env` over a private channel (password
   manager), never through git, chat or email.
3. Either run `python run.py drive-login` there, or set
   `GOOGLE_DRIVE_REFRESH_TOKEN` to skip the browser.
4. `notebooklm login`, then `python run.py check` until everything is ✓.

## Credentials

The pipeline uses **three separate credentials**. None of them are committed to
git.

| What | Used for | Where it lives | How you set it up |
|------|----------|----------------|-------------------|
| NotebookLM sign-in | creating notebooks + generating artifacts | `~/.notebooklm/profiles/default/storage_state.json` | `notebooklm login` |
| Google Drive (OAuth) | uploading `output/` to Drive | `pipeline/.env` + `pipeline/.drive_token.json` | Google Cloud Console → `.env` → `python run.py drive-login` |
| MongoDB | saving content for the phoenix backend | `pipeline/.env` | copy `MONGODB_URI` from `Lernoverse/phoenix/.env` |

### 1. NotebookLM sign-in

A normal Google sign-in in a browser window; the session cookies are saved under
`~/.notebooklm/` (outside the repo).

```bash
notebooklm login                          # opens a browser — sign in to Google
notebooklm auth check --test --json       # expect "status": "ok", "token_fetch": true
```

Re-run `notebooklm login` whenever the pipeline reports an auth / CSRF error.

### 2. Google Drive

Drive uploads use an **OAuth "Desktop app" client** that you create once in your
own Google Cloud project. The pipeline asks only for the `drive.file` scope: it
can see and change **only the files and folders it created** — nothing else in
your Drive.

**a) Create the OAuth client** (one time, ~5 minutes, at
<https://console.cloud.google.com/>):

1. Top bar → project picker → **New project** (e.g. `lernoverse-pipeline`) → select it.
2. **APIs & Services → Library** → search **Google Drive API** → **Enable**.
3. **APIs & Services → OAuth consent screen** (a.k.a. *Google Auth Platform*):
   - User type **External** → fill in an app name and your email → save.
   - **Audience / Test users** → **Add users** → add the Google account whose
     Drive should receive the files.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   - Application type: **Desktop app** → name it → **Create**.
   - Copy the **Client ID** and **Client secret** from the dialog.

**b) Put them in `pipeline/.env`:**

```bash
GOOGLE_DRIVE_CLIENT_ID=1234567890-abc123.apps.googleusercontent.com
GOOGLE_DRIVE_CLIENT_SECRET=GOCSPX-xxxxxxxxxxxxxxxxxxxx
DRIVE_ROOT_FOLDER=Lernoverse NCERT        # top-level folder in "My Drive"
```

**c) Sign in once:**

```bash
cd pipeline
python run.py drive-login                 # opens the browser → pick the test-user account → Allow
```

Google will warn *"Google hasn't verified this app"* — that's expected for your
own Testing-mode app: click **Continue**. The sign-in is saved to
`pipeline/.drive_token.json` (git-ignored, readable only by you) and refreshed
automatically.

**d) Check it:**

```bash
python run.py drive                       # uploads everything already in output/
```

**Running on another machine / server (no browser):** copy the
`refresh_token` value from `.drive_token.json` into that machine's `.env` as
`GOOGLE_DRIVE_REFRESH_TOKEN=...` (along with the client ID and secret). The
pipeline then signs in from `.env` alone and writes nothing to disk.

> **Sign-in expires after 7 days** while the OAuth app is in *Testing* mode
> (Google's rule for unverified apps). When the log says the Drive sign-in
> expired, run `python run.py drive-login` again. To stop this, set the app's
> publishing status to **In production** on the consent-screen page.
> `drive.file` is a non-sensitive scope, so publishing shouldn't need Google's
> review; you may still see the "unverified app" screen when signing in.

If Drive isn't set up, uploads are skipped with a single warning — generation
and local downloads are unaffected.

### 3. MongoDB

The pipeline writes to the **same database as the phoenix backend**:

```bash
MONGODB_URI=mongodb+srv://<user>:<password>@<cluster>.mongodb.net/<database>
MONGODB_DB=<database>                     # only needed if the URI has no /<database>
```

Copy both from `Lernoverse/phoenix/.env`. This is the production password —
treat `pipeline/.env` like any other secret.

### Where secrets live — and what's git-ignored

| File | In git? | Contents |
|------|---------|----------|
| `pipeline/.env.example` | ✅ yes | every setting, documented, **no values** |
| `pipeline/.env` | ❌ ignored | your real client ID/secret, Mongo URI |
| `pipeline/.drive_token.json` | ❌ ignored | Drive sign-in (refresh token) |
| `pipeline/client_secret.json` | ❌ ignored | optional: the downloaded OAuth JSON, used if `.env` has no client ID |
| `~/.notebooklm/` | outside repo | NotebookLM session |

Never paste these values into chat, issues or commits. If one leaks: for Drive,
delete the OAuth client in Cloud Console (Credentials) and create a new one;
for MongoDB, rotate the database user's password in Atlas.

## Usage

```bash
cd pipeline

python run.py sync          # pull the sheet → progress.csv (adds new rows)
python run.py generate      # PDFs → notebooks → artifacts; downloads + Drive upload as each finishes
python run.py download      # catch-up / repair pass: fetch anything missing or broken
python run.py upload        # Drive upload, then MongoDB save — both in one go
python run.py drive         # only: upload anything in output/ not yet on Drive
python run.py mongo         # save content to the phoenix MongoDB (--dry-run to preview)
python run.py titles        # ask each notebook for the chapter's real name → chapter_title column
python run.py status        # progress table
python run.py check         # verify .env + NotebookLM / Drive / MongoDB access
python run.py all           # generate then download
python run.py drive-login   # one-time Drive sign-in
```

Useful flags on `generate` / `download` / `all`:

| Flag | Meaning |
|------|---------|
| `--concurrency N` | chapters processed in parallel (default 3) |
| `--max-attempts N` | retries per row before giving up (default 3; `0` = never give up) |
| `--only-failed` | only (re)process rows in `failed`/`partial` state |
| `--limit N` | cap the number of rows (handy for a test run) |
| `--artifacts a,b` | override which artifacts to make (default: `quiz,flashcards,mind_map,slide_deck,audio,cinematic_video,infographic`) |
| `--no-download-now` | during `generate`, don't download each artifact as it finishes |
| `--no-drive` | skip Google Drive uploads for this run |
| `--no-mongo` | skip saving to MongoDB for this run |
| `--usage-every SECONDS` | how often to re-print the live usage tables (default 120; `0` = off) |
| `-v` (before the command) | also show library + HTTP logs on the console |

Examples:

```bash
python run.py generate --limit 1 --artifacts quiz          # quick smoke test
python run.py generate --only-failed                       # retry just the failures
python run.py download --concurrency 5
python run.py -v generate --no-drive                       # debug, no uploads
```

## What gets tracked (`progress.csv`)

`progress.csv` mirrors the sheet (Class/Subject/Book/Chapter/PDF URL/Index) and
adds tracking columns — it's the single source of truth, safe to re-upload to
Google Sheets:

- `notebook_id`, `source_id` — the NotebookLM notebook + uploaded source per chapter
- `chapter_title` — the chapter's real name, asked from the notebook once its source is ready (used as the title in MongoDB)
- `gen_status`, `dl_status` — overall state: `pending` / `done` / `partial` / `rate_limited` / `failed`
- `gen_attempts`, `dl_attempts`, `gen_error`, `dl_error` — retry bookkeeping
- per artifact: `<name>_id`, `<name>_gen`, `<name>_dl` (e.g. `quiz_id`, `slide_deck_gen`, `cinematic_video_dl`)

## Output layout

```
output/                                   Google Drive: My Drive / Lernoverse NCERT /
  Class 11/                                 (same folders, same files)
    Chemistry/
      Chapter 1/
        source.pdf
        quiz.json        quiz.html        quiz.md
        flashcards.json  flashcards.html  flashcards.md
        mind_map.json
        slides.pdf       slides.pptx
        audio.m4a                         # AAC in MP4 — not MP3
        cinematic_video.mp4
        infographic.png
        drive.json                        # local only: Drive id + links per file
        mongo.json                        # local only: last MongoDB save (chapter _id, counts)
```

Extra formats are set by `DOWNLOAD_FORMATS` at the top of `run.py`; which files
go to Drive by `DRIVE_EXTS` (default: all). `drive.json` holds, per file,
`view_url` (open in browser), `preview_url` (embed in an `<iframe>` — use this
for audio/video in the app) and `download_url` (raw file; unreliable for large
files).

## MongoDB (phoenix backend)

After each chapter's files reach Drive, the chapter is saved into the phoenix
database using the exact shapes of `phoenix/app/models/ncert/*`:

| Collection | What |
|------------|------|
| `ncertclasses` → `ncertsubjects` → `ncertbooks` → `ncertchapters` | the hierarchy; chapter title is `chapter_title` from `progress.csv` (falls back to the mind map's root node) |
| `ncertquizquestions` | multiple-choice questions only (`answer_options`, exactly one correct, + `hint`); other question types NotebookLM produces are skipped |
| `ncertflashcards` | every card (`card_type` 1 — NotebookLM doesn't send a type) |
| `ncertmindmaps` | the tree, converted to `{title, root: {id, text, level, expanded, children}}` |
| `ncertresources` | audio, video, slides PDF, chapter PDF (`url` = Drive `preview_url`), plus quiz / flashcard / mind-map entries with counts |

Re-running is safe: records are matched on natural keys (class number, subject
name, question text, card front…), so `_id`s never change; class/subject/book/
chapter are only filled in on first creation, so edits made in the backend are
kept. Pipeline-made resources carry `meta.source: "notebooklm-pipeline"`.
Not saved (no place in the models): `slides.pptx` and the `.html` / `.md`
exports — they're on Drive only. Bump `SCHEMA_VERSION` in `mongo_sync.py`
when changing the saved shapes, so every chapter re-syncs.

## Notes

- One notebook per (class, subject, chapter); the chapter PDF is uploaded as its source.
- Chapters run concurrently; within a chapter artifacts are made one after another.
- Every artifact draws on NotebookLM's shared usage budget (5-hour + weekly
  windows). The run starts with a usage table, re-prints it every 2 minutes, and
  skips artifacts Google says there's no quota for — they're marked `⏸` and
  retried on the next run. `notebooklm usage --categories` shows the same data.
- A failed artifact is retried *in place* (NotebookLM's "Retry"), so the
  notebook never collects duplicates.
- Downloads are written to a temp file, checked (PDF/MP4/PNG headers, valid
  JSON), then renamed — a broken or partial file never appears under the real name.
- The NCERT host (`ncert.nic.in`) resets the bare apex domain; the downloader
  automatically retries on `www.ncert.nic.in`.
- Configurable defaults (sheet ID, artifact set, formats, timeouts, paths) live
  at the top of `run.py`; secrets live in `pipeline/.env`.
