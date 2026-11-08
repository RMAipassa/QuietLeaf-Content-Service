import re
import unicodedata


_LOOKALIKES = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "і": "i", "ѕ": "s",
    "һ": "h", "ӏ": "l", "ѵ": "v", "ɡ": "g", "ο": "o", "ρ": "p", "ι": "i", "ո": "n",
    "\u200b": None, "\u200c": None, "\u200d": None, "\ufeff": None, "\u00ad": None,
})


def _notice_text(value: str) -> str:
    # Normalize only for comparison; the original story strings are never rewritten.
    return unicodedata.normalize("NFKC", value).casefold().translate(_LOOKALIKES).strip(" \t\r[]()")


_HIDDEN_STYLE = re.compile(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse))\s*(?:!important\s*)?(?:;|$)", re.I)


def is_hidden_html(element) -> bool:
    attributes = getattr(element, "attrs", None) or {}
    return "hidden" in attributes or bool(_HIDDEN_STYLE.search(str(attributes.get("style", ""))))


def remove_hidden_elements(root) -> None:
    """Exclude HTML that the source page does not display, irrespective of its wording."""
    for element in list(root.select("[hidden], [style]")):
        if element.attrs is not None and is_hidden_html(element):
            element.decompose()


def _notice_pattern(brand: str) -> re.Pattern:
    site = rf"(?:https?://)?(?:www\.)?{brand}(?:[ \t]*(?:\.|\(\.\))[ \t]*com|[ \t]+dot[ \t]+com)?/?"
    subject = r"(?:chapter|content|novel|story)"
    rules = (
        rf"(?:this\s+{subject}\s+(?:is|was)\s+)?(?:hosted|published|posted|available|updated\s+first)\s+(?:on|at|by)\s+{site}",
        rf"(?:the\s+)?(?:{subject}\s+)?source\s*(?:is|:|=)\s*{site}",
        rf"(?:the\s+)?source\s+of\s+this\s+{subject}\s+is\s+{site}",
        rf"{site}\s+is\s+the\s+(?:source|host)\s+(?:of|for)\s+this\s+{subject}",
        rf"(?:read|find|follow)(?:\s+the)?(?:\s+(?:latest|newest|more|full|complete|original))?\s+(?:chapters?|novels?|story)(?:\s+(?:only|exclusively))?\s+(?:on|at)\s+{site}(?:\s+for\s+free)?",
        rf"(?:please\s+)?visit\s+{site}(?:\s+for\s+(?:more|the\s+latest|the\s+fastest)\s+(?:chapters?|updates?|novels?))?",
        rf"new\s+chapters\s+come\s+out\s+(?:on|at)\s+{site}\s+first",
        rf"you\s+are\s+reading\s+(?:on|at)\s+{site}(?:,\s*the\s+official\s+source)?",
        rf"get\s+(?:faster|the\s+latest|the\s+fastest)\s+updates\s+(?:on|at)\s+{site}",
        rf"(?:enjoying\s+the\s+story\?\s*)?read\s+on\s+(?:at|on)\s+{site}",
        rf"this\s+{subject}\s+(?:is|was)\s+taken\s+from\s+{site}(?:\s+without\s+permission)?",
    )
    return re.compile(r"(?:" + "|".join(rules) + r")[.!…]*", re.I)


NOTICE_PATTERNS = {
    "novelping": _notice_pattern(r"novel\s*ping"),
    "wtr-lab": _notice_pattern(r"wtr[\s-]*lab"),
}
# Keep a spaced domain ("novelping . com") together; allow closing dialogue quotes.
SENTENCE_BREAK = re.compile(r'''(?<=[.!?])\s+(?!\s|com\b)|(?<=[.!?]["'”’»\)\]])\s+|(?<=[.!?]["'”’»\)\]]["'”’»\)\]])\s+|\n''', re.I)


def clean_chapter_text(source_id: str, text: str) -> str:
    """Drop recognized source notices, keeping unrelated prose and formatting."""
    pattern = NOTICE_PATTERNS.get(source_id)
    if pattern is None:
        return text
    output = []
    changed = False
    for paragraph in re.split(r"\n\s*\n", text):
        sentences = SENTENCE_BREAK.split(paragraph)
        normalized = [_notice_text(sentence) for sentence in sentences]
        notices = [bool(pattern.fullmatch(sentence)) for sentence in normalized]
        for index in range(len(sentences) - 1):
            if (re.fullmatch(r"enjoying\s+the\s+story\?", normalized[index]) and notices[index + 1]
                    and re.match(r"read\s+on\s+", normalized[index + 1])):
                notices[index] = True
        kept = [sentence for sentence, notice in zip(sentences, notices) if not notice]
        if len(kept) == len(sentences):
            output.append(paragraph)
        else:
            changed = True
            remainder = " ".join(kept).strip()
            if remainder:
                output.append(remainder)
    return "\n\n".join(output).strip() if changed else text
