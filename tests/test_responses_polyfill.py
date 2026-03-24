import unittest

from app.proxy import _expand_previous_response_input, _normalize_responses_input_items


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


if __name__ == "__main__":
    unittest.main()
