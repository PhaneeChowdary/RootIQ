"""Change Markdown from a model into plain text.

Customer emails and Jira comments do not show Markdown. Thus "**Cause**" must become "Cause".
The rules:
- A heading such as "### Summary" becomes its text.
- Bold, italic and code marks go away. The text stays.
- A "*" or "+" bullet becomes "- ".
- A link becomes "text ". Address.
- More than one empty line becomes one empty line."""

import re

_RULES = [
    (re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+", re.M), ""),                       # headings
    (re.compile(r"^([ \t]*)[*+][ \t]+", re.M), r"\1- "),                       # bullets
    (re.compile(r"\*\*(.+?)\*\*|__(.+?)__", re.S), lambda m: m.group(1) or m.group(2)),  # bold
    (re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)\*(?!\w)"), r"\1"),          # italic
    (re.compile(r"`([^`\n]+)`"), r"\1"),                                  # inline code
    (re.compile(r"^```[^\n]*\n?|^```\s*$", re.M), ""),                    # code fences
    (re.compile(r"\[([^\]]+)\]\((\S+?)\)"), r"\1 (\2)"),                  # links
    (re.compile(r"\n{3,}"), "\n\n"),                                      # empty lines
]


def plain_text(text: str) -> str:
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text.strip()
