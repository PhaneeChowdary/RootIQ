"""Deterministic guards on agent output.

unverified_ids  finds incident and ticket ids that the model wrote but no tool returned, for example an invented
                "INC-1234" or "a1b2c3d4". The reviewer and the final step act on them.
unverified_log_times
                finds log times with a date and a time that the model quoted but no tool returned. A small model
                can invent whole log lines. A real quoted line always has a time that is in the tool output.
misquoted_lines finds quoted log lines with a time, in `backticks` or "quotes", whose words or time zone are not the
                same as the real line at that second. A real time with invented words is still an invented line.
Deadline        a time limit for one run. The team checks it between model calls and between agents.
"""
import re
import time

# Ticket keys, for example RI-14, INC-1234 or INV-2023-09-15-001, and incident ids of 8 hex characters, for example a473f2aa.
_KEY = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d[\w-]*")
_HEX_ID = re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{8}\b")
# Standard names that look like keys but are not identifiers.
_NOT_IDS = {"UTF-8", "UTF-16", "SHA-1", "SHA-256", "SHA-512", "ISO-8601", "ISO-8859", "HTTP-1", "HTTP-2", "TLS-1",
            "IPV-4", "IPV-6", "MD-5", "AES-256", "RSA-2048", "COVID-19"}


def unverified_ids(text: str, known_text: str) -> list[str]:
    """Ids in `text` that never appear in `known_text`: the tool output, the case and the lessons."""
    known = known_text.lower()
    found = [m.group(0).rstrip("-.") for m in _KEY.finditer(text)] + _HEX_ID.findall(text)
    return [i for i in dict.fromkeys(found) if i.upper() not in _NOT_IDS and i.lower() not in known]


# A log timestamp with a date and a time: 2026-10-02T07:41:17Z, 2026-10-02 07:41:17 or 2026-10-02T07:41
_STAMP = re.compile(r"\b(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2})?)")


def _stamps(text: str) -> list[str]:
    return [f"{d}T{t}" for d, t in _STAMP.findall(text)]


def unverified_log_times(text: str, known_text: str) -> list[str]:
    """Log times in `text` that no tool output contains. The date and time can have a T or a space between them."""
    known = set()
    for stamp in _stamps(known_text):
        known.add(stamp)
        known.add(stamp[:16])  # the same minute, for a quote without seconds
    return [s for s in dict.fromkeys(_stamps(text)) if s not in known and s[:16] not in known]


_QUOTE = re.compile(r"`([^`]{12,400})`|\"([^\"]{12,400})\"|“([^”]{12,400})”")
_OFFSET = re.compile(r"\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?([+-]\d{2}:?\d{2})")
_WORD = re.compile(r"[a-z][a-z0-9_]{2,}")
_LEVELS = {"error", "warn", "warning", "info", "debug"}


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _LEVELS}


def misquoted_lines(text: str, known_text: str, min_overlap: float = 0.6) -> list[str]:
    """Quoted log lines whose time is real but whose words or time zone are not. Each result names the time."""
    lines_by_second: dict[str, list[str]] = {}
    for line in known_text.splitlines():
        for stamp in _stamps(line):
            lines_by_second.setdefault(stamp, []).append(line)
    bad = []
    for match in _QUOTE.finditer(text):
        quote = next(g for g in match.groups() if g)
        stamps = _stamps(quote)
        if not stamps or len(stamps[0]) < 19:  # only quotes of one line with a time to the second
            continue
        stamp = stamps[0]
        candidates = lines_by_second.get(stamp)
        if not candidates:
            continue  # an unknown time: unverified_log_times reports it
        offset = _OFFSET.search(quote)
        if offset and offset.group(1).replace(":", "") not in ("+0000", "-0000"):
            bad.append(f"{stamp} (the log time is UTC, not {offset.group(1)})")
            continue
        message = _words(_STAMP.sub(" ", quote))
        if len(message) < 3:
            continue
        best = max(len(message & _words(line)) / len(message) for line in candidates)
        if best < min_overlap:
            bad.append(f"{stamp} (the words differ from the real line)")
    return list(dict.fromkeys(bad))


# A code reference: path:line, for example src/gateway/client.py:42 or Dockerfile:3
_CODE_REF = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*(?:[\w-]+\.[A-Za-z][\w]{0,9}|Dockerfile|Makefile|Procfile)):(\d{1,5})\b")


