import unittest

from app.proxy import (
    ResponseConversationCache,
    _expand_previous_response_input,
    _normalize_responses_input_items,
)


class ResponsesPolyfillTests(unittest.TestCase):
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
