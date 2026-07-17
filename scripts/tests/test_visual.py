import re
import unittest
from html.parser import HTMLParser
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[2]
VISUAL = SKILL_ROOT / "assets" / "framework-visual.html"


class VisualParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.tags = []
        self.attrs = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs.extend(attrs)
        for name, value in attrs:
            if name == "id":
                self.ids.add(value)

    def handle_data(self, data):
        if data.strip():
            self.text.append(data.strip())


class FrameworkVisualTests(unittest.TestCase):
    def setUp(self):
        self.source = VISUAL.read_text(encoding="utf-8")
        self.parser = VisualParser()
        self.parser.feed(self.source)

    def test_visual_asset_exists_and_is_self_contained(self):
        self.assertTrue(VISUAL.is_file())
        self.assertIn('id="ag-router-framework"', self.source)
        self.assertIn("document.getElementById(\"ag-router-framework\")", self.source)
        self.assertNotRegex(
            self.source,
            r"\b(fetch|XMLHttpRequest|WebSocket|EventSource)\b",
        )
        self.assertNotRegex(self.source, r"https?://")

    def test_visual_exposes_models_efforts_and_decision_controls(self):
        text = "\n".join(self.parser.text)
        for expected in (
            "Luna",
            "Terra",
            "Sol",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
            "ultra",
            "Fingerprint",
            "Gates",
            "Evidencia",
            "Pareto",
            "Validar",
        ):
            self.assertIn(expected, text + self.source)
        for expected_id in (
            "router-profile",
            "router-complexity",
            "router-risk",
            "router-ambiguity",
            "router-budget",
            "router-latency",
            "router-map",
            "router-choice",
        ):
            self.assertIn(expected_id, self.parser.ids)

    def test_visual_javascript_queries_existing_static_ids(self):
        queried_ids = set(re.findall(r"#(router-[a-z-]+)", self.source))
        dynamic_suffixes = {
            "router-complexity-value",
            "router-risk-value",
            "router-ambiguity-value",
            "router-budget-value",
            "router-latency-value",
        }
        self.assertTrue(queried_ids - dynamic_suffixes <= self.parser.ids)
        for dynamic_id in dynamic_suffixes:
            self.assertIn(dynamic_id, self.parser.ids)


if __name__ == "__main__":
    unittest.main()
