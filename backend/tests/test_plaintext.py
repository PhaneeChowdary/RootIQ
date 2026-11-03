import unittest

from backend.app.agent.plaintext import plain_text


class PlainTextTests(unittest.TestCase):
    def test_markdown_marks_are_removed_and_text_stays(self):
        text = "### Summary\nCause in `shared-eu`.\n\n\n\n* **Event**: a *large* campaign.\n  * see [docs](https://x.y)"
        self.assertEqual(plain_text(text), "Summary\nCause in shared-eu.\n\n- Event: a large campaign.\n  - see docs (https://x.y)")

    def test_plain_text_does_not_change(self):
        text = "Hello,\n\nWe see 2 * 3 errors per minute. We will update you at 10:00.\n- Next step: retry"
        self.assertEqual(plain_text(text), text)


if __name__ == "__main__":
    unittest.main()
