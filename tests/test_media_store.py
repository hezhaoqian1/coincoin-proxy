import unittest

from app.media_store import extract_media_urls


class MediaStoreTests(unittest.TestCase):
    def test_extracts_image_urls_from_supported_response_shapes(self) -> None:
        self.assertEqual(
            extract_media_urls(
                "image",
                {
                    "data": [
                        {"url": "https://example.com/a.png"},
                        {"image_url": {"url": "https://example.com/b.png"}},
                        {"download_url": "http://example.com/c.png"},
                        {"b64_json": "not-stored"},
                        {"url": "data:image/png;base64,skip"},
                    ],
                    "result": {
                        "images": [{"url": "https://example.com/d.png"}],
                        "output": {"data": [{"url": "https://example.com/e.png"}]},
                    },
                },
            ),
            [
                "https://example.com/a.png",
                "https://example.com/b.png",
                "http://example.com/c.png",
                "https://example.com/d.png",
                "https://example.com/e.png",
            ],
        )

    def test_extracts_video_urls_from_nested_task_responses(self) -> None:
        self.assertEqual(
            extract_media_urls(
                "video",
                {
                    "code": 0,
                    "data": {
                        "status": "completed",
                        "output": {"url": "https://example.com/final.mp4"},
                    },
                },
            ),
            ["https://example.com/final.mp4"],
        )
        self.assertEqual(
            extract_media_urls(
                "video",
                {
                    "result": {
                        "data": {
                            "output": {"video_url": {"url": "https://example.com/result.mp4"}},
                        },
                    },
                },
            ),
            ["https://example.com/result.mp4"],
        )

    def test_ignores_unsupported_media_types_and_inline_data(self) -> None:
        self.assertEqual(extract_media_urls("audio", {"url": "https://example.com/a.mp3"}), [])
        self.assertEqual(extract_media_urls("image", {"data": [{"url": "data:image/png;base64,abc"}]}), [])
        self.assertEqual(extract_media_urls("video", {"output": {"url": "data:video/mp4;base64,abc"}}), [])


if __name__ == "__main__":
    unittest.main()
