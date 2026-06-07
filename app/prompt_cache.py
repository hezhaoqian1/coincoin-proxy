from __future__ import annotations

import hashlib
from typing import Any


def build_claude_code_prompt_cache_key(
    user: Any,
    api_key_id: str,
    display_model: str,
    public_model: Any,
    *,
    effective_backend_model: str = "",
) -> str:
    metadata = getattr(public_model, "metadata", None) or {}
    if metadata.get("compat_family") != "claude-code":
        return ""
    seed = (
        f"{getattr(user, 'id', '')}:"
        f"{api_key_id or ''}:"
        f"{display_model or ''}:"
        f"{effective_backend_model or ''}"
    )
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]
    return f"cc-{digest}"
