import re


WTRLAB_TITLE_ANNOTATION = re.compile(r"%\{([^{}|]+)\|[^{}]*\}")


def display_title(source_id: str, title: str) -> str:
    """Normalize source-specific display escaping and alternate-name metadata."""
    if source_id == "novelping":
        return title.replace(r"\%", "%")
    if source_id != "wtr-lab":
        return title
    return WTRLAB_TITLE_ANNOTATION.sub(lambda match: match.group(1).strip() or match.group(0), title).strip()


def publication_status(value: object) -> str:
    if not isinstance(value, str):
        return "unknown"
    status = re.sub(r"[\s_-]+", "", value.strip().casefold())
    if status in {"complete", "completed", "finished"}:
        return "completed"
    if status in {"ongoing", "inprogress", "updating", "serializing", "serialising"}:
        return "ongoing"
    return "unknown"
