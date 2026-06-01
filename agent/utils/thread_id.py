"""Deterministic thread ID generation from MR identifiers."""

from __future__ import annotations

import hashlib


def generate_thread_id(project_path: str, mr_iid: int) -> str:
    """Generate a deterministic UUID-shaped thread ID from project path and MR IID.

    Same MR always produces the same thread ID so sandbox and state are reused.
    """
    key = f"gitlab-mr:{project_path}:{mr_iid}"
    h = hashlib.sha256(key.encode()).hexdigest()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"
