import time
import unittest
from email.utils import formatdate
from types import SimpleNamespace

from app.config import settings
from app.proxy import (
    ResponseConversationCache,
    _apply_previous_response_polyfill,
    _channel_fallback_config,
    _classify_responses_stream_event,
    _drop_encrypted_reasoning_items,
    _expand_previous_response_input,
    _first_output_timeout_seconds,
    _is_invalid_encrypted_content_error,
    _is_retryable_responses_stream_error,
    _normalize_responses_input_items,
    _rate_limit_cooldown_seconds,
    _responses_stream_outcome,
    _sanitize_encrypted_ids,
)
from app.router import ModelConfig


class ResponsesStreamClassificationTests(unittest.TestCase):
    def test_prelude_events_and_comments_are_held(self):
        self.assertEqual(_classify_responses_stream_event(b'data: {"type":"response.created","response":{"error":null}}')[0], "hold")
        self.assertEqual(_classify_responses_stream_event(b'event: response.in_progress\ndata: {"type":"response.in_progress"}')[0], "hold")
        self.assertEqual(_classify_responses_stream_event(b": keepalive")[0], "hold")
        self.assertEqual(_classify_responses_stream_event(b"")[0], "hold")

    def test_output_and_done_events_commit_the_stream(self):
        self.assertEqual(_classify_responses_stream_event(b'data: {"type":"response.output_item.added","item":{"type":"reasoning"}}')[0], "output")
        self.assertEqual(_classify_responses_stream_event(b"data:[DONE]")[0], "output")
        self.assertEqual(_classify_responses_stream_event(b"data: not json")[0], "output")

    def test_failed_and_error_events_carry_the_error(self):
        kind, error = _classify_responses_stream_event(
            b'data: {"type":"response.failed","response":{"error":{"code":"server_error","message":"boom"}}}'
        )
        self.assertEqual((kind, error["code"]), ("failed", "server_error"))
        kind, error = _classify_responses_stream_event(b'data: {"type":"error","code":"rate_limit_exceeded","message":"slow down"}')
        self.assertEqual((kind, error["code"]), ("failed", "rate_limit_exceeded"))
        kind, error = _classify_responses_stream_event(b'data: {"error":{"message":"Upstream rate limit exceeded"}}')
        self.assertEqual((kind, error["message"]), ("failed", "Upstream rate limit exceeded"))

    def test_only_channel_side_errors_are_retried(self):
        self.assertTrue(_is_retryable_responses_stream_error({"code": "rate_limit_exceeded"}))
        self.assertTrue(_is_retryable_responses_stream_error({"type": "server_error", "message": "boom"}))
        self.assertTrue(_is_retryable_responses_stream_error({"message": "用户额度不足"}))
        self.assertFalse(_is_retryable_responses_stream_error({"code": "context_length_exceeded", "message": "try again"}))
        self.assertFalse(_is_retryable_responses_stream_error({"code": "invalid_encrypted_content"}))
        self.assertFalse(_is_retryable_responses_stream_error({"message": "something odd"}))
        self.assertFalse(_is_retryable_responses_stream_error({}))

    def test_rate_limit_cooldown_reads_retry_after_and_reset_hints(self):
        self.assertEqual(_rate_limit_cooldown_seconds({"retry-after": "120"}), 120.0)
        self.assertEqual(_rate_limit_cooldown_seconds({"retry-after": "86400"}), 600.0)
        future = time.time() + 90
        http_date = formatdate(future, usegmt=True)
        self.assertAlmostEqual(_rate_limit_cooldown_seconds({"retry-after": http_date}), 90, delta=2)
        self.assertEqual(_rate_limit_cooldown_seconds(error={"resets_in_seconds": 45}), 45.0)
        self.assertAlmostEqual(_rate_limit_cooldown_seconds(error={"resets_at": int(time.time()) + 30}), 30, delta=2)
        self.assertIsNone(_rate_limit_cooldown_seconds({"retry-after": "soon"}))
        self.assertIsNone(_rate_limit_cooldown_seconds({}, {"message": "rate limited"}))

    def test_drops_only_encrypted_reasoning_and_compaction_items(self):
        payload = {
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
                {"type": "reasoning", "summary": [], "encrypted_content": "gAAAA1"},
                {"type": "reasoning", "summary": [{"type": "summary_text", "text": "plain"}]},
                {"type": "compaction", "encrypted_content": "gAAAA2"},
                {"type": "function_call", "call_id": "call_1", "name": "ls", "arguments": "{}"},
            ]
        }

        self.assertTrue(_drop_encrypted_reasoning_items(payload))
        self.assertEqual(
            [item["type"] for item in payload["input"]],
            ["message", "reasoning", "function_call"],
        )
        self.assertFalse(_drop_encrypted_reasoning_items(payload))
        self.assertTrue(_is_invalid_encrypted_content_error({"code": "invalid_encrypted_content"}))
        self.assertTrue(_is_invalid_encrypted_content_error("The encrypted content for item rs_1 could not be verified."))
        self.assertFalse(_is_invalid_encrypted_content_error({"code": "context_length_exceeded"}))

    def test_first_output_timeout_uses_model_overrides_and_high_effort_floor(self):
        model = SimpleNamespace(public_id="gpt-6-sol")
        originals = (
            settings.upstream_first_output_timeout_seconds,
            settings.upstream_first_output_timeouts,
            settings.upstream_high_effort_first_output_timeout_seconds,
        )
        try:
            settings.upstream_first_output_timeout_seconds = 0
            settings.upstream_first_output_timeouts = ""
            settings.upstream_high_effort_first_output_timeout_seconds = 120
            self.assertEqual(_first_output_timeout_seconds(model, "xhigh"), 0)
            settings.upstream_first_output_timeouts = '{"gpt-6-sol": 45}'
            self.assertEqual(_first_output_timeout_seconds(model, "medium"), 45)
            self.assertEqual(_first_output_timeout_seconds(model, "xhigh"), 120)
            self.assertEqual(_first_output_timeout_seconds(SimpleNamespace(public_id="gpt-6-luna")), 0)
            settings.upstream_first_output_timeout_seconds = 90
            self.assertEqual(_first_output_timeout_seconds(SimpleNamespace(public_id="gpt-6-luna")), 90)
            settings.upstream_first_output_timeouts = "not json"
            self.assertEqual(_first_output_timeout_seconds(model), 90)
        finally:
            (
                settings.upstream_first_output_timeout_seconds,
                settings.upstream_first_output_timeouts,
                settings.upstream_high_effort_first_output_timeout_seconds,
            ) = originals

    def test_stream_outcome(self):
        usage = {"input": 0, "output": 0}
        self.assertEqual(_responses_stream_outcome({"terminal": "response.completed"}, usage), "completed")
        self.assertEqual(_responses_stream_outcome({"terminal": "response.incomplete"}, usage), "incomplete")
        self.assertEqual(_responses_stream_outcome({"terminal": "response.failed", "error": {}}, usage), "failed")
        self.assertEqual(_responses_stream_outcome({"terminal": "", "error": {"message": "x"}}, usage), "failed")
        self.assertEqual(_responses_stream_outcome({"terminal": "", "done": True}, usage), "done")
        self.assertEqual(_responses_stream_outcome({"terminal": ""}, {"input": 5, "output": 0}), "done")
        self.assertEqual(_responses_stream_outcome({"terminal": ""}, usage), "eof")


