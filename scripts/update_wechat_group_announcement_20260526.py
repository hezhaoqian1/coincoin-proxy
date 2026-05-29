#!/usr/bin/env python3
"""Publish the 2026-05-26 CoinCoin WeChat group announcement.

Run this after deploying the bundled static image:

    COINCOIN_ADMIN_TOKEN=... \
      python scripts/update_wechat_group_announcement_20260526.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


DEFAULT_IMAGE_URL = "/wechat-group-coincoin-2026-05-26.png?v=20260529-1"
ANNOUNCEMENT_TEMPLATE: dict[str, Any] = {
    "title": "进群再领 $15",
    "content": (
        "加入 CoinCoin 微信群，联系管理员领取额外 $15 API 额度。"
        "该二维码 6 月 2 日前有效，过期后可复制微信号 birdsync。"
    ),
    "priority": "info",
    "display_type": "modal",
    "audience": "all",
    "cta_label": "复制微信号",
    "cta_value": "birdsync",
}


def _request_json(
    base_url: str,
    admin_token: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> Any:
    url = f"{base_url.rstrip('/')}{path}"
    headers = {
        "Accept": "application/json",
        "X-Admin-Token": admin_token,
    }
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} failed: HTTP {exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{method} {path} failed: {exc.reason}") from exc


def _build_payload(image_url: str) -> dict[str, Any]:
    return {**ANNOUNCEMENT_TEMPLATE, "image_url": image_url}


def _absolute_url(base_url: str, value: str) -> str:
    if value.startswith(("http://", "https://")):
        return value
    return urllib.parse.urljoin(f"{base_url.rstrip('/')}/", value.lstrip("/"))


def _verify_image_url(base_url: str, image_url: str) -> None:
    url = _absolute_url(base_url, image_url)
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            content_type = (resp.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
            if not content_type.startswith("image/"):
                raise RuntimeError(f"image URL did not return an image: {url} content_type={content_type or 'unknown'}")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"image URL is not reachable: {url} HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"image URL is not reachable: {url} {exc.reason}") from exc


def _is_current_group_announcement(ann: dict[str, Any], payload: dict[str, Any]) -> bool:
    return (
        ann.get("status") == "active"
        and ann.get("image_url") == payload["image_url"]
        and ann.get("cta_value") == payload["cta_value"]
    )


def _is_stale_group_announcement(ann: dict[str, Any], keep_id: str, payload: dict[str, Any]) -> bool:
    if ann.get("id") == keep_id or ann.get("status") != "active":
        return False
    image_url = str(ann.get("image_url") or "")
    content = str(ann.get("content") or "")
    return (
        ann.get("cta_value") == payload["cta_value"]
        or "wechat-group-coincoin" in image_url
        or "station-payout-proofs" in image_url
        or "微信群" in content
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create the latest WeChat group announcement and archive stale group QR announcements.",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("COINCOIN_BASE_URL", "https://clawfather.up.railway.app"),
        help="CoinCoin public base URL. Defaults to production.",
    )
    parser.add_argument(
        "--admin-token",
        default=os.getenv("COINCOIN_ADMIN_TOKEN", ""),
        help="Admin token, or set COINCOIN_ADMIN_TOKEN.",
    )
    parser.add_argument(
        "--image-url",
        default=os.getenv("COINCOIN_ANNOUNCEMENT_IMAGE_URL", DEFAULT_IMAGE_URL),
        help="Public image URL to put into the announcement.",
    )
    parser.add_argument(
        "--skip-image-check",
        action="store_true",
        help="Do not verify that --image-url returns image/* before mutating announcements.",
    )
    parser.add_argument(
        "--no-archive-stale",
        action="store_true",
        help="Leave older active WeChat group announcements untouched.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the payload and exit without calling the admin API.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = _build_payload(args.image_url)
    if args.dry_run:
        print(json.dumps({"base_url": args.base_url, "payload": payload}, ensure_ascii=False, indent=2))
        return 0
    if not args.admin_token:
        print("error: provide --admin-token or COINCOIN_ADMIN_TOKEN", file=sys.stderr)
        return 2

    if not args.skip_image_check:
        _verify_image_url(args.base_url, payload["image_url"])

    anns = _request_json(args.base_url, args.admin_token, "GET", "/admin/announcements")
    if not isinstance(anns, list):
        raise RuntimeError("GET /admin/announcements returned a non-list payload")

    current = next((ann for ann in anns if _is_current_group_announcement(ann, payload)), None)
    if current:
        target = _request_json(
            args.base_url,
            args.admin_token,
            "PATCH",
            f"/admin/announcements/{current['id']}",
            {**payload, "status": "active"},
        )
        action = "updated_current"
    else:
        target = _request_json(args.base_url, args.admin_token, "POST", "/admin/announcements", payload)
        action = "created"

    target_id = target.get("id")
    if not target_id and current:
        target_id = current.get("id")
    keep_id = str(target_id or "")
    archived: list[str] = []
    if keep_id and not args.no_archive_stale:
        for ann in anns:
            if _is_stale_group_announcement(ann, keep_id, payload):
                _request_json(
                    args.base_url,
                    args.admin_token,
                    "PATCH",
                    f"/admin/announcements/{ann['id']}",
                    {"status": "archived"},
                )
                archived.append(str(ann["id"]))

    print(json.dumps({"action": action, "id": keep_id, "archived": archived}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