_CODE_PATH = re.compile(r"(?<![\w/.:-])((?:[\w.-]+/)+[\w-]+\.[A-Za-z][\w]{0,9}|[\w-]+\.(?:py|js|ts|tsx|go|java|rb|rs|yaml|yml|toml|json|tf|sql)|Dockerfile)(?![\w/-])")


def unverified_code_refs(text: str, exists) -> list[str]:
    """Code references whose file or line does not exist: path:line, and also a file path without a line.
    `exists(path, line)` asks the code source. Line 1 checks only that the file exists."""
    bad = []
    for path, line in dict.fromkeys(_CODE_REF.findall(text)):
        if not path[0].isdigit() and not exists(path, int(line)):
            bad.append(f"{path}:{line}")
    for path in dict.fromkeys(_CODE_PATH.findall(_CODE_REF.sub(" ", text))):
        if "/" in path or path.split(".")[-1] in ("py", "js", "ts", "go", "yaml", "yml") or path == "Dockerfile":
            if not path[0].isdigit() and "://" not in path and not exists(path, 1):
                bad.append(path)
    return bad


# path:line and then a quote of the code on that line, for example `src/db.js:12` - `pool.get(timeout)`.
# A quote starts with a character that is not a space. Else the closing tick of `path:line` starts a quote of the text after it.
_CODE_QUOTE = re.compile(_CODE_REF.pattern + r"`?[ \t]*[-–—:=]?[ \t]*`([^`\s][^`\n]{3,})`")
_BLOCK = re.compile(r"```[\w+.-]*\n(.*?)```", re.S)
# A label before a code block that shows code which is not in the file yet
_NEW_CODE = re.compile(r"\b(new|proposed|after|fix|fixed|suggest\w*|should|replace\w*|change to|patch)\b", re.I)
_IDENT = re.compile(r"[A-Za-z_][\w]+")


def _flat(text: str) -> str:
    return " ".join(text.split())


def misquoted_code(text: str, lines_of, window: int = 3, min_overlap: float = 0.8) -> list[str]:
    """Quoted code that is not in the file: `path:line` with a quote next to it, and a code block after a path,
    but not a block of new code. `lines_of(path)` returns the lines of the file, or None for an unknown file.
    unverified_code_refs reports an unknown file. Each result names the place."""
    bad = []
    for path, line, quote in _CODE_QUOTE.findall(text):
        lines = None if path[0].isdigit() else lines_of(path)
        words = set(_IDENT.findall(quote))
        if not lines or len(words) < 2:
            continue
        at = int(line)
        near = lines[max(0, at - 1 - window): at + window]
        if _flat(quote) in _flat(" ".join(near)):
            continue
        if len(words & set(_IDENT.findall(" ".join(near)))) / len(words) < min_overlap:
            bad.append(f"{path}:{line} (the quoted code differs from the file)")
    for block in _BLOCK.finditer(text):
        before = text[max(0, block.start() - 300): block.start()]
        label = next((x for x in reversed(before.splitlines()) if x.strip()), "")
        paths = [m.group(1) for m in _CODE_REF.finditer(before)] or _CODE_PATH.findall(before)
        if not paths or _NEW_CODE.search(label):
            continue
        lines = lines_of(paths[-1])
        quoted = [_flat(x) for x in block.group(1).splitlines() if len(_IDENT.findall(x)) >= 2]
        if not lines or not quoted:
            continue
        file_lines = [_flat(x) for x in lines]
        found = sum(any(q in f for f in file_lines) for q in quoted)
        if found / len(quoted) < 0.6:
            bad.append(f"{paths[-1]} (the quoted code is not in the file)")
    return list(dict.fromkeys(bad))


_FENCE = re.compile(r"```.*?```", re.S)
_TICKS = re.compile(r"`([^`\n]{25,})`")


def without_code(text: str) -> str:
    """The text without code blocks and long code quotes, for cloud models when the code must stay local.
    The path:line references stay, so that the other agents can still name the place."""
    text = _FENCE.sub("[code kept on this computer]", text)
    return _TICKS.sub(lambda m: m.group(0) if _CODE_REF.fullmatch(m.group(1).strip()) else "[code]", text)


class Deadline:
    def __init__(self, seconds: float | None) -> None:
        self.seconds = seconds
        self.started = time.monotonic()

    @property
    def expired(self) -> bool:
        return self.seconds is not None and time.monotonic() - self.started >= self.seconds
