"""Keep inline images in a Responses request within an upstream's image budget.

Some relays cap the base64 images one request may carry. The sub2api "image
relay" allows 20 MiB per image and 32 MiB / 20 images per request, counted on
decoded bytes, and rejects anything larger. Codex resends the whole history on
every turn, so a screenshot-heavy session eventually crosses that cap and every
later turn fails the same way.

When a channel rejects a request like that, its limits are parsed from the
error and remembered, and the oldest inline images are replaced with a short
note until the request fits. The newest image is never dropped: if it alone
does not fit, the request is left as is and the upstream error goes through.
"""

from __future__ import annotations

import json
import re
import time
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

OMITTED_IMAGE_NOTE = (
    "[An earlier image was omitted here: the conversation's inline images "
    "exceeded the upstream request size limit. View it again if it is still needed.]"
)

# How long a channel's announced limits are applied before being re-learned.
LEARNED_BUDGET_TTL_SECONDS = 6 * 3600

# Images are dropped in multiples of this many, so the trimmed history stays the
# same for several turns and keeps hitting the upstream prompt cache instead of
# changing its prefix every time one more image is added.
DROP_STEP = 4

# Stay this far under the announced byte limits.
_SAFETY_MARGIN_BYTES = 64 * 1024

_TOTAL_LIMIT_RE = re.compile(r"inline images exceed the configured (\d+) MiB", re.IGNORECASE)
_COUNT_LIMIT_RE = re.compile(r"at most (\d+) inline images", re.IGNORECASE)
_IMAGE_LIMIT_RE = re.compile(r"inline image exceeds the configured (\d+) MiB", re.IGNORECASE)

_TOOL_OUTPUT_TYPES = frozenset({"function_call_output", "custom_tool_call_output"})


@dataclass(frozen=True)
class InlineImageBudget:
    max_total_bytes: Optional[int] = None
    max_images: Optional[int] = None
    max_image_bytes: Optional[int] = None

    def merge(self, other: "InlineImageBudget") -> "InlineImageBudget":
        """The stricter of two budgets, field by field."""

        def _min(a: Optional[int], b: Optional[int]) -> Optional[int]:
            if a is None:
                return b
            if b is None:
                return a
            return min(a, b)

        return InlineImageBudget(
            max_total_bytes=_min(self.max_total_bytes, other.max_total_bytes),
            max_images=_min(self.max_images, other.max_images),
            max_image_bytes=_min(self.max_image_bytes, other.max_image_bytes),
        )


def _error_text(error: Any) -> str:
    if isinstance(error, (bytes, bytearray)):
        return bytes(error).decode("utf-8", errors="replace")
    if isinstance(error, str):
        return error
    try:
        return json.dumps(error, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(error or "")


def parse_inline_image_budget_error(error: Any) -> Optional[InlineImageBudget]:
    """The limits an upstream image-budget rejection announces, if it is one."""
    text = _error_text(error)
    total = _TOTAL_LIMIT_RE.search(text)
    count = _COUNT_LIMIT_RE.search(text)
    single = _IMAGE_LIMIT_RE.search(text)
    if not (total or count or single):
        return None
    return InlineImageBudget(
        max_total_bytes=(int(total.group(1)) << 20) if total else None,
        max_images=int(count.group(1)) if count else None,
        max_image_bytes=(int(single.group(1)) << 20) if single else None,
    )


_learned: Dict[str, Tuple[float, InlineImageBudget]] = {}


def learn_inline_image_budget(channel_key: str, error: Any) -> bool:
    """Remember the limits a channel's error announced; False if it announced none."""
    budget = parse_inline_image_budget_error(error)
    if budget is None or not channel_key:
        return False
    known = learned_inline_image_budget(channel_key)
    if known is not None:
        budget = known.merge(budget)
    _learned[channel_key] = (time.monotonic() + LEARNED_BUDGET_TTL_SECONDS, budget)
    return True


def learned_inline_image_budget(channel_key: str) -> Optional[InlineImageBudget]:
    entry = _learned.get(channel_key or "")
    if entry is None:
        return None
    expires_at, budget = entry
    if time.monotonic() >= expires_at:
        _learned.pop(channel_key, None)
        return None
    return budget


def forget_learned_inline_image_budgets() -> None:
    _learned.clear()


def _data_url_decoded_size(url: str) -> Optional[int]:
    if len(url) < 5 or url[:5].lower() != "data:":
        return None
    header, sep, data = url[5:].partition(",")
    if not sep:
        return None
    if not header.lower().endswith(";base64"):
        return len(data)
    data = data.strip()
    padding = len(data) - len(data.rstrip("="))
    return max(0, len(data) * 3 // 4 - padding)


def _inline_images(items: List[Any]) -> List[Tuple[int, str, int, int]]:
    """(item index, field, part index, decoded bytes) of each inline image, oldest first."""
    found: List[Tuple[int, str, int, int]] = []
    for item_index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        fields = ["content"]
        if item.get("type") in _TOOL_OUTPUT_TYPES:
            fields.append("output")
        for field in fields:
            parts = item.get(field)
            if not isinstance(parts, list):
                continue
            for part_index, part in enumerate(parts):
                if not isinstance(part, dict) or part.get("type") != "input_image":
                    continue
                url = part.get("image_url")
                size = _data_url_decoded_size(url) if isinstance(url, str) else None
                if size is not None:
                    found.append((item_index, field, part_index, size))
    return found


def fit_inline_images(payload: Dict[str, Any], budget: InlineImageBudget) -> int:
    """Replace the oldest inline images in `payload["input"]` until it fits `budget`.

    Returns how many images were replaced: 0 when it already fits, -1 when it
    cannot be made to fit without dropping the newest image (left unchanged).
    Items are copied before they change, so a shallow copy of a shared payload
    is safe to pass.
    """
    items = payload.get("input")
    if not isinstance(items, list):
        return 0
    images = _inline_images(items)
    if not images:
        return 0
    sizes = [size for _, _, _, size in images]
    max_total = None
    if budget.max_total_bytes is not None:
        max_total = max(0, budget.max_total_bytes - _SAFETY_MARGIN_BYTES)
    max_single = None
    if budget.max_image_bytes is not None:
        max_single = max(0, budget.max_image_bytes - _SAFETY_MARGIN_BYTES)

    def _fits(dropped: int) -> bool:
        kept = sizes[dropped:]
        if budget.max_images is not None and len(kept) > budget.max_images:
            return False
        if max_total is not None and sum(kept) > max_total:
            return False
        if max_single is not None and any(size > max_single for size in kept):
            return False
        return True

    if _fits(0):
        return 0
    newest = len(images) - 1
    needed = next((n for n in range(1, newest + 1) if _fits(n)), None)
    if needed is None:
        return -1
    drop = min(newest, -(-needed // DROP_STEP) * DROP_STEP)

    new_items = list(items)
    copied: Dict[int, Dict[str, Any]] = {}
    for item_index, field, part_index, _ in images[:drop]:
        item = copied.get(item_index)
        if item is None:
            item = copied[item_index] = deepcopy(items[item_index])
            new_items[item_index] = item
        item[field][part_index] = {"type": "input_text", "text": OMITTED_IMAGE_NOTE}
    payload["input"] = new_items
    return drop
