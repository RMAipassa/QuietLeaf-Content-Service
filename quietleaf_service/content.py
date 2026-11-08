import re


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
        kept = [sentence for sentence in sentences
                if not pattern.fullmatch(sentence.strip(" \t\r[]()"))]
        if len(kept) == len(sentences):
            output.append(paragraph)
        else:
            changed = True
            remainder = " ".join(kept).strip()
            if remainder:
                output.append(remainder)
    return "\n\n".join(output).strip() if changed else text