class ResponsesPolyfillTests(unittest.TestCase):
    def test_locked_channel_fallback_keeps_selected_model_id(self):
        previous = ModelConfig(
            model_id="gpt-5.4",
            upstream_url="https://primary.example/v1",
            api_key="primary-key",
            price_input_per_million=250,
            price_output_per_million=1500,
            strip_unsupported=False,
            auth_style="bearer",
            channel_id="ch_primary",
            fallback_from_channel_id="ch_seed",
            route_attempt=2,
        )
        fallback = ModelConfig(
            model_id="gpt-5.5",
            upstream_url="https://backup.example/v1",
            api_key="backup-key",
            price_input_per_million=500,
            price_output_per_million=3000,
            strip_unsupported=False,
            auth_style="bearer",
            channel_id="ch_backup",
            fallback_from_channel_id="ch_seed,ch_primary",
            route_attempt=1,
        )

        locked = _channel_fallback_config(previous, fallback, lock_model_selection=True)

        self.assertEqual(locked.model_id, "gpt-5.4")
        self.assertEqual(locked.channel_id, "ch_backup")
        self.assertEqual(locked.upstream_url, "https://backup.example/v1")
        self.assertEqual(locked.api_key, "backup-key")
        self.assertEqual(locked.fallback_from_channel_id, "ch_seed,ch_primary")
        self.assertEqual(locked.route_attempt, 3)

    def test_sanitize_encrypted_ids_preserves_reasoning_encrypted_content(self):
        reasoning_blob = "gAAAAAB_reasoning_state"
        compaction_blob = "gBAAAAA_compaction_state"
        payload = {
            "input": [
                {
                    "id": "rs_gAAAAAB_internal_reasoning_id",
                    "type": "reasoning",
                    "summary": [],
                    "encrypted_content": reasoning_blob,
                    "status": "completed",
                },
                {
                    "id": "compaction_1",
                    "type": "compaction",
                    "encrypted_content": compaction_blob,
                },
            ]
        }

        _sanitize_encrypted_ids(payload)

        self.assertEqual(payload["input"][0]["encrypted_content"], reasoning_blob)
        self.assertEqual(payload["input"][1]["encrypted_content"], compaction_blob)

    def test_normalizes_string_input_to_message_item(self):
        items = _normalize_responses_input_items("hello openclaw")

        self.assertEqual(
            items,
            [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello openclaw"}],
                }
            ],
        )

    def test_heals_legacy_char_array_cache_entries(self):
        items = _normalize_responses_input_items(list("hello"))

        self.assertEqual(
            items,
            [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}],
                }
            ],
        )

    def test_expand_previous_response_input_rebuilds_valid_input(self):
        cached_output = [
            {
                "id": "msg_cached",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "cached reply"}],
            }
        ]
        payload = {
            "previous_response_id": "resp_cached",
            "input": "follow up question",
        }

        counts = _expand_previous_response_input(payload, (list("hello"), cached_output))

        self.assertEqual(counts, (1, 1, 1))
        self.assertEqual(payload["previous_response_id"], "resp_cached")
        self.assertEqual(len(payload["input"]), 3)
        self.assertEqual(payload["input"][0]["content"][0]["text"], "hello")
        self.assertEqual(payload["input"][1]["content"][0]["text"], "cached reply")
        self.assertEqual(payload["input"][2]["content"][0]["text"], "follow up question")
        self.assertEqual(cached_output[0]["id"], "msg_cached")

    def test_apply_previous_response_polyfill_drops_previous_response_id(self):
        cached_output = [
            {
                "id": "msg_cached",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "cached reply"}],
            }
        ]
        payload = {
            "previous_response_id": "resp_cached",
            "input": "follow up question",
        }

        counts = _apply_previous_response_polyfill(payload, (list("hello"), cached_output))

        self.assertEqual(counts, (1, 1, 1))
        self.assertNotIn("previous_response_id", payload)
        self.assertEqual(len(payload["input"]), 3)
        self.assertEqual(payload["input"][0]["content"][0]["text"], "hello")
        self.assertEqual(payload["input"][1]["content"][0]["text"], "cached reply")
        self.assertEqual(payload["input"][2]["content"][0]["text"], "follow up question")

    def test_response_cache_trims_to_recent_turn_budget(self):
        cache = ResponseConversationCache(
            ttl_seconds=300,
            max_entries=10,
            max_total_bytes=1024 * 1024,
            max_entry_bytes=1024 * 1024,
            max_turns=2,
        )

        expanded_input = [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": f"turn-{idx}"}],
            }
            for idx in range(6)
        ]
        response_output = [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}]

        cache.set("resp_trimmed", expanded_input, response_output)
        cached = cache.get("resp_trimmed")

        self.assertIsNotNone(cached)
        cached_input, _ = cached
        self.assertEqual(len(cached_input), 4)
        self.assertEqual([item["content"][0]["text"] for item in cached_input], ["turn-2", "turn-3", "turn-4", "turn-5"])

    def test_response_cache_skips_oversized_entries(self):
        cache = ResponseConversationCache(
            ttl_seconds=300,
            max_entries=10,
            max_total_bytes=1024 * 1024,
            max_entry_bytes=256,
            max_turns=8,
        )

        huge_input = [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "x" * 2048}],
            }
        ]

        cache.set("resp_huge", huge_input, [])

        self.assertIsNone(cache.get("resp_huge"))

    def test_response_cache_evicts_oldest_to_stay_within_budget(self):
        sample_input = [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "sample" * 160}],
            }
        ]
        sample_size = ResponseConversationCache(
            ttl_seconds=300,
            max_entries=10,
            max_total_bytes=1024 * 1024,
            max_entry_bytes=1024 * 1024,
            max_turns=8,
        )._estimate_size_bytes(sample_input, [])
        cache = ResponseConversationCache(
            ttl_seconds=300,
            max_entries=10,
            max_total_bytes=(sample_size * 2) + 10,
            max_entry_bytes=sample_size + 10,
            max_turns=8,
        )

        for response_id in ("resp_1", "resp_2", "resp_3"):
            cache.set(
                response_id,
                [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "sample" * 160}],
                    }
                ],
                [],
            )

        self.assertIsNone(cache.get("resp_1"))
        self.assertIsNotNone(cache.get("resp_3"))


if __name__ == "__main__":
    unittest.main()
