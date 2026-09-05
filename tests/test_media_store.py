import unittest
import json
import tempfile
from datetime import datetime

from app.config import settings
from app.media_store import extract_media_urls, record_media_artifacts
from app.models import MediaArtifact


class _FakeSession:
    def __init__(self) -> None:
        self.items = []

    def add(self, item) -> None:
        self.items.append(item)


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


class MediaStoreAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_records_b64_image_as_short_content_url(self) -> None:
        original_dir = settings.media_artifact_storage_dir
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                settings.media_artifact_storage_dir = tmpdir
                session = _FakeSession()
                created = await record_media_artifacts(
                    session,
                    user_id="u_test",
                    api_key_id="k_test",
                    media_type="image",
                    endpoint="image-jobs/generations",
                    model="gpt-image-2",
                    provider_model="gpt-image-2",
                    payload={"data": [{"b64_json": "iVBORw0KGgo="}]},
                    source_type="image_job",
                    source_id="ij_test",
                    cost_cents=110,
                    completed_at=datetime.utcnow(),
                )
            finally:
                settings.media_artifact_storage_dir = original_dir

        self.assertEqual(created, 1)
        self.assertEqual(len(session.items), 1)
        artifact = session.items[0]
        self.assertIsInstance(artifact, MediaArtifact)
        self.assertTrue(artifact.url.startswith("/v1/media-artifacts/"))
        self.assertTrue(artifact.url.endswith("/content"))
        self.assertNotIn("iVBOR", artifact.url)
        metadata = json.loads(artifact.metadata_json)
        self.assertEqual(metadata["source"], "b64_json")
        self.assertEqual(metadata["content_type"], "image/png")
        self.assertEqual(artifact.cost_cents, 110)


if __name__ == "__main__":
    unittest.main()
