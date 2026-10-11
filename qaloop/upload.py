"""Shared file-upload path guard (issue #76).

Uploads are restricted to a *declared* upload directory: only absolute
paths that resolve inside that directory are accepted. Relative paths and
`..` escapes are rejected with ValueError so a rejected upload surfaces as
a FAILED step observation.
"""
from __future__ import annotations

import os


def check_upload_path(path: str, upload_dir: str | None) -> str:
    """Return the absolute path if it is inside upload_dir; raise ValueError."""
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ValueError("upload path must be an absolute path")
    allowed = os.path.abspath(upload_dir or "")
    want = os.path.abspath(path)
    if os.path.commonpath([want, allowed]) != allowed:
        raise ValueError(
            f"upload path {path!r} is outside the upload dir {allowed!r}")
    return want
