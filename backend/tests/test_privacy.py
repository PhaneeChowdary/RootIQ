import unittest

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from backend.app.privacy import redact, redact_messages

LOG = '2026-10-03T19:54:17Z ERROR [gateway-client] TLS handshake failed host=pay.gateway.example latency_ms=1834 status=502 case=KAN-12 incident=07a2b89e'


class RedactTests(unittest.TestCase):
    def test_personal_data_is_masked(self):
        cases = [
            ("Contact ann.lee@acme.io now", "Contact [email] now", "email"),
            ("card 4111 1111 1111 1111 declined", "card [card] declined", "card"),
            ("card=4111-1111-1111-1111", "card=[card]", "card"),
            ("call +1 415-555-0100 today", "call [phone] today", "phone"),
            ("call (415) 555-0100", "call [phone]", "phone"),
            ("ssn 123-45-6789", "ssn [ssn]", "ssn"),
            ("iban DE89 3704 0044 0532 0130 00 ok", "iban [iban] ok", "iban"),
            ("client 203.0.113.7 blocked", "client 203.0.113.x blocked", "ip"),
        ]
        for text, expected, kind in cases:
            with self.subTest(text=text):
                masked, found = redact(text)
                self.assertEqual(masked, expected)
                self.assertEqual(found[kind], 1)

    def test_secrets_are_masked(self):
        for text in ("Authorization: Bearer abcdefghijklmnop123", "password=hunter2!", 'api_key: "sk-live-123"',
                     "token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
                     "key AKIAIOSFODNN7EXAMPLE used", "GEMINI=AIzaSyA1234567890abcdefghijklmnopqrstu",
                     "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"):
            with self.subTest(text=text[:30]):
                masked, found = redact(text)
                self.assertEqual(found["secret"], 1, masked)
                self.assertIn("[secret]", masked)
        self.assertEqual(redact("password=hunter2 user=ann")[0], "password=[secret] user=ann")

    def test_log_facts_stay(self):
        masked, found = redact(LOG)
        self.assertEqual(masked, LOG)
        self.assertFalse(found)
        for text in ("2026-10-03 19:54:17", "took 1234567 ms", "order 1234567890123 shipped", "v2026.10.03-1", "pool size=2 max=50"):
            with self.subTest(text=text):
                self.assertEqual(redact(text)[0], text)

    def test_messages_are_copied_not_changed(self):
        human = HumanMessage("Customer ann@acme.io cannot pay")
        tool = ToolMessage(content="ERROR card 4111111111111111 refused", tool_call_id="1")
        ai = AIMessage(content=[{"type": "text", "text": "I will email bob@acme.io"}])
        out = redact_messages([human, tool, ai])
        self.assertEqual(out[0].content, "Customer [email] cannot pay")
        self.assertEqual(out[1].content, "ERROR card [card] refused")
        self.assertEqual(out[1].tool_call_id, "1")
        self.assertEqual(out[2].content[0]["text"], "I will email [email]")
        self.assertEqual(human.content, "Customer ann@acme.io cannot pay")
        self.assertEqual(redact_messages("mail ann@acme.io"), "mail [email]")


if __name__ == "__main__":
    unittest.main()
