import re


def _notice_pattern(brand: str) -> re.Pattern:
    site = rf"(?:https?://)?(?:www\.)?{brand}(?:\.com)?/?"
    subject = r"(?:chapter|content|novel|story)"
    rules = (
        rf"(?:this\s+{subject}\s+(?:is|was)\s+)?(?:hosted|published|posted|available)\s+(?:on|at|by)\s+{site}",
        rf"(?:the\s+)?(?:{subject}\s+)?source\s*(?:is|:|=)\s*{site}",
        rf"{site}\s+is\s+the\s+(?:source|host)\s+(?:of|for)\s+this\s+{subject}",
        rf"(?:read|find)(?:\s+the)?(?:\s+(?:latest|more|full|complete|original))?\s+(?:chapters?|novels?|story)(?:\s+(?:only|exclusively))?\s+(?:on|at)\s+{site}(?:\s+for\s+free)?",
        rf"(?:please\s+)?visit\s+{site}(?:\s+for\s+(?:more|the\s+latest|the\s+fastest)\s+(?:chapters?|updates?|novels?))?",
    )
    return re.compile(r"(?:" + "|".join(rules) + r")[.!…]*", re.I)


NOTICE_PATTERNS = {
    "novelping": _notice_pattern(r"novel\s*ping"),
    "wtr-lab": _notice_pattern(r"wtr[\s-]*lab"),
}


def clean_chapter_text(source_id: str, text: str) -> str:
    """Drop recognized source notices, keeping unrelated prose and formatting."""
    pattern = NOTICE_PATTERNS.get(source_id)
    if pattern is None:
        return text
    output = []
    changed = False
    for paragraph in re.split(r"\n\s*\n", text):
        sentences = re.split(r"(?<=[.!?])\s+|\n", paragraph)
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
