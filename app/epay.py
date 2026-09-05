import hashlib
import logging
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .config import settings
from .payment_common import normalize_rmb

logger = logging.getLogger("coincoin.epay")

CALLBACK_SIGN_KEYS = {
    "pid",
    "trade_no",
    "out_trade_no",
    "type",
    "name",
    "money",
    "trade_status",
    "sign",
    "sign_type",
}


class EpayVerificationError(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def _stringify_params(params: dict[str, Any]) -> dict[str, str]:
    return {
        str(key): "" if value is None else str(value)
        for key, value in params.items()
    }


def _filtered_sign_params(params: dict[str, Any]) -> dict[str, str]:
    return {
        key: value
        for key, value in sorted(_stringify_params(params).items())
        if key not in ("sign", "sign_type") and value != ""
    }


def epay_submit_url() -> str:
    return settings.epay_api_url.rstrip("/") + "/submit.php"


def epay_query_url() -> str:
    return settings.epay_api_url.rstrip("/") + "/api.php"


def epay_configured() -> bool:
    return bool(settings.epay_pid) and bool(settings.epay_key) and bool(settings.epay_api_url)


def generate_epay_sign(params: dict[str, Any], key: str | None = None) -> str:
    secret = key or settings.epay_key
    if not secret:
        raise RuntimeError("epay key is not configured")

    sign_str = "&".join(f"{k}={v}" for k, v in _filtered_sign_params(params).items())
    sign_str += secret
    return hashlib.md5(sign_str.encode("utf-8")).hexdigest()


def verify_epay_sign(params: dict[str, Any], key: str | None = None) -> bool:
    expected = _stringify_params(params).get("sign", "")
    if not expected:
        return False
    return generate_epay_sign(params, key=key) == expected


def extract_epay_params_from_proof_url(proof_url: str) -> dict[str, str]:
    raw = (proof_url or "").strip()
    if not raw:
        raise EpayVerificationError("proof_url is required")

    parsed = urlparse(raw)
    query = parsed.query
    if not query:
        query = raw[1:] if raw.startswith("?") else raw

    params = {
        key: values[-1]
        for key, values in parse_qs(query, keep_blank_values=True).items()
        if values
    }
    if not params:
        raise EpayVerificationError("unable to parse payment proof URL")
    return params


def verify_epay_callback_params(
    params: dict[str, Any],
    *,
    require_success: bool = True,
) -> dict[str, str]:
    raw_params = _stringify_params(params)
    normalized = {
        key: value
        for key, value in raw_params.items()
        if key in CALLBACK_SIGN_KEYS
    }

    if not epay_configured():
        raise EpayVerificationError("epay is not configured on coincoin-proxy")
    if not verify_epay_sign(normalized):
        raise EpayVerificationError("payment signature verification failed")
    if normalized.get("sign_type", "MD5").upper() != "MD5":
        raise EpayVerificationError("unsupported sign_type")
    if normalized.get("pid", "") != str(settings.epay_pid):
        raise EpayVerificationError("payment pid mismatch")

    order_no = normalized.get("out_trade_no") or raw_params.get("order_no") or ""
    if not order_no:
        raise EpayVerificationError("payment callback missing out_trade_no")

    trade_no = normalized.get("trade_no", "")
    if not trade_no:
        raise EpayVerificationError("payment callback missing trade_no")

    money = normalized.get("money", "")
    if not money:
        raise EpayVerificationError("payment callback missing money")

    trade_status = normalized.get("trade_status", "").upper()
    if require_success and trade_status != "TRADE_SUCCESS":
        raise EpayVerificationError("payment not completed")

    normalized["out_trade_no"] = order_no
    normalized["trade_no"] = trade_no
    normalized["money"] = normalize_rmb(money)
    normalized["trade_status"] = trade_status
    return normalized


def build_epay_submit_url(
    *,
    out_trade_no: str,
    name: str,
    money: str,
    pay_type: str,
    notify_url: str,
    return_url: str,
    sitename: str | None = None,
) -> str:
    if not epay_configured():
        raise RuntimeError("epay credentials are not configured")

    params: dict[str, Any] = {
        "pid": settings.epay_pid,
        "type": pay_type,
        "out_trade_no": out_trade_no,
        "notify_url": notify_url,
        "return_url": return_url,
        "name": name,
        "money": normalize_rmb(money),
    }

    final_sitename = sitename or settings.epay_site_name
    if final_sitename:
        params["sitename"] = final_sitename

    params["sign"] = generate_epay_sign(params)
    params["sign_type"] = "MD5"
    return epay_submit_url() + "?" + urlencode(_stringify_params(params))


async def query_epay_order(out_trade_no: str) -> dict[str, Any]:
    if not epay_configured():
        return {"code": -1, "msg": "epay credentials are not configured"}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                epay_query_url(),
                params={
                    "act": "order",
                    "pid": settings.epay_pid,
                    "key": settings.epay_key,
                    "out_trade_no": out_trade_no,
                },
            )
    except Exception as exc:
        logger.error("query_epay_order failed for %s: %s", out_trade_no, exc)
        return {"code": -1, "msg": str(exc)}

    if resp.status_code == 404:
        return {"code": -1, "msg": "api.php 不存在，该平台未开放查询接口"}

    try:
        data = resp.json()
    except Exception:
        logger.warning(
            "query_epay_order non-json response for %s: status=%s body=%s",
            out_trade_no,
            resp.status_code,
            resp.text[:200],
        )
        return {"code": -1, "msg": "epay query API unavailable"}

    if not isinstance(data, dict):
        return {"code": -1, "msg": "invalid epay query response"}
    return data
