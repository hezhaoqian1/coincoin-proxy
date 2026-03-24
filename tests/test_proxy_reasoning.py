import json
import os
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("COINCOIN_DATABASE_URL", "mysql://user:pass@127.0.0.1:3306/test")

from app.proxy import (  # noqa: E402
    _response_reasoning_requested,
    _rewrite_responses_sse_event,
    _strip_reasoning_from_response_payload,
)


class ProxyReasoningFilterTests(unittest.TestCase):
    def test_reasoning_is_opt_in(self):
        self.assertFalse(_response_reasoning_requested({}))
        self.assertFalse(_response_reasoning_requested({"reasoning": False}))
        self.assertFalse(_response_reasoning_requested({"reasoning": {}}))
        self.assertTrue(_response_reasoning_requested({"reasoning": True}))
        self.assertTrue(_response_reasoning_requested({"reasoning": {"effort": "high"}}))

    def test_json_response_strips_reasoning_items_and_parts(self):
        payload = {
            "id": "resp_123",
            "output": [
                {"type": "reasoning", "summary": "hidden"},
                {
                    "type": "message",
                    "content": [
                        {"type": "reasoning", "text": "hidden"},
                        {"type": "output_text", "text": "final answer"},
                    ],
                },
                {"type": "function_call", "name": "echo", "arguments": "{}"},
            ],
        }

        filtered = _strip_reasoning_from_response_payload(payload)

        self.assertEqual(
            filtered["output"],
            [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "final answer"}],
                },
                {"type": "function_call", "name": "echo", "arguments": "{}"},
            ],
        )

    def test_stream_reasoning_event_is_dropped(self):
        raw = b'data: {"type":"response.reasoning.delta","delta":"thinking"}'

        rewritten, event = _rewrite_responses_sse_event(raw, strip_reasoning=True)

        self.assertIsNone(rewritten)
        self.assertIsNone(event)

    def test_stream_completed_event_strips_reasoning_output(self):
        raw_event = {
            "type": "response.completed",
            "response": {
                "id": "resp_123",
                "output": [
                    {"type": "reasoning", "summary": "hidden"},
                    {
                        "type": "message",
                        "content": [
                            {"type": "reasoning", "text": "hidden"},
                            {"type": "output_text", "text": "final answer"},
                        ],
                    },
                ],
            },
        }
        raw = b"data: " + json.dumps(raw_event, ensure_ascii=False).encode()

        rewritten, event = _rewrite_responses_sse_event(raw, strip_reasoning=True)

        self.assertIsNotNone(rewritten)
        self.assertIsNotNone(event)
        self.assertEqual(
            event["response"]["output"],
            [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "final answer"}],
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
