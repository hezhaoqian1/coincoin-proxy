import unittest

import httpx

from app.channel_probe_contract import classify_probe_response


class ChannelProbeContractTests(unittest.TestCase):
    def test_anthropic_thinking_truncated_before_text_is_degraded(self) -> None:
        status, message = classify_probe_response(
            httpx.Response(200),
            {
                "id": "msg_reasoning_only",
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "",
                        "signature": "signed-thinking-block",
                    }
                ],
                "stop_reason": "max_tokens",
                "usage": {"input_tokens": 4, "output_tokens": 8},
            },
            1_500,
            endpoint="chat/completions",
            channel_type="anthropic_compatible",
        )

        self.assertEqual(status, "degraded")
        self.assertEqual(message, "probe output truncated before visible text")

    def test_anthropic_redacted_thinking_truncated_before_text_is_degraded(self) -> None:
        status, message = classify_probe_response(
            httpx.Response(200),
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "redacted_thinking", "data": "opaque-reasoning"}],
                "stop_reason": "max_tokens",
                "usage": {"output_tokens": 64},
            },
            1_500,
            endpoint="chat/completions",
            channel_type="anthropic_compatible",
        )

        self.assertEqual(status, "degraded")
        self.assertEqual(message, "probe output truncated before visible text")

    def test_responses_reasoning_truncated_before_text_is_degraded(self) -> None:
        status, message = classify_probe_response(
            httpx.Response(200),
            {
                "id": "resp_reasoning_only",
                "object": "response",
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [
                    {
                        "id": "rs_reasoning",
                        "type": "reasoning",
                        "summary": [],
                    }
                ],
                "usage": {
                    "output_tokens": 16,
                    "output_tokens_details": {"reasoning_tokens": 16},
                },
            },
            1_500,
            endpoint="responses",
            channel_type="openai_compatible",
        )

        self.assertEqual(status, "degraded")
        self.assertEqual(message, "probe output truncated before visible text")

    def test_chat_reasoning_truncated_before_text_is_degraded(self) -> None:
        status, message = classify_probe_response(
            httpx.Response(200),
            {
                "id": "chatcmpl_reasoning_only",
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "reasoning_content": "internal reasoning",
                        },
                    }
                ],
                "usage": {
                    "completion_tokens": 8,
                    "completion_tokens_details": {"reasoning_tokens": 8},
                },
            },
            1_500,
            endpoint="chat/completions",
            channel_type="openai_compatible",
        )

        self.assertEqual(status, "degraded")
        self.assertEqual(message, "probe output truncated before visible text")

    def test_reasoning_without_token_truncation_still_fails(self) -> None:
        cases = [
            (
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "thinking", "signature": "signed"}],
                    "stop_reason": "end_turn",
                    "usage": {"output_tokens": 8},
                },
                "chat/completions",
                "anthropic_compatible",
            ),
            (
                {
                    "object": "response",
                    "status": "completed",
                    "output": [{"id": "rs_reasoning", "type": "reasoning", "summary": []}],
                    "usage": {"output_tokens": 16},
                },
                "responses",
                "openai_compatible",
            ),
        ]

        for payload, endpoint, channel_type in cases:
            with self.subTest(endpoint=endpoint, channel_type=channel_type):
                status, message = classify_probe_response(
                    httpx.Response(200),
                    payload,
                    1_500,
                    endpoint=endpoint,
                    channel_type=channel_type,
                )
                self.assertEqual(status, "failed")
                self.assertEqual(message, "response missing structured model output")

    def test_truncation_without_reported_output_usage_still_fails(self) -> None:
        status, message = classify_probe_response(
            httpx.Response(200),
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "thinking", "signature": "signed"}],
                "stop_reason": "max_tokens",
                "usage": {"output_tokens": 0},
            },
            1_500,
            endpoint="chat/completions",
            channel_type="anthropic_compatible",
        )

        self.assertEqual(status, "failed")
        self.assertEqual(message, "response missing structured model output")

    def test_chat_reasoning_without_length_finish_reason_still_fails(self) -> None:
        status, message = classify_probe_response(
            httpx.Response(200),
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "reasoning_content": "internal reasoning",
                        },
                    }
                ],
                "usage": {"completion_tokens": 8},
            },
            1_500,
            endpoint="chat/completions",
            channel_type="openai_compatible",
        )

        self.assertEqual(status, "failed")
        self.assertEqual(message, "response missing structured model output")

    def test_retryable_http_failures_are_failed(self) -> None:
        for status_code in (408, 409, 429, 500, 503):
            with self.subTest(status_code=status_code):
                status, message = classify_probe_response(
                    httpx.Response(status_code),
                    {},
                    1_500,
                    endpoint="responses",
                    channel_type="openai_compatible",
                )
                self.assertEqual(status, "failed")
                self.assertEqual(message, f"HTTP {status_code}")

    def test_successful_http_error_payload_is_failed_and_masked(self) -> None:
        status, message = classify_probe_response(
            httpx.Response(200),
            {"error": {"message": "provider error\nprivate details"}},
            1_500,
            endpoint="responses",
            channel_type="openai_compatible",
        )

        self.assertEqual(status, "failed")
        self.assertEqual(message, "provider error private details")

    def test_valid_slow_response_is_degraded(self) -> None:
        status, message = classify_probe_response(
            httpx.Response(200),
            {
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "OK"}],
                    }
                ]
            },
            30_000,
            endpoint="responses",
            channel_type="openai_compatible",
        )

        self.assertEqual(status, "degraded")
        self.assertEqual(message, "slow response 30000ms")

    def test_reasoning_detail_usage_can_prove_responses_truncation(self) -> None:
        status, message = classify_probe_response(
            httpx.Response(200),
            {
                "status": "incomplete",
                "incomplete_details": {"reason": "max_tokens"},
                "output": [{"type": "reasoning", "encrypted_content": "opaque"}],
                "usage": {
                    "output_tokens": 0,
                    "output_tokens_details": {"reasoning_tokens": 64},
                },
            },
            1_500,
            endpoint="responses",
            channel_type="openai_compatible",
        )

        self.assertEqual(status, "degraded")
        self.assertEqual(message, "probe output truncated before visible text")


if __name__ == "__main__":
    unittest.main()
