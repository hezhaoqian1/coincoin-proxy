import unittest

from app.main import app


class RootResponsesRouteTests(unittest.TestCase):
    def test_root_responses_supports_get_and_post(self):
        methods = set()
        for route in app.routes:
            if getattr(route, "path", None) == "/responses":
                methods.update(getattr(route, "methods", set()))

        self.assertIn("GET", methods)
        self.assertIn("POST", methods)
