"""Masks personal data and secrets before text goes to a cloud model.

The agents read logs, tickets and customer messages. These can contain emails, phone numbers, card numbers and
secrets. Before each call to a cloud model, for example Groq, Gemini, Claude, GPT or Grok, the app replaces
these values with labels such as [email]. The local model gets the text as it is: the text stays on this computer.

What is masked:
- emails                        ann@acme.io                  -> [email]
- card numbers, Luhn checked    4111 1111 1111 1111          -> [card]
- phone numbers, 9+ digits      +1 415-555-0100              -> [phone]
- US social security numbers    123-45-6789                  -> [ssn]
- IBAN bank accounts            DE89 3704 0044 0532 0130 00  -> [iban]
- secrets: bearer tokens, JWTs, AWS keys, private keys, password= / token= / api_key= values -> [secret]
- IPv4 addresses: the last part only, so the network stays visible  203.0.113.7 -> 203.0.113.x

Times, IDs, Jira keys, error codes and numbers in log fields are not changed. The investigation needs them, and the
guard against invented log lines compares times.

ROOTIQ_PRIVACY_MASKING: "cloud", the default, masks for cloud models only. "off" masks nothing.
"""

import re
import threading
from collections import Counter

_counts: Counter = Counter()
_lock = threading.Lock()

EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}")
AWS_KEY = re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b")
KEY_VALUE = re.compile(r"(?i)\b((?:pass(?:word|wd)?|pwd|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret)\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&]+)")
PROVIDER_KEY = re.compile(r"\b(sk-[A-Za-z0-9_-]{16,}|xai-[A-Za-z0-9]{16,}|AIza[0-9A-Za-z_-]{30,}|gh[pousr]_[A-Za-z0-9]{30,}|xox[abpr]-[A-Za-z0-9-]{10,})\b")
IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){3,7}(?:[ ]?[A-Z0-9]{1,3})?\b")
SSN = re.compile(r"(?<![\w-])\d{3}-\d{2}-\d{4}(?![\w-])")
CARD = re.compile(r"(?<![\w-])\d(?:[ -]?\d){12,18}(?![\w-])")
PHONE = re.compile(r"(?<![\w:.-])(?:\+\d{1,3}[ .-]?)?(?:\(\d{2,4}\)[ .-]?)?\d{2,4}[ .-]\d{3,4}(?:[ .-]\d{2,4}){0,2}(?![\w:.-])")
IPV4 = re.compile(r"\b((?:25[0-5]|2[0-4]\d|1?\d?\d)\.(?:25[0-5]|2[0-4]\d|1?\d?\d)\.(?:25[0-5]|2[0-4]\d|1?\d?\d))\.(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")


def _luhn(digits: str) -> bool:
    total = 0
    for i, d in enumerate(reversed(digits)):
        n = int(d) * (2 if i % 2 else 1)
        total += n - 9 if n > 9 else n
    return total % 10 == 0


def redact(text: str) -> tuple[str, Counter]:
    """The text with personal data and secrets masked, and how many of each kind."""
    found: Counter = Counter()
    if not text:
        return text, found

    def sub(pattern, label, value_check=None, keep_group=None):
        nonlocal text

        def repl(match):
            if value_check and not value_check(match.group(0)):
                return match.group(0)
            found[label.strip("[]")] += 1
            return (match.group(keep_group) if keep_group else "") + label
        text = pattern.sub(repl, text)

    sub(PRIVATE_KEY, "[secret]")
    sub(JWT, "[secret]")
    sub(BEARER, "[secret]")
    sub(AWS_KEY, "[secret]")
    sub(PROVIDER_KEY, "[secret]")
    sub(KEY_VALUE, "[secret]", lambda v: not v.endswith("[secret]"), keep_group=1)  # not the same secret twice
    sub(EMAIL, "[email]")
    sub(IBAN, "[iban]", lambda v: sum(c.isdigit() for c in v) >= 10)
    sub(SSN, "[ssn]")
    sub(CARD, "[card]", lambda v: _luhn(re.sub(r"\D", "", v)) and len(set(re.sub(r"\D", "", v))) > 1)
    sub(PHONE, "[phone]", lambda v: 9 <= sum(c.isdigit() for c in v) <= 15 and not re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", v))

    def ip(match):
        found["ip"] += 1
        return f"{match.group(1)}.x"
    text = IPV4.sub(ip, text)
    return text, found


def note(found: Counter) -> None:
    if found:
        with _lock:
            _counts.update(found)


def masked_counts() -> dict[str, int]:
    """How many values of each kind the app masked since it started, for /metrics and the Security tab."""
    with _lock:
        return dict(_counts)


def redact_messages(value):
    """Mask a model input: a string, a prompt value or a list of LangChain messages. The original is not changed."""
    from langchain_core.messages import BaseMessage
    from langchain_core.prompt_values import PromptValue

    if isinstance(value, str):
        text, found = redact(value)
        note(found)
        return text
    if isinstance(value, PromptValue):
        value = value.to_messages()
    if isinstance(value, list):
        return [redact_message(m) if isinstance(m, BaseMessage) else m for m in value]
    return value


def redact_message(message):
    content = message.content
    found: Counter = Counter()
    if isinstance(content, str):
        content, found = redact(content)
    elif isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                text, part_found = redact(part["text"])
                found.update(part_found)
                part = {**part, "text": text}
            elif isinstance(part, str):
                part, part_found = redact(part)
                found.update(part_found)
            parts.append(part)
        content = parts
    note(found)
    return message.model_copy(update={"content": content}) if found else message
