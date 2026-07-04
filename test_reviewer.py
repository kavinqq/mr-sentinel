"""Tests for reviewer pure helpers."""
import unittest

import reviewer


class TestCompletionText(unittest.TestCase):
    FINDINGS = [{"severity": "high"}, {"severity": "high"}, {"severity": "low"}]

    def test_english_default(self):
        text = reviewer.completion_text("group/backend-app", 45, "https://gl/mr/45",
                                        self.FINDINGS, 3, language="en")
        self.assertIn("AI review complete", text)
        self.assertIn("group/backend-app", text)
        self.assertIn("!45", text)
        self.assertIn("🔴2", text)
        self.assertIn("🟠0", text)
        self.assertIn("🟡1", text)
        self.assertIn("https://gl/mr/45", text)

    def test_chinese_when_configured(self):
        text = reviewer.completion_text("group/backend-app", 45, None,
                                        self.FINDINGS, 3, language="zh-TW")
        self.assertIn("AI Review 完成", text)

    def test_signature_includes_engine_label(self):
        sig = reviewer.build_signature("scanned by X, vetted by Y")
        self.assertIn("mr-sentinel", sig)
        self.assertIn("scanned by X", sig)


if __name__ == "__main__":
    unittest.main()
