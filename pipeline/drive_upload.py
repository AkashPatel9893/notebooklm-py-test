"""Google Drive mirror of the pipeline's output/ folder.

Credentials come from ``pipeline/.env`` (see ``.env.example``):

    GOOGLE_DRIVE_CLIENT_ID / GOOGLE_DRIVE_CLIENT_SECRET   OAuth "Desktop app" client
    GOOGLE_DRIVE_REFRESH_TOKEN   optional — skip the browser sign-in (servers/CI)
    DRIVE_ROOT_FOLDER            top-level Drive folder (default "Lernoverse NCERT")

Without a refresh token in .env, sign in once with ``python run.py drive-login``;
the resulting token is cached in ``.drive_token.json`` (git-ignored). A
downloaded ``client_secret.json`` still works as a fallback for the client id.
Scope is ``drive.file``: the pipeline can only see and change files and folders
it created itself — nothing else in your Drive.

Files land in a folder tree mirroring ``output/`` file-for-file:

    output/Class 11/Chemistry/Chapter 1/quiz.json
      →  My Drive / Lernoverse NCERT / Class 11 / Chemistry / Chapter 1 / quiz.json

and are shared "anyone with the link can view", so the links work for students
without signing in.

The Google client library is synchronous and not thread-safe; callers must
serialise calls (run.py does this with one asyncio.Lock + ``asyncio.to_thread``).
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")  # never overrides variables already set in the shell

CLIENT_SECRET = HERE / "client_secret.json"
TOKEN_FILE = HERE / ".drive_token.json"
SCOPES = ["https://www.googleapis.com/auth/drive.file"]
TOKEN_URI = "https://oauth2.googleapis.com/token"
ROOT_FOLDER = os.getenv("DRIVE_ROOT_FOLDER") or "Lernoverse NCERT"
FOLDER_MIME = "application/vnd.google-apps.folder"
CHUNK = 8 * 1024 * 1024  # resumable-upload chunk size

mimetypes.add_type("audio/mp4", ".m4a")
mimetypes.add_type("text/markdown", ".md")


class DriveNotConfigured(RuntimeError):
    """No usable Drive credentials — run `python run.py drive-login`."""


def _client_id_secret() -> tuple[str, str] | None:
    cid = os.getenv("GOOGLE_DRIVE_CLIENT_ID", "").strip()
    secret = os.getenv("GOOGLE_DRIVE_CLIENT_SECRET", "").strip()
    return (cid, secret) if cid and secret else None


def login() -> None:
    """Interactive one-time sign-in; opens the browser."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    pair = _client_id_secret()
    if pair:
        flow = InstalledAppFlow.from_client_config({"installed": {
            "client_id": pair[0],
            "client_secret": pair[1],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": TOKEN_URI,
            "redirect_uris": ["http://localhost"],
        }}, SCOPES)
    elif CLIENT_SECRET.exists():
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
    else:
        raise DriveNotConfigured(
            "set GOOGLE_DRIVE_CLIENT_ID and GOOGLE_DRIVE_CLIENT_SECRET in pipeline/.env "
            "(OAuth 'Desktop app' client from Google Cloud Console)"
        )
    creds = flow.run_local_server(port=0, prompt="consent")
    _save_token(creds)


def _save_token(creds: Any) -> None:
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    os.chmod(TOKEN_FILE, 0o600)


def _credentials() -> Any:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    refresh = os.getenv("GOOGLE_DRIVE_REFRESH_TOKEN", "").strip()
    pair = _client_id_secret()
    if refresh and pair:
        # Headless: everything from .env, nothing written to disk.
        creds = Credentials(None, refresh_token=refresh, client_id=pair[0],
                            client_secret=pair[1], token_uri=TOKEN_URI, scopes=SCOPES)
        creds.refresh(Request())
        return creds

    if not TOKEN_FILE.exists():
        hint = "" if pair else " (and set GOOGLE_DRIVE_CLIENT_ID/SECRET in pipeline/.env)"
        raise DriveNotConfigured(f"not signed in to Drive — run `python run.py drive-login`{hint}")
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _save_token(creds)
        else:
            raise DriveNotConfigured("Drive sign-in expired — run `python run.py drive-login`")
    return creds


