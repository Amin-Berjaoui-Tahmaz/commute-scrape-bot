from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


def _looks_generated(name: str) -> bool:
    if not name:
        return False

    stem = Path(name).stem
    suffix = Path(name).suffix.lower()
    if suffix in {".pdf", ".png", ".jpg", ".jpeg", ".zip", ".txt", ".csv"}:
        return False

    if len(stem) >= 20 and re.fullmatch(r"[A-Za-z0-9]+", stem):
        return True

    return False


def _sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:\\"/|?*\x00-\x1f]', "_", name).strip(" .")
    return cleaned or "download"


def build_download_target(download_dir: Path, suggested_filename: str, download_url: str) -> Path:
    """Choose a stable filename for a download.

    When the browser provides only a generic or generated name (for example a
    long alphanumeric string), prefer the basename from the URL path if it looks
    more meaningful. This keeps PDF downloads readable in the local downloads
    folder.
    """
    parsed = urlparse(download_url or "")
    url_name = Path(unquote(parsed.path)).name if parsed.path else ""

    candidate = suggested_filename or ""
    if url_name and (_looks_generated(candidate) or not candidate):
        candidate = url_name

    filename = _sanitize_filename(candidate or url_name or "download")
    return download_dir / filename


def save_download_to_path(download: Any, target: Path) -> Path:
    """Persist a Playwright download to a concrete location on disk."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    download.save_as(target)
    return target
