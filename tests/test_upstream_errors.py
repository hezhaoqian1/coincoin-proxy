import json
import unittest

from app.upstream_errors import is_context_window_error, is_insufficient_balance_error, upstream_error_summary


class InsufficientBalanceErrorTests(unittest.TestCase):
    def test_matches_known_provider_messages(self):
        cases = [
            # tyapi (seen live on 2026-11-10 as HTTP 403)
            {"code": "INSUFFICIENT_BALANCE", "message": "Insufficient user balance. Please top up your user balance."},
            # zzshu (seen live on 2026-11-10 as HTTP 403)
            {"error": {"message": "Insufficient user quota", "type": "upstream_error"}},
            # sub2api
            {"error": {"message": "Insufficient account balance", "type": "billing_error"}},
            # new-api
            {"error": {"message": "用户额度不足, 剩余额度: ＄0.012", "type": "new_api_error"}},
            {"error": {"message": "预扣费额度失败, 用户剩余额度: ＄0.000000, 需要预扣费额度: ＄0.044000"}},
            {"error": {"message": "user quota is not enough", "code": "insufficient_user_quota"}},
            {"error": {"message": "该令牌额度已用尽"}},
            # OpenAI / Anthropic / OpenRouter
            {"error": {"message": "You exceeded your current quota, please check your plan and billing details.", "code": "insufficient_quota"}},
            {"type": "error", "error": {"type": "invalid_request_error", "message": "Your credit balance is too low to access the Anthropic API."}},
            {"error": {"message": "Insufficient credits. Add more using https://example.test/credits", "code": 402}},
            b"Insufficient Balance",
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                self.assertTrue(is_insufficient_balance_error(payload))

    def test_ordinary_failures_do_not_match(self):
        cases = [
            {"error": {"message": "Upstream access forbidden", "type": "upstream_error"}},
            {"error": {"message": "invalid api key", "code": "invalid_api_key"}},
            {"error": {"message": "Insufficient permissions for this model"}},
            {"code": "GROUP_DISABLED", "message": "API Key 所属分组已停用"},
            {"error": {"message": "Rate limit exceeded"}},
            {"error": {"message": "No available accounts: no available accounts"}},
            b"<html>502 Bad Gateway</html>",
            b"",
            None,
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                self.assertFalse(is_insufficient_balance_error(payload))

    def test_summary_prefers_error_message_and_redacts_keys(self):
        self.assertEqual(
            upstream_error_summary({"code": "INSUFFICIENT_BALANCE", "message": "Insufficient user balance.\nPlease top up."}),
            "Insufficient user balance. Please top up.",
        )
        self.assertEqual(
            upstream_error_summary(b'{"error": {"message": "quota exhausted for sk-abcdefghijklmnop"}}'),
            "quota exhausted for [redacted]",
        )
        self.assertEqual(upstream_error_summary("plain text failure"), "plain text failure")
        self.assertEqual(len(upstream_error_summary("x" * 1000)), 200)


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