def file_md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def links(file_id: str) -> dict[str, str]:
    """The three useful URL shapes for one Drive file."""
    return {
        # Opens Google's viewer page (works for everything, any size).
        "view_url": f"https://drive.google.com/file/d/{file_id}/view",
        # Embeddable player/viewer — put this in an <iframe> in the app.
        "preview_url": f"https://drive.google.com/file/d/{file_id}/preview",
        # Raw file. Fine for small files; large ones (~100 MB+) get a
        # virus-scan warning page instead, and heavy use hits download quotas.
        "download_url": f"https://drive.google.com/uc?export=download&id={file_id}",
    }


def _q(value: str) -> str:
    """Escape a value for a Drive search query string literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


class Drive:
    def __init__(self) -> None:
        from googleapiclient.discovery import build

        self.svc = build("drive", "v3", credentials=_credentials(), cache_discovery=False)
        self._folders: dict[tuple[str, ...], str] = {}

    def _find(self, name: str, parent: str | None, folder: bool) -> dict[str, Any] | None:
        q = [f"name = '{_q(name)}'", "trashed = false"]
        q.append(f"mimeType {'=' if folder else '!='} '{FOLDER_MIME}'")
        q.append(f"'{parent}' in parents" if parent else "'root' in parents")
        res = self.svc.files().list(
            q=" and ".join(q), spaces="drive",
            fields="files(id, name, md5Checksum, size)", pageSize=10,
        ).execute()
        files = res.get("files", [])
        return files[0] if files else None

    def folder(self, parts: tuple[str, ...]) -> str:
        """Find-or-create ROOT_FOLDER/parts[0]/parts[1]/... and return its id."""
        path = (ROOT_FOLDER, *parts)
        parent: str | None = None
        for i in range(1, len(path) + 1):
            sub = path[:i]
            if sub in self._folders:
                parent = self._folders[sub]
                continue
            found = self._find(sub[-1], parent, folder=True)
            if found is None:
                body: dict[str, Any] = {"name": sub[-1], "mimeType": FOLDER_MIME}
                if parent:
                    body["parents"] = [parent]
                found = self.svc.files().create(body=body, fields="id").execute()
            parent = self._folders[sub] = found["id"]
        assert parent is not None
        return parent

    def upload(
        self,
        path: Path,
        parts: tuple[str, ...],
        md5: str | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> dict[str, Any]:
        """Upload (or update in place) one file, share it by link, return its info.

        If a file with the same name and identical content already exists in the
        target folder, nothing is sent. ``on_progress(sent_bytes, total_bytes)``
        is called after every chunk (called from this worker thread).
        """
        from googleapiclient.http import MediaFileUpload

        md5 = md5 or file_md5(path)
        parent = self.folder(parts)
        existing = self._find(path.name, parent, folder=False)
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

        if existing and existing.get("md5Checksum") == md5:
            file_id, sent = existing["id"], False
        else:
            media = MediaFileUpload(str(path), mimetype=mime, resumable=True, chunksize=CHUNK)
            if existing:
                req = self.svc.files().update(fileId=existing["id"], media_body=media, fields="id")
            else:
                req = self.svc.files().create(
                    body={"name": path.name, "parents": [parent]}, media_body=media, fields="id")
            response = None
            total = path.stat().st_size
            while response is None:
                status, response = req.next_chunk()
                if status is not None and on_progress is not None:
                    on_progress(status.resumable_progress, total)
            if on_progress is not None:
                on_progress(total, total)
            file_id, sent = response["id"], True

        # "Anyone with the link can view" — idempotent on Drive's side.
        self.svc.permissions().create(
            fileId=file_id, body={"type": "anyone", "role": "reader"}, fields="id",
        ).execute()

        stat = path.stat()
        return {
            "id": file_id,
            "name": path.name,
            "mime": mime,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "md5": md5,
            "uploaded": sent,
            "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **links(file_id),
        }
