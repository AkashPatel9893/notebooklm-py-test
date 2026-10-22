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
  for the rest of the run and retries next time. The API doesn't report
  what's left, so every call is counted locally (``.youtube_quota.json``,
  per Pacific-time day like Google's) — see ``quota()`` — and checked
  *before* it is sent: a call that wouldn't fit is never made, and an
  upload only starts when its playlist step still fits after it.
* Short bursts trip ``rateLimitExceeded``: calls are spaced out
  (``YOUTUBE_CALL_GAP`` / ``YOUTUBE_UPLOAD_GAP`` seconds), and if Google still
  says slow down, ``RateLimited`` is raised so the caller waits and retries.

YouTube can't replace a video's media, so a changed local file becomes a new
video; an identical file (same md5) is never sent twice.

The Google client library is synchronous and not thread-safe; callers must
serialise calls (run.py does this with one asyncio.Lock + ``asyncio.to_thread``).
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

import drive_upload

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")  # never overrides variables already set in the shell

TOKEN_FILE = HERE / ".youtube_token.json"
PLAYLISTS_FILE = HERE / ".youtube_playlists.json"  # playlist title → id, saves list calls
QUOTA_FILE = HERE / ".youtube_quota.json"  # units this pipeline spent today (Pacific time)
SCOPES = ["https://www.googleapis.com/auth/youtube"]  # upload + change privacy + channel name
PRIVACY_CHOICES = ("public", "unlisted", "private")
PRIVACY = (os.getenv("YOUTUBE_PRIVACY") or "unlisted").strip().lower()
CATEGORY_EDUCATION = "27"
CHUNK = 8 * 1024 * 1024  # resumable-upload chunk size
# Retries per chunk / request on network errors and 5xx, with exponential
# backoff (~1 min total) — rides out a short connection drop mid-upload.
RETRIES = 6
# Units per call (developers.google.com/youtube/v3/determine_quota_cost).
DAILY_QUOTA = int(os.getenv("YOUTUBE_DAILY_QUOTA") or 10_000)
COST_UPLOAD, COST_WRITE, COST_LIST = 1600, 50, 1
QUOTA_TZ = ZoneInfo("America/Los_Angeles")  # Google resets the quota at midnight PT
# Units an upload must leave free for what follows it (playlist insert + lookups),
# so the pipeline never uploads a video it then can't put in its playlist.
UPLOAD_RESERVE = COST_WRITE + 10
# Minimum seconds between any two API calls, and between two upload starts —
# keeps the pipeline well under YouTube's per-minute burst limits.
CALL_GAP = float(os.getenv("YOUTUBE_CALL_GAP") or 1)
UPLOAD_GAP = float(os.getenv("YOUTUBE_UPLOAD_GAP") or 60)
_last_call = _last_upload = 0.0


class YouTubeNotConfigured(RuntimeError):
    """No usable YouTube credentials — run `python run.py youtube-login`."""


class QuotaExceeded(RuntimeError):
    """Daily API quota or the channel's upload limit is used up."""


class RateLimited(RuntimeError):
    """YouTube's short-term rate limit — fine again after a wait."""


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


def retryable(err: BaseException) -> bool:
    """Worth trying again in a minute: network trouble, 5xx, 408/429 — not bad
    requests, auth, or quota (those fail the same way until something changes)."""
    from googleapiclient.errors import HttpError

    if isinstance(err, RateLimited):
        return True
    if isinstance(err, (QuotaExceeded, YouTubeNotConfigured)):
        return False
    if isinstance(err, HttpError):
        return err.resp.status in (408, 429) or err.resp.status >= 500
    return isinstance(err, (OSError, TimeoutError, ConnectionError)) or \
        type(err).__module__.startswith(("httplib2", "ssl", "socket", "http.client"))


def _quota_reason(err: Any) -> str | None:
    try:
        reasons = [e.get("reason") for e in json.loads(err.content)["error"]["errors"]]
    except (ValueError, KeyError, TypeError):
        return None
    return next((r for r in reasons if r in ("quotaExceeded", "uploadLimitExceeded",
                                             "rateLimitExceeded", "userRateLimitExceeded")), None)


def _today() -> str:
    return datetime.now(QUOTA_TZ).date().isoformat()


