import re


WTRLAB_TITLE_ANNOTATION = re.compile(r"%\{([^{}|]+)\|[^{}]*\}")


def display_title(source_id: str, title: str) -> str:
    """Keep WTR-LAB's visible label, omitting its alternate-name metadata."""
    if source_id != "wtr-lab":
        return title
    return WTRLAB_TITLE_ANNOTATION.sub(lambda match: match.group(1).strip() or match.group(0), title).strip()
