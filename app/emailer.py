import logging
from dataclasses import dataclass

import httpx

from .config import settings

logger = logging.getLogger("coincoin.emailer")

RESEND_EMAILS_URL = "https://api.resend.com/emails"


@dataclass
class EmailSendResult:
    sent: bool
    provider_id: str = ""
    error: str = ""


async def send_verification_email(email: str, code: str, ttl_minutes: int) -> EmailSendResult:
    """Send a short verification email through Resend's REST API."""
    api_key = (settings.resend_api_key or "").strip()
    if not api_key:
        logger.warning("email verification skipped: COINCOIN_RESEND_API_KEY is not configured")
        return EmailSendResult(sent=False, error="email provider not configured")

    subject = "CoinCoin 验证码"
    text = f"你的 CoinCoin 验证码是 {code}。{ttl_minutes} 分钟内有效。"
    html = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;line-height:1.6;color:#111827">
      <p>你的 CoinCoin 验证码是：</p>
      <p style="font-size:28px;font-weight:700;letter-spacing:0.16em;margin:16px 0">{code}</p>
      <p>{ttl_minutes} 分钟内有效。如果不是你本人操作，可以忽略这封邮件。</p>
    </div>
    """.strip()

    payload = {
        "from": settings.email_from,
        "to": [email],
        "subject": subject,
        "html": html,
        "text": text,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "coincoin-proxy/1.0",
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(RESEND_EMAILS_URL, headers=headers, json=payload)
        if response.status_code >= 400:
            logger.warning("resend send failed: status=%s body=%s", response.status_code, response.text[:500])
            return EmailSendResult(sent=False, error="email provider rejected request")
        data = response.json()
        return EmailSendResult(sent=True, provider_id=str(data.get("id") or ""))
    except Exception as exc:
        logger.warning("resend send failed: %s", exc)
        return EmailSendResult(sent=False, error="email provider unavailable")