def quota() -> dict[str, Any]:
    """This pipeline's quota use today: used, limit, left, uploads_left, resets_at (UTC)."""
    try:
        data = json.loads(QUOTA_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    used = data.get("used", 0) if data.get("day") == _today() else 0
    left = max(0, DAILY_QUOTA - used)
    midnight = (datetime.now(QUOTA_TZ) + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return {"used": used, "limit": DAILY_QUOTA, "left": left,
            "uploads_left": left // (COST_UPLOAD + COST_WRITE),
            "resets_at": midnight.astimezone(timezone.utc)}


def _charge(units: int, exhausted: bool = False) -> None:
    used = quota()["used"]
    used = max(used, DAILY_QUOTA) if exhausted else used + units
    QUOTA_FILE.write_text(json.dumps({"day": _today(), "used": used}), encoding="utf-8")


def _spend(units: int, reserve: int = 0, upload: bool = False) -> None:
    """Before a call: refuse it locally when it (plus ``reserve``) doesn't fit in
    today's quota, wait out the call/upload spacing, then count it."""
    global _last_call, _last_upload
    q = quota()
    if q["left"] < units + reserve:
        raise QuotaExceeded(f"only {q['left']} of {q['limit']} YouTube quota units left today, "
                            f"this needs {units + reserve} — not sent")
    now = time.monotonic()
    wait = max(_last_call + CALL_GAP, (_last_upload + UPLOAD_GAP) if upload else 0) - now
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()
    if upload:
        _last_upload = _last_call
    _charge(units)


def _quota_error(e: Any, what: str) -> QuotaExceeded | RateLimited | None:
    """QuotaExceeded for a daily quota/limit HttpError (and marks today used up),
    RateLimited for a short-term one, else None."""
    reason = _quota_reason(e)
    if not reason:
        return None
    if reason in ("rateLimitExceeded", "userRateLimitExceeded"):  # a burst, not the budget
        return RateLimited(f"YouTube {reason} — slowing down")
    _charge(0, exhausted=True)
    return QuotaExceeded(f"YouTube {reason} — {what} resume on the next run")


class YouTube:
    def __init__(self) -> None:
        from googleapiclient.discovery import build

        check_privacy()
        self.svc = build("youtube", "v3", credentials=_credentials(), cache_discovery=False)

    def channel_title(self) -> str:
        _spend(COST_LIST)
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
        tags: list[str] | None = None,
        made_for_kids: bool = False,
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
                        "categoryId": CATEGORY_EDUCATION, "tags": tags or []},
            "status": {"privacyStatus": PRIVACY, "selfDeclaredMadeForKids": made_for_kids},
        }
        media = MediaFileUpload(str(path), mimetype="video/mp4", resumable=True, chunksize=CHUNK)
        req = self.svc.videos().insert(part="snippet,status", body=body, media_body=media)
        total = path.stat().st_size
        response = None
        # Google charges the attempt, finished or not.
        _spend(COST_UPLOAD, reserve=UPLOAD_RESERVE, upload=True)
        try:
            while response is None:
                status, response = req.next_chunk(num_retries=RETRIES)
                if status is not None and on_progress is not None:
                    on_progress(status.resumable_progress, total)
        except HttpError as e:
            if q := _quota_error(e, "uploads"):
                raise q from e
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

    def _playlists(self) -> dict[str, str]:
        try:
            return json.loads(PLAYLISTS_FILE.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _playlist_id(self, title: str, description: str, refresh: bool = False) -> str:
        """The channel's playlist called ``title``; created when it doesn't exist."""
        cache = {} if refresh else self._playlists()
        if title not in cache:
            req = self.svc.playlists().list(part="snippet", mine=True, maxResults=50)
            while req is not None:
                res = self._call(req, COST_LIST, "playlists")
                for p in res.get("items", []):
                    cache.setdefault(p["snippet"]["title"], p["id"])
                req = self.svc.playlists().list_next(req, res)
        if title not in cache:
            res = self._call(self.svc.playlists().insert(part="snippet,status", body={
                "snippet": {"title": _clean_title(title), "description": description[:5000]},
                "status": {"privacyStatus": PRIVACY},
            }), COST_WRITE, "playlists")
            cache[title] = res["id"]
        PLAYLISTS_FILE.write_text(json.dumps(cache, indent=2), encoding="utf-8")
        return cache[title]

    def _call(self, req: Any, units: int, what: str) -> Any:
        """Execute one request, counting its quota and turning quota errors into QuotaExceeded."""
        from googleapiclient.errors import HttpError

        _spend(units)
        try:
            return req.execute(num_retries=RETRIES)
        except HttpError as e:
            if q := _quota_error(e, what):
                raise q from e
            raise

    def _playlist_items(self, playlist_id: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        req = self.svc.playlistItems().list(part="snippet,status", playlistId=playlist_id,
                                            maxResults=50)
        while req is not None:
            res = self._call(req, COST_LIST, "playlists")
            items += res.get("items", [])
            req = self.svc.playlistItems().list_next(req, res)
        return items

    def add_to_playlist(
        self,
        video_id: str,
        title: str,
        description: str,
        key: Any = None,
        key_of: Callable[[dict[str, Any]], Any] | None = None,
    ) -> tuple[str, str]:
        """Put a video in the playlist ``title`` (~50 quota units + 1 to read its order).

        With ``key`` (this video's sort key) and ``key_of`` (playlist item snippet →
        its key, or None when unknown) the video goes in before the first item
        that sorts after it, so the playlist reads in chapter order; otherwise
        it's appended. Returns (playlist id, playlist item id).
        """
        from googleapiclient.errors import HttpError

        def insert(pid: str) -> dict[str, Any]:
            snippet: dict[str, Any] = {
                "playlistId": pid, "resourceId": {"kind": "youtube#video", "videoId": video_id}}
            if key is not None and key_of is not None:
                keys = [key_of(it["snippet"]) for it in self._playlist_items(pid)]
                after = [i for i, k in enumerate(keys) if k is not None and k > key]
                if after:
                    snippet["position"] = after[0]
            return self._call(self.svc.playlistItems().insert(part="snippet", body={"snippet": snippet}),
                              COST_WRITE, "playlists")

        pid = self._playlist_id(title, description)
        try:
            item = insert(pid)
        except HttpError as e:
            if e.resp.status != 404:  # playlist deleted on YouTube — find/create it again
                raise
            pid = self._playlist_id(title, description, refresh=True)
            item = insert(pid)
        return pid, item["id"]

    def remove_from_playlist(self, item_id: str) -> None:
        """Take one entry out of a playlist (~50 units); already gone is fine."""
        from googleapiclient.errors import HttpError

        try:
            self._call(self.svc.playlistItems().delete(id=item_id), COST_WRITE, "playlists")
        except HttpError as e:
            if e.resp.status != 404:
                raise

    def set_privacy(self, video_id: str, made_for_kids: bool = False,
                    privacy: str | None = None) -> str:
        """Set a video's privacy (YOUTUBE_PRIVACY unless given; ~50 units); returns what YouTube applied."""
        privacy = privacy or PRIVACY
        res = self._call(self.svc.videos().update(part="status", body={
            "id": video_id,
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": made_for_kids},
        }), COST_WRITE, "privacy changes")
        return res.get("status", {}).get("privacyStatus", privacy)

    def long_uploads(self) -> str:
        """Channel's longUploadsStatus: 'allowed' lets it upload over 15 minutes;
        'eligible'/'disallowed' mean not yet — phone-verify at youtube.com/verify (~1 unit)."""
        if not hasattr(self, "_long"):
            res = self._call(self.svc.channels().list(part="status", mine=True), COST_LIST, "uploads")
            items = res.get("items", [])
            self._long = items[0].get("status", {}).get("longUploadsStatus", "") if items else ""
        return self._long

    def video_states(self, video_ids: list[str]) -> dict[str, tuple[str, str]]:
        """{id: (state, reason)} — state is YouTube's uploadStatus (uploaded = still
        processing, processed, rejected, failed, deleted), or 'missing' when the
        video is gone. ~1 unit per 50 ids."""
        out: dict[str, tuple[str, str]] = {}
        for i in range(0, len(video_ids), 50):
            chunk = video_ids[i:i + 50]
            res = self._call(self.svc.videos().list(part="status,processingDetails",
                                                    id=",".join(chunk)), COST_LIST, "uploads")
            for v in res.get("items", []):
                st, pd = v.get("status", {}), v.get("processingDetails", {})
                state = st.get("uploadStatus", "")
                if pd.get("processingStatus") in ("failed", "terminated") and state != "processed":
                    state = "failed"
                out[v["id"]] = (state, st.get("rejectionReason") or st.get("failureReason")
                                or pd.get("processingFailureReason") or "")
            for vid in chunk:
                out.setdefault(vid, ("missing", ""))
        return out

    def delete(self, video_id: str) -> None:
        """Delete a video (~50 units); already gone is fine."""
        from googleapiclient.errors import HttpError

        try:
            self._call(self.svc.videos().delete(id=video_id), COST_WRITE, "deletes")
        except HttpError as e:
            if e.resp.status != 404:
                raise

    def find_upload(self, title: str, skip_ids: set[str]) -> dict[str, Any] | None:
        """A video among the channel's 50 newest uploads titled ``title`` (and not in
        ``skip_ids``) — one an earlier attempt sent but never recorded (~2 units)."""
        if not hasattr(self, "_uploads"):
            res = self._call(self.svc.channels().list(part="contentDetails", mine=True),
                             COST_LIST, "uploads")
            items = res.get("items", [])
            self._uploads = items[0]["contentDetails"]["relatedPlaylists"]["uploads"] if items else ""
        if not self._uploads:
            return None
        res = self._call(self.svc.playlistItems().list(
            part="snippet,status", playlistId=self._uploads, maxResults=50), COST_LIST, "uploads")
        want = _clean_title(title)
        for it in res.get("items", []):
            vid = it["snippet"]["resourceId"]["videoId"]
            if it["snippet"]["title"] == want and vid not in skip_ids:
                return {"id": vid, "title": want,
                        "privacy": it.get("status", {}).get("privacyStatus", PRIVACY)}
        return None
