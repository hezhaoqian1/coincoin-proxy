"""Accept compressed request bodies (``Content-Encoding: gzip`` / ``deflate``).

Large chat-agent requests resend the whole conversation on every turn, so a
single body is often 0.5–3 MB of JSON. From mainland China the upload to our
Singapore region runs at roughly 100 KB/s per connection, which makes the
upload itself the slowest part of the request. JSON compresses 4–8x, so a
client (or the client's own gateway) that gzips the body cuts that time by the
same factor. Like sub2api's ``ReadRequestBodyWithPrealloc``, the body is
decoded before the application sees it: ``Content-Encoding`` is removed,
``Content-Length`` is rewritten, and the handlers read plain JSON as before.

* ``gzip`` / ``x-gzip`` (including concatenated gzip members) and ``deflate``
  (zlib-wrapped, with a raw-deflate fallback) are supported, also stacked as a
  comma-separated list.
* The decoded body is capped (64 MB by default, same as sub2api) to stop
  decompression bombs; the compressed body has the same cap.
* Corrupt or truncated data is a 400 and an unknown encoding is a 415, both as
  an ``invalid_request_error`` body that Anthropic and OpenAI clients parse.
"""

from __future__ import annotations

import json
import logging
import zlib
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("coincoin.request_decompression")

ASGIApp = Callable[..., Awaitable[None]]

MAX_DECOMPRESSED_BYTES = 64 * 1024 * 1024
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})
_GZIP_WBITS = 16 + zlib.MAX_WBITS
_SUPPORTED = ("gzip", "x-gzip", "deflate")


class _BodyTooLarge(Exception):
    pass


class _BodyDecodeError(Exception):
    pass


class _ClientDisconnected(Exception):
    pass


def _inflate(data: bytes, wbits: int, limit: int, *, multi_member: bool) -> bytes:
    """Inflate ``data`` without ever holding more than ``limit + 1`` output bytes."""
    out = bytearray()
    buf = data
    while True:
        decoder = zlib.decompressobj(wbits)
        pending = buf
        while True:
            # max_length must stay >= 1: 0 means "no limit" to zlib.
            out += decoder.decompress(pending, limit + 1 - len(out))
            if len(out) > limit:
                raise _BodyTooLarge()
            pending = decoder.unconsumed_tail
            if not pending:
                break
        if not decoder.eof:
            raise _BodyDecodeError("truncated compressed body")
        buf = decoder.unused_data
        if not multi_member or not buf.strip(b"\x00"):
            return bytes(out)


def decode_body(data: bytes, encodings: List[str], limit: int = MAX_DECOMPRESSED_BYTES) -> bytes:
    """Undo ``encodings`` (as listed in Content-Encoding, i.e. applied in order)."""
    for encoding in reversed(encodings):
        try:
            if encoding in ("gzip", "x-gzip"):
                data = _inflate(data, _GZIP_WBITS, limit, multi_member=True)
            elif encoding == "deflate":
                try:
                    data = _inflate(data, zlib.MAX_WBITS, limit, multi_member=False)
                except (zlib.error, _BodyDecodeError):
                    data = _inflate(data, -zlib.MAX_WBITS, limit, multi_member=False)
            else:  # pragma: no cover - filtered by the caller
                raise _BodyDecodeError(f"unsupported encoding {encoding}")
        except zlib.error as exc:
            raise _BodyDecodeError(str(exc)) from exc
    return data


def parse_content_encoding(value: str) -> Tuple[List[str], List[str]]:
    """Return (encodings to undo, unsupported encodings) for a header value."""
    encodings = [part.strip().lower() for part in value.split(",")]
    encodings = [part for part in encodings if part and part != "identity"]
    unsupported = [part for part in encodings if part not in _SUPPORTED]
    return encodings, unsupported


def _error_body(message: str) -> bytes:
    return json.dumps(
        {"type": "error", "error": {"type": "invalid_request_error", "message": message}},
        ensure_ascii=False,
    ).encode("utf-8")


async def _send_error(send: Callable[[Dict[str, Any]], Awaitable[None]], status: int, message: str, extra_headers: Optional[List[Tuple[bytes, bytes]]] = None) -> None:
    body = _error_body(message)
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    headers += extra_headers or []
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body, "more_body": False})


async def _read_body(receive: Callable[[], Awaitable[Dict[str, Any]]], limit: int) -> bytes:
    chunks: List[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message.get("type") == "http.disconnect":
            raise _ClientDisconnected()
        if message.get("type") != "http.request":
            continue
        chunk = message.get("body", b"") or b""
        size += len(chunk)
        if size > limit:
            raise _BodyTooLarge()
        chunks.append(chunk)
        if not message.get("more_body", False):
            return b"".join(chunks)


class RequestDecompressionASGIMiddleware:
    def __init__(self, app: ASGIApp, max_decompressed_bytes: int = MAX_DECOMPRESSED_BYTES) -> None:
        self.app = app
        self.max_decompressed_bytes = int(max_decompressed_bytes)

    async def __call__(self, scope: Dict[str, Any], receive: Callable[[], Awaitable[Dict[str, Any]]], send: Callable[[Dict[str, Any]], Awaitable[None]]) -> None:
        if scope.get("type") != "http" or str(scope.get("method", "")).upper() not in _BODY_METHODS:
            await self.app(scope, receive, send)
            return
        raw_encoding = b""
        for name, value in scope.get("headers") or []:
            if name.lower() == b"content-encoding":
                raw_encoding = value
                break
        if not raw_encoding:
            await self.app(scope, receive, send)
            return
        encodings, unsupported = parse_content_encoding(raw_encoding.decode("latin-1"))
        if not encodings:
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if unsupported:
            logger.warning("rejecting request body with unsupported Content-Encoding %s on %s", ",".join(unsupported), path)
            await _send_error(
                send,
                415,
                f"Unsupported Content-Encoding: {', '.join(unsupported)}. Supported: gzip, deflate.",
                [(b"accept-encoding", b"gzip, deflate")],
            )
            return
        limit = self.max_decompressed_bytes
        try:
            compressed = await _read_body(receive, limit)
            body = decode_body(compressed, encodings, limit)
        except _ClientDisconnected:
            return
        except _BodyTooLarge:
            logger.warning("rejecting compressed request body over %d bytes on %s", limit, path)
            await _send_error(send, 413, f"Request body exceeds {limit // (1024 * 1024)} MB after decompression.")
            return
        except _BodyDecodeError as exc:
            logger.warning("rejecting undecodable %s request body on %s: %s", ",".join(encodings), path, exc)
            await _send_error(send, 400, f"Could not decode the {', '.join(encodings)} request body: {exc}")
            return

        headers = [
            (name, value)
            for name, value in scope.get("headers") or []
            if name.lower() not in (b"content-encoding", b"content-length")
        ]
        headers.append((b"content-length", str(len(body)).encode()))
        new_scope = dict(scope, headers=headers)
        delivered = False

        async def decoded_receive() -> Dict[str, Any]:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(new_scope, decoded_receive, send)
