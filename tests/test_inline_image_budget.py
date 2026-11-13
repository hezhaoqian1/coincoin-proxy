import unittest
from copy import deepcopy
from unittest.mock import patch

from app import inline_image_budget as budget_module
from app.inline_image_budget import (
    DROP_STEP,
    OMITTED_IMAGE_NOTE,
    InlineImageBudget,
    fit_inline_images,
    forget_learned_inline_image_budgets,
    learn_inline_image_budget,
    learned_inline_image_budget,
    parse_inline_image_budget_error,
)

KIB = 1024
MIB = 1024 * 1024


def _data_url(decoded_bytes: int, mime: str = "image/png") -> str:
    """A base64 data URL that decodes to exactly `decoded_bytes` (a multiple of 3)."""
    assert decoded_bytes % 3 == 0
    return f"data:{mime};base64," + "A" * (decoded_bytes // 3 * 4)


def _user_image(url: str, text: str = "look") -> dict:
    return {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": text}, {"type": "input_image", "image_url": url}],
    }


def _tool_image(url: str, call_id: str) -> dict:
    return {"type": "function_call_output", "call_id": call_id, "output": [{"type": "input_image", "image_url": url}]}


def _image_urls(items: list) -> list:
    urls = []
    for item in items:
        for field in ("content", "output"):
            parts = item.get(field)
            if isinstance(parts, list):
                urls.extend(part["image_url"] for part in parts if part.get("type") == "input_image")
    return urls


def _notes(items: list) -> int:
    count = 0
    for item in items:
        for field in ("content", "output"):
            parts = item.get(field)
            if isinstance(parts, list):
                count += sum(1 for part in parts if part.get("text") == OMITTED_IMAGE_NOTE)
    return count


class ParseInlineImageBudgetErrorTests(unittest.TestCase):
    def test_reads_total_count_and_single_image_limits(self):
        self.assertEqual(
            parse_inline_image_budget_error("image relay inline images exceed the configured 32 MiB request limit"),
            InlineImageBudget(max_total_bytes=32 * MIB),
        )
        self.assertEqual(
            parse_inline_image_budget_error("inline images exceed the configured 24 MiB raw request limit; run compact manually"),
            InlineImageBudget(max_total_bytes=24 * MIB),
        )
        self.assertEqual(
            parse_inline_image_budget_error("image relay is configured for at most 20 inline images per request"),
            InlineImageBudget(max_images=20),
        )
        self.assertEqual(
            parse_inline_image_budget_error("image relay inline image exceeds the configured 20 MiB limit"),
            InlineImageBudget(max_image_bytes=20 * MIB),
        )

    def test_reads_errors_wrapped_in_json_or_bytes(self):
        error = {
            "type": "invalid_request_error",
            "code": "basispoints_request_invalid",
            "message": "image relay inline images exceed the configured 32 MiB request limit",
        }
        self.assertEqual(parse_inline_image_budget_error(error).max_total_bytes, 32 * MIB)
        self.assertEqual(
            parse_inline_image_budget_error(b'{"error":{"message":"image relay inline images exceed the configured 8 MiB request limit"}}'),
            InlineImageBudget(max_total_bytes=8 * MIB),
        )

    def test_other_errors_are_not_image_budget_errors(self):
        self.assertIsNone(parse_inline_image_budget_error({"code": "context_length_exceeded"}))
        self.assertIsNone(parse_inline_image_budget_error("Request too large"))
        self.assertIsNone(parse_inline_image_budget_error(None))


class LearnedInlineImageBudgetTests(unittest.TestCase):
    def setUp(self):
        forget_learned_inline_image_budgets()
        self.addCleanup(forget_learned_inline_image_budgets)

    def test_learns_and_merges_the_stricter_limits_per_channel(self):
        self.assertFalse(learn_inline_image_budget("ch_a", {"code": "rate_limit_exceeded"}))
        self.assertIsNone(learned_inline_image_budget("ch_a"))
        self.assertTrue(learn_inline_image_budget("ch_a", "inline images exceed the configured 32 MiB request limit"))
        self.assertTrue(learn_inline_image_budget("ch_a", "at most 20 inline images per request"))
        self.assertTrue(learn_inline_image_budget("ch_a", "inline images exceed the configured 48 MiB request limit"))
        self.assertEqual(learned_inline_image_budget("ch_a"), InlineImageBudget(max_total_bytes=32 * MIB, max_images=20))
        self.assertIsNone(learned_inline_image_budget("ch_b"))

    def test_learned_limits_expire(self):
        with patch.object(budget_module.time, "monotonic", return_value=1000.0):
            learn_inline_image_budget("ch_a", "at most 4 inline images per request")
        with patch.object(budget_module.time, "monotonic", return_value=1000.0 + budget_module.LEARNED_BUDGET_TTL_SECONDS - 1):
            self.assertIsNotNone(learned_inline_image_budget("ch_a"))
        with patch.object(budget_module.time, "monotonic", return_value=1000.0 + budget_module.LEARNED_BUDGET_TTL_SECONDS):
            self.assertIsNone(learned_inline_image_budget("ch_a"))


class FitInlineImagesTests(unittest.TestCase):
    def test_request_within_budget_is_untouched(self):
        items = [_user_image(_data_url(300 * KIB)), _user_image(_data_url(300 * KIB))]
        payload = {"input": items}
        self.assertEqual(fit_inline_images(payload, InlineImageBudget(max_total_bytes=1 * MIB, max_images=2)), 0)
        self.assertIs(payload["input"], items)

    def test_drops_oldest_images_in_steps_and_keeps_the_newest(self):
        urls = [_data_url(300 * KIB + 3 * index) for index in range(10)]
        items = [_user_image(url) if index % 2 else _tool_image(url, f"call_{index}") for index, url in enumerate(urls)]
        original_items = deepcopy(items)
        payload = {"input": items}

        # 10 × ~300 KiB against 2 MiB (minus margin): 4 images must go, which is one step.
        dropped = fit_inline_images(payload, InlineImageBudget(max_total_bytes=2 * MIB))

        self.assertEqual(dropped, DROP_STEP)
        self.assertEqual(_image_urls(payload["input"]), urls[DROP_STEP:])
        self.assertEqual(_notes(payload["input"]), DROP_STEP)
        # Text parts and item order survive; the caller's items are not modified.
        self.assertEqual(payload["input"][1]["content"][0], {"type": "input_text", "text": "look"})
        self.assertEqual(payload["input"][0]["output"][0]["type"], "input_text")
        self.assertEqual(_image_urls(items), urls)
        self.assertEqual(items, original_items)
        self.assertIs(payload["input"][-1], items[-1])

    def test_rounding_up_never_drops_the_newest_image(self):
        urls = [_data_url(600 * KIB) for _ in range(3)]
        payload = {"input": [_user_image(url) for url in urls]}

        dropped = fit_inline_images(payload, InlineImageBudget(max_total_bytes=1 * MIB))

        self.assertEqual(dropped, 2)
        self.assertEqual(_image_urls(payload["input"]), urls[-1:])

    def test_trimmed_history_is_stable_while_images_are_added(self):
        budget = InlineImageBudget(max_images=5)
        history = [_user_image(_data_url(3 * index + 3)) for index in range(6)]
        trimmed = []
        for _ in range(DROP_STEP):
            payload = {"input": list(history)}
            fit_inline_images(payload, budget)
            trimmed.append(payload["input"][:6])
            history.append(_user_image(_data_url(3)))
        # Every turn in the step sends the same first six items, so the cached prefix survives.
        self.assertTrue(all(prefix == trimmed[0] for prefix in trimmed))

    def test_count_limit(self):
        urls = [_data_url(3) for _ in range(7)]
        payload = {"input": [_user_image(url) for url in urls]}
        self.assertEqual(fit_inline_images(payload, InlineImageBudget(max_images=5)), DROP_STEP)
        self.assertEqual(_image_urls(payload["input"]), urls[DROP_STEP:])

    def test_oversized_old_image_is_dropped(self):
        big, small = _data_url(900 * KIB), _data_url(3)
        payload = {"input": [_user_image(big)] + [_user_image(small) for _ in range(5)]}
        self.assertEqual(fit_inline_images(payload, InlineImageBudget(max_image_bytes=512 * KIB)), DROP_STEP)
        self.assertNotIn(big, _image_urls(payload["input"]))

    def test_gives_up_when_the_newest_image_alone_does_not_fit(self):
        items = [_user_image(_data_url(300 * KIB)), _user_image(_data_url(1200 * KIB))]
        payload = {"input": items}
        self.assertEqual(fit_inline_images(payload, InlineImageBudget(max_total_bytes=1 * MIB)), -1)
        self.assertIs(payload["input"], items)

    def test_ignores_remote_urls_and_non_image_parts(self):
        items = [
            _user_image("https://example.com/a.png"),
            {"type": "function_call", "call_id": "c1", "name": "view_image", "arguments": "{}"},
            {"type": "message", "role": "user", "content": "plain text"},
            _user_image(_data_url(3)),
        ]
        payload = {"input": items}
        self.assertEqual(fit_inline_images(payload, InlineImageBudget(max_images=1)), 0)
        self.assertEqual(fit_inline_images({"input": "text only"}, InlineImageBudget(max_images=0)), 0)


if __name__ == "__main__":
    unittest.main()
