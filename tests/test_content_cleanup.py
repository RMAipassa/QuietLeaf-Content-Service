import pytest

from quietleaf_service.archive import ArchiveStore
from quietleaf_service.content import clean_chapter_text
from quietleaf_service.content import remove_hidden_elements
from quietleaf_service.app import make_app
from fastapi.testclient import TestClient
from bs4 import BeautifulSoup


@pytest.mark.parametrize(("provider", "brand"), [("novelping", "NovelPing"), ("wtr-lab", "WTR-LAB")])
@pytest.mark.parametrize("notice", [
    "Hosted on {brand}.",
    "This chapter is hosted on {brand}.com.",
    "Source is {brand}.",
    "[Source: {brand}]",
    "{brand} is the source of this chapter.",
    "Read more chapters on {brand}.com.",
    "Visit https://www.{brand}.com for the latest updates.",
    "This story is hosted at {brand}.com",
    "Find the original chapter at {brand}(.)com.",
    "Follow the newest chapters on {brand}(.)com.",
    "New chapters come out on {brand} first.",
    "You are reading on {brand}(.)com, the official source.",
    "This chapter is updated first at {brand} . com.",
    "Follow the newest chapters on {brand} dot com.",
    "Get faster updates on {brand} . com.",
    "The source of this content is {brand}.com.",
    "Read on at {brand}.com.",
    "Enjoying the story? Read on at {brand}.com.",
    "This chapter was taken from {brand}(.)com without permission.",
])
def test_removes_known_source_notices(provider, brand, notice):
    text = notice.format(brand=brand) + "\n\nAn original story paragraph."
    assert clean_chapter_text(provider, text) == "An original story paragraph."


def test_preserves_story_sentences_next_to_a_notice():
    text = "The door opened. This chapter is hosted on NovelPing.com. Mira stepped outside.\n\nThe road was quiet."
    assert clean_chapter_text("novelping", text) == "The door opened. Mira stepped outside.\n\nThe road was quiet."


@pytest.mark.parametrize("dialogue", ['He said, “Stop!”', 'He said, "Stop!"', "He said, ‘Stop!’", "He said, 'Stop!'", '(He whispered, “Stop!”)'])
def test_removes_every_notice_in_a_paragraph_after_dialogue(dialogue):
    text = (dialogue + " This story is hosted at novelping.com. Mira stepped outside. "
            "Find the original chapter at novelping(.)com. Follow the newest chapters on novelping(.)com. "
            "New chapters come out on NovelPing first. The road was quiet.")
    assert clean_chapter_text("novelping", text) == dialogue + " Mira stepped outside. The road was quiet."


@pytest.mark.parametrize("text", [
    '"The source is NovelPing," she said.',
    "He opened NovelPing to read a story.\n\nThe door opened.\nA second line.",
    "Hosted on NovelPing.com.evil.",
    "Source: A mysterious letter.",
])
def test_preserves_story_mentions_and_unrelated_content(text):
    assert clean_chapter_text("novelping", text) == text


def test_only_filters_the_matching_provider_and_serves_existing_archives_cleanly(tmp_path):
    text = 'He said, “Stop!” This story is hosted at novelping.com.\n\nFollow the newest chapters on novelping(.)com.\n\nThe door opened. Hosted on NovelPing.'
    assert clean_chapter_text("collection", text) == text
    assert clean_chapter_text("wtr-lab", text) == text
    store = ArchiveStore(tmp_path)
    store.save_book("novelping", {"id": "story", "title": "Story"})
    store.save_chapters("novelping", "story", [{"id": "chapter-1", "title": "One", "ordinal": 0}])
    store.save_text("novelping", "story", "chapter-1", text)
    assert store.text("novelping", "story", "chapter-1") == 'He said, “Stop!”\n\nThe door opened.'


@pytest.mark.parametrize("site", ["novelping.com", "n\u043evelping.com", "novel\u0440ing dot com", "novel\u200bping(.)com", "ｎｏｖｅｌｐｉｎｇ．ｃｏｍ"])
def test_reported_two_sentence_notice_with_obfuscated_source_names(site):
    text = f"The traveller smiled. Enjoying the story? Read on at {site}. The door opened."
    assert clean_chapter_text("novelping", text) == "The traveller smiled. The door opened."


@pytest.mark.parametrize("text", [
    "Enjoying the story? The traveller turned the page.",
    "Enjoying the story? Read on at another library. The traveller nodded.",
    '"Enjoying the story?" she asked. The sign read n\u043evelping.com.',
])
def test_prompt_and_unicode_story_text_are_not_blanket_removed_or_rewritten(text):
    assert clean_chapter_text("novelping", text) == text


def test_quoted_story_prompt_is_preserved_even_next_to_a_read_on_notice():
    text = '"Enjoying the story?" she asked. Read on at n\u043evelping.com. He nodded.'
    assert clean_chapter_text("novelping", text) == '"Enjoying the story?" she asked. He nodded.'


def test_hidden_html_is_excluded_regardless_of_watermark_wording():
    page = BeautifulSoup('''<article><p>Visible story.<span style="display:none">An unknown advertising phrase.</span> More visible story.</p>
        <div hidden><span style="visibility:hidden">Hidden nested text.</span></div>
        <p style="color:red; DISPLAY : none !important;">Hidden paragraph.</p>
        <p style="visibility: hidden">Hidden visibility text.</p>
        <p style="display:inline">Visible inline text.</p><p aria-hidden="true">Still visually displayed.</p></article>''', "html.parser")
    remove_hidden_elements(page)
    text = page.get_text(" ", strip=True)
    assert text == "Visible story. More visible story. Visible inline text. Still visually displayed."


def test_reported_chapter_already_in_archive_is_cleaned_without_refetch_or_rewrite(tmp_path):
    book_id = "my-reconstruction-talent-is-too-op"
    chapter_id = "chapter-211-test-ii"
    body = "The traveller smiled. Enjoying the story? Read on at n\u043evelping.com. The door opened."
    store = ArchiveStore(tmp_path)
    store.save_book("novelping", {"id": book_id, "title": "My Reconstruction Talent Is Too OP"})
    store.save_chapters("novelping", book_id, [{"id": chapter_id, "title": "Chapter 211", "ordinal": 210}])
    store.save_text("novelping", book_id, chapter_id, body)
    with TestClient(make_app([], "test-token", tmp_path)) as api:
        response = api.get(f"/v1/books/novelping/{book_id}/chapters/{chapter_id}/text", headers={"Authorization": "Bearer test-token"})
        assert response.status_code == 200
        assert response.text == "The traveller smiled. The door opened."
    with store._connect() as db:
        assert db.execute("SELECT body FROM chapters WHERE source=? AND book_id=? AND id=?", ("novelping", book_id, chapter_id)).fetchone()[0] == body
