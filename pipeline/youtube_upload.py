"""YouTube upload of a chapter's generated videos.

Credentials come from ``pipeline/.env`` (see ``.env.example``):

    GOOGLE_DRIVE_CLIENT_ID / GOOGLE_DRIVE_CLIENT_SECRET   same OAuth client as Drive
    YOUTUBE_REFRESH_TOKEN   optional — skip the browser sign-in (servers/CI)
    YOUTUBE_PRIVACY         unlisted (default) / private / public — applied to new
                            uploads and, when changed, to videos already uploaded

The Cloud project needs "YouTube Data API v3" enabled and the ``youtube``
scope on its consent screen (``youtube.upload`` alone can't change an existing
video's privacy). Sign in once with ``python run.py youtube-login``
using the Google account that owns the channel; the token is cached in
``.youtube_token.json`` (git-ignored). It's a separate sign-in from Drive: a
different scope, and the channel may belong to a different account.

Two limits Google applies to every API project:

* Until the project passes Google's API audit, every upload is locked to
  *private* whatever YOUTUBE_PRIVACY says — links won't play for students.
* Each upload costs ~1,600 of the default 10,000 daily quota units (≈6 a day).
  ``QuotaExceeded`` is raised when it runs out; the pipeline stops uploading
  for the rest of the run and retries next time.

YouTube can't replace a video's media, so a changed local file becomes a new
video; an identical file (same md5) is never sent twice.

The Google client library is synchronous and not thread-safe; callers must
serialise calls (run.py does this with one asyncio.Lock + ``asyncio.to_thread``).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

import drive_upload

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")  # never overrides variables already set in the shell

TOKEN_FILE = HERE / ".youtube_token.json"
SCOPES = ["https://www.googleapis.com/auth/youtube"]  # upload + change privacy + channel name
PRIVACY_CHOICES = ("public", "unlisted", "private")
PRIVACY = (os.getenv("YOUTUBE_PRIVACY") or "unlisted").strip().lower()
CATEGORY_EDUCATION = "27"
CHUNK = 8 * 1024 * 1024  # resumable-upload chunk size


class YouTubeNotConfigured(RuntimeError):
    """No usable YouTube credentials — run `python run.py youtube-login`."""


class QuotaExceeded(RuntimeError):
    """Daily API quota or the channel's upload limit is used up."""


def configured() -> bool:
    """Signed in (token file or refresh token in .env) — cheap, no network."""
    return TOKEN_FILE.exists() or bool(os.getenv("YOUTUBE_REFRESH_TOKEN", "").strip())


def check_privacy() -> None:
    if PRIVACY not in PRIVACY_CHOICES:
        raise YouTubeNotConfigured(f"YOUTUBE_PRIVACY={PRIVACY!r} in pipeline/.env — "
                                   f"use one of: {', '.join(PRIVACY_CHOICES)}")


