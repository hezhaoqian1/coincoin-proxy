import json
import unittest

from app.upstream_errors import is_context_window_error


class ContextWindowErrorTests(unittest.TestCase):
    def test_matches_known_provider_messages(self):
        cases = [
            # sub2api / OpenAI Responses relays (seen in prod as HTTP 502)
            {"error": {"message": "Your input exceeds the context window of this model. Please adjust your input and try again."}},
            # OpenAI chat/completions
            {"error": {"message": "This model's maximum context length is 128000 tokens.", "code": "context_length_exceeded"}},
            {"error": {"message": "bad request", "code": "context_length_exceeded"}},
            # Anthropic native
            {"type": "error", "error": {"type": "invalid_request_error", "message": "prompt is too long: 215000 tokens > 200000 maximum"}},
            # Responses stream failure envelope
            {"response": {"error": {"code": "context_too_large", "message": "too big"}}},
            {"detail": "Input token count exceeds the context length of the model"},
            # kedaya (seen live on 2026-11-10 as HTTP 502)
            {"error": {"message": "The request exceeds the context limit.", "type": "upstream_error"}},
            # Bedrock-style wording that Claude Code also treats as prompt-too-long
            {"message": "Input is too long for requested model."},
            {"error": "Request exceeds the token limit for this context"},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                self.assertTrue(is_context_window_error(payload))
                self.assertTrue(is_context_window_error(json.dumps(payload).encode("utf-8")))

    def test_plain_text_body_is_scanned(self):
        self.assertTrue(is_context_window_error(b"upstream error: input exceeds the context window"))

    def test_ordinary_failures_do_not_match(self):
        cases = [
            {"error": {"message": "Upstream access forbidden", "type": "upstream_error"}},
            {"error": {"message": "Service temporarily unavailable"}},
            {"error": {"message": "max_tokens: 64000 > 32000, which is the maximum allowed"}},
            {"error": {"message": "Rate limit exceeded"}},
            {"code": "INSUFFICIENT_BALANCE", "message": "Insufficient user balance. Please top up your user balance."},
            {"error": {"message": "No available accounts: no available accounts", "type": "api_error"}},
            b"<!DOCTYPE html><html><head><title>502 Bad Gateway</title></head><body>cloudflare</body></html>",
            {"error": {"message": "Request rate exceeds the limit for this key"}},
            b"<html>502 Bad Gateway</html>",
            b"",
            None,
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                self.assertFalse(is_context_window_error(payload))

    def test_echoed_request_content_in_json_error_is_ignored(self):
        payload = {
            "error": {"message": "invalid tool schema", "type": "invalid_request_error"},
            "request": {"messages": [{"role": "user", "content": "why does my prompt exceed the context window?"}]},
        }
        self.assertFalse(is_context_window_error(payload))
        self.assertFalse(is_context_window_error(json.dumps(payload)))


if __name__ == "__main__":
    unittest.main()
