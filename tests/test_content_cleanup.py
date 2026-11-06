import pytest

from quietleaf_service.archive import ArchiveStore
from quietleaf_service.content import clean_chapter_text


@pytest.mark.parametrize(("provider", "brand"), [("novelping", "NovelPing"), ("wtr-lab", "WTR-LAB")])
@pytest.mark.parametrize("notice", [
    "Hosted on {brand}.",
    "This chapter is hosted on {brand}.com.",
    "Source is {brand}.",
    "[Source: {brand}]",
    "{brand} is the source of this chapter.",
    "Read more chapters on {brand}.com.",
    "Visit https://www.{brand}.com for the latest updates.",
])
def test_removes_known_source_notices(provider, brand, notice):
    text = notice.format(brand=brand) + "\n\nAn original story paragraph."
    assert clean_chapter_text(provider, text) == "An original story paragraph."


def test_preserves_story_sentences_next_to_a_notice():
    text = "The door opened. This chapter is hosted on NovelPing.com. Mira stepped outside.\n\nThe road was quiet."
    assert clean_chapter_text("novelping", text) == "The door opened. Mira stepped outside.\n\nThe road was quiet."


@pytest.mark.parametrize("text", [
    '"The source is NovelPing," she said.',
    "He opened NovelPing to read a story.\n\nThe door opened.\nA second line.",
    "Hosted on NovelPing.com.evil.",
    "Source: A mysterious letter.",
])
def test_preserves_story_mentions_and_unrelated_content(text):
    assert clean_chapter_text("novelping", text) == text


def test_only_filters_the_matching_provider_and_serves_existing_archives_cleanly(tmp_path):
    text = "An original story paragraph.\n\nHosted on NovelPing."
    assert clean_chapter_text("collection", text) == text
    assert clean_chapter_text("wtr-lab", text) == text
    store = ArchiveStore(tmp_path)
    store.save_book("novelping", {"id": "story", "title": "Story"})
    store.save_chapters("novelping", "story", [{"id": "chapter-1", "title": "One", "ordinal": 0}])
    store.save_text("novelping", "story", "chapter-1", text)
    assert store.text("novelping", "story", "chapter-1") == "An original story paragraph."