def login() -> None:
    """Interactive one-time sign-in; opens the browser."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    pair = drive_upload._client_id_secret()
    if not pair:
        raise YouTubeNotConfigured(
            "set GOOGLE_DRIVE_CLIENT_ID and GOOGLE_DRIVE_CLIENT_SECRET in pipeline/.env "
            "(the same OAuth client Drive uses)"
        )
    flow = InstalledAppFlow.from_client_config({"installed": {
        "client_id": pair[0],
        "client_secret": pair[1],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": drive_upload.TOKEN_URI,
        "redirect_uris": ["http://localhost"],
    }}, SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    _save_token(creds)


def _save_token(creds: Any) -> None:
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    os.chmod(TOKEN_FILE, 0o600)


def _credentials() -> Any:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    refresh = os.getenv("YOUTUBE_REFRESH_TOKEN", "").strip()
    pair = drive_upload._client_id_secret()
    if refresh and pair:
        # Headless: everything from .env, nothing written to disk.
        creds = Credentials(None, refresh_token=refresh, client_id=pair[0],
                            client_secret=pair[1], token_uri=drive_upload.TOKEN_URI,
                            scopes=SCOPES)
        creds.refresh(Request())
        return creds

    if not TOKEN_FILE.exists():
        raise YouTubeNotConfigured("not signed in to YouTube — run `python run.py youtube-login`")
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _save_token(creds)
        else:
            raise YouTubeNotConfigured("YouTube sign-in expired — run `python run.py youtube-login`")
    return creds


def links(video_id: str) -> dict[str, str]:
    return {
        "url": f"https://youtu.be/{video_id}",
        "watch_url": f"https://www.youtube.com/watch?v={video_id}",
        # Embeddable player — put this in an <iframe> in the app.
        "embed_url": f"https://www.youtube.com/embed/{video_id}",
    }


def _clean_title(title: str) -> str:
    # YouTube rejects < and > in titles and caps them at 100 characters.
    t = " ".join(title.replace("<", "").replace(">", "").split())
    return t if len(t) <= 100 else t[:99].rstrip() + "…"


def _quota_reason(err: Any) -> str | None:
    try:
        reasons = [e.get("reason") for e in json.loads(err.content)["error"]["errors"]]
    except (ValueError, KeyError, TypeError):
        return None
    return next((r for r in reasons if r in ("quotaExceeded", "uploadLimitExceeded",
                                             "rateLimitExceeded")), None)


class YouTube:
    def __init__(self) -> None:
        from googleapiclient.discovery import build

        check_privacy()
        self.svc = build("youtube", "v3", credentials=_credentials(), cache_discovery=False)

    def channel_title(self) -> str:
        res = self.svc.channels().list(part="snippet", mine=True).execute()
        items = res.get("items", [])
        if not items:
            raise YouTubeNotConfigured("this Google account has no YouTube channel — create one "
                                       "at youtube.com, then run `python run.py youtube-login`")
        return items[0]["snippet"]["title"]

    def upload(
        self,
        path: Path,
        title: str,
        description: str,
        md5: str,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> dict[str, Any]:
        """Upload one video and return its info (id + links).

        ``on_progress(sent_bytes, total_bytes)`` is called after every chunk
        (from this worker thread). Raises ``QuotaExceeded`` when YouTube says
        the daily quota / upload limit is used up.
        """
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload

        body = {
            "snippet": {"title": _clean_title(title), "description": description[:5000],
                        "categoryId": CATEGORY_EDUCATION},
            "status": {"privacyStatus": PRIVACY, "selfDeclaredMadeForKids": False},
        }
        media = MediaFileUpload(str(path), mimetype="video/mp4", resumable=True, chunksize=CHUNK)
        req = self.svc.videos().insert(part="snippet,status", body=body, media_body=media)
        total = path.stat().st_size
        response = None
        try:
            while response is None:
                status, response = req.next_chunk()
                if status is not None and on_progress is not None:
                    on_progress(status.resumable_progress, total)
        except HttpError as e:
            reason = _quota_reason(e)
            if reason:
                raise QuotaExceeded(f"YouTube {reason} — uploads resume on the next run") from e
            raise
        if on_progress is not None:
            on_progress(total, total)

        video_id = response["id"]
        stat = path.stat()
        return {
            "id": video_id,
            "name": path.name,
            "title": body["snippet"]["title"],
            # What we asked for (YOUTUBE_PRIVACY) vs what YouTube applied — the
            # latter stays "private" until the API project is audited.
            "requested_privacy": PRIVACY,
            "privacy": response.get("status", {}).get("privacyStatus", PRIVACY),
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "md5": md5,
            "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **links(video_id),
        }

    def set_privacy(self, video_id: str) -> str:
        """Set an uploaded video to YOUTUBE_PRIVACY (~50 quota units); returns what YouTube applied."""
        from googleapiclient.errors import HttpError

        try:
            res = self.svc.videos().update(part="status", body={
                "id": video_id,
                "status": {"privacyStatus": PRIVACY, "selfDeclaredMadeForKids": False},
            }).execute()
        except HttpError as e:
            reason = _quota_reason(e)
            if reason:
                raise QuotaExceeded(f"YouTube {reason} — privacy changes resume on the next run") from e
            raise
        return res.get("status", {}).get("privacyStatus", PRIVACY)
