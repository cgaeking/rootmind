"""DocumentRepository: FTS5 strategies, fuzzy search, locations, deletes and pruning."""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import text

from rootmind.database import repository as repository_module


def _count(repo, table: str, doc_ids: list[str]) -> int:
    with repo.engine.connect() as conn:
        return sum(
            conn.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE doc_id = :d"),  # noqa: S608 (fixed names)
                {"d": d},
            ).scalar()
            for d in doc_ids
        )


def _add(repo, name: str, content: str, connector: str = "docs", file_type: str = "txt"):
    return repo.upsert_document(connector, name, name, file_type, content_text=content)


@pytest.fixture
def filled(repo):
    _add(repo, "phrase.txt", "The camping lantern was left at the trailhead.")
    _add(repo, "apart.txt", "A lantern hung in the barn. Later we went camping by the lake.")
    _add(repo, "one_term.txt", "Lantern oil, lantern wicks and a lantern hook.")
    _add(repo, "invoices.txt", "All invoices for March are attached.")
    _add(repo, "lantern_manual.txt", "Assembly instructions for the stand.")
    _add(repo, "tent.md", "SKU FH-TENT-2P weighs 2.1 kg.", file_type="md")
    _add(repo, "other.txt", "Another lantern in the camping shed.", connector="other")
    return repo


def _names(results) -> list[str]:
    return [r["file_name"] for r in results]


# --- full-text search -------------------------------------------------------------------


def test_exact_phrase_ranks_first(filled):
    results = filled.search_fulltext("camping lantern")
    assert _names(results)[0] == "phrase.txt"
    assert {"apart.txt", "one_term.txt"} <= set(_names(results))


def test_prefix_match(filled):
    assert "invoices.txt" in _names(filled.search_fulltext("invoice"))


def test_filename_strategy_finds_names(filled):
    assert "lantern_manual.txt" in _names(filled.search_fulltext("manual"))


def test_hyphenated_terms(filled):
    assert _names(filled.search_fulltext("FH-TENT-2P")) == ["tent.md"]


def test_snippets_highlight_and_can_be_skipped(filled):
    with_snippets = filled.search_fulltext("trailhead")
    assert with_snippets[0]["snippet"] == "The camping lantern was left at the **trailhead**."
    assert with_snippets[0]["position"] == "The camping lantern was left at the trailhead.".index(
        "trailhead"
    )

    fast = filled.search_fulltext("trailhead", with_snippets=False)
    assert fast[0]["snippet"] == ""
    assert fast[0]["position"] is None


def test_snippet_comes_from_the_most_precise_strategy(filled):
    by_name = {r["file_name"]: r["snippet"] for r in filled.search_fulltext("camping lantern")}
    assert "**camping lantern**" in by_name["phrase.txt"]  # phrase match
    assert "**lantern**" in by_name["apart.txt"] and "**camping**" in by_name["apart.txt"]


def test_snippets_do_not_change_the_ranking(filled):
    for query in ("camping lantern", "lantern", "invoice"):
        fast = [r["doc_id"] for r in filled.search_fulltext(query, with_snippets=False)]
        rich = [r["doc_id"] for r in filled.search_fulltext(query, with_snippets=True)]
        assert fast == rich


def test_filters(filled):
    assert _names(filled.search_fulltext("SKU", file_type="md")) == ["tent.md"]
    assert filled.search_fulltext("SKU", file_type="pdf") == []
    other_only = filled.search_fulltext("lantern", connector_names={"other"})
    assert _names(other_only) == ["other.txt"]
    assert "other.txt" not in _names(filled.search_fulltext("lantern", connector_name="docs"))
    assert filled.search_fulltext("lantern", connector_names=set()) == []


@pytest.mark.parametrize(
    "query", ["foo-bar", "AND", "OR OR", '"unbalanced', "*", "NEAR(", "lantern)", "a", "", "^:"]
)
def test_fts_special_characters_never_raise(filled, query, caplog):
    caplog.set_level(logging.WARNING)
    results = filled.search_fulltext(query)
    assert isinstance(results, list)
    assert caplog.records == []  # strategy failures are logged at debug level only


def test_fts_operators_are_matched_literally(repo):
    _add(repo, "ops.txt", "Terms AND conditions apply. NEAR the river.")
    assert _names(repo.search_fulltext("AND")) == ["ops.txt"]
    assert _names(repo.search_fulltext("NEAR")) == ["ops.txt"]


def test_updated_document_is_reindexed_once(repo):
    doc = _add(repo, "a.txt", "old words about kayaks")
    _add(repo, "a.txt", "new words about canoes")

    assert repo.search_fulltext("kayaks") == []
    assert _names(repo.search_fulltext("canoes")) == ["a.txt"]
    assert _count(repo, "documents_fts", [doc.id]) == 1


def test_non_indexed_documents_are_not_returned(repo):
    repo.upsert_document("docs", "bad.pdf", "bad.pdf", "pdf", status="error", error_message="x")
    assert repo.search_fulltext("bad") == []
    assert repo.list_documents() == []


# --- fuzzy search ---------------------------------------------------------------------------


def test_fuzzy_search_finds_a_typo(filled):
    results = filled.search_fuzzy("lantren")
    assert "phrase.txt" in _names(results)
    assert all(r["score"] >= 0.3 for r in results)
    assert results[0]["similarity"].endswith("%")


def test_fuzzy_search_respects_threshold_and_filters(filled):
    assert filled.search_fuzzy("zzzqqq") == []
    assert filled.search_fuzzy("lantren", min_similarity=1.0) == []
    other = filled.search_fuzzy("lantren", connector_names={"other"})
    assert _names(other) == ["other.txt"]
    assert filled.search_fuzzy("lantren", file_type="md") == []
    assert filled.search_fuzzy("lantren", connector_names=set()) == []


def test_fuzzy_filters_apply_before_the_limit(repo):
    """Regression: filters ran after LIMIT, so a scoped search could miss its match."""
    for i in range(30):
        _add(repo, f"best{i}.txt", "lantern lantern", connector="other")
    _add(repo, "mine.txt", "a lantern", connector="docs")

    results = repo.search_fuzzy("lantern", limit=1, connector_names={"docs"})

    assert _names(results) == ["mine.txt"]


def test_fuzzy_words_are_chosen_deterministically(repo):
    """Regression: a set's iteration order picked a random 1,000 words per run."""
    words = [f"word{i:04d}x" for i in range(1500)]
    doc = _add(repo, "many.txt", " ".join(["firstword", *words, "lastword"]))

    assert "many.txt" in _names(repo.search_fuzzy("firstword", min_similarity=1.0))
    assert repo.search_fuzzy("lastword", min_similarity=1.0) == []
    assert doc.id


def test_rebuild_trigram_index(filled, monkeypatch):
    monkeypatch.setattr(repository_module, "_TRIGRAM_REBUILD_BATCH", 2)  # exercise batching
    with filled.engine.connect() as conn:
        conn.execute(text("DELETE FROM document_trigrams"))
        conn.commit()
    assert filled.search_fuzzy("lantren") == []
    progress = []

    count = filled.rebuild_trigram_index(lambda done, total: progress.append((done, total)))

    assert count == 7
    assert progress[-1] == (7, 7)
    assert "phrase.txt" in _names(filled.search_fuzzy("lantren"))


# --- locations --------------------------------------------------------------------------


@pytest.fixture
def located(repo):
    pdf = _add(repo, "a.pdf", "[Page 1]\nfirst page\n\n[Page 2]\nsecond page", file_type="pdf")
    pptx = _add(repo, "b.pptx", "=== Slide 1 ===\n\nIntro\n\n=== Slide 2 ===\n\nPlan", "docs")
    xlsx = _add(
        repo, "c.xlsx", "=== Sheet: Retail ===\nA | 1\n=== Sheet: Wholesale ===\nB | 2", "docs"
    )
    txt = _add(repo, "d.txt", "no markers here")
    return {"pdf": pdf, "pptx": pptx, "xlsx": xlsx, "txt": txt}


def _position(repo, doc, needle: str) -> int:
    return repo.get_document_content(doc.id)["content"].index(needle)


def test_resolve_page_labels(repo, located):
    pdf, pptx, xlsx = located["pdf"], located["pptx"], located["xlsx"]
    assert repo.resolve_page(pdf.id, _position(repo, pdf, "second")) == "Page 2"
    assert repo.resolve_page(pdf.id, _position(repo, pdf, "first")) == "Page 1"
    assert repo.resolve_page(pptx.id, _position(repo, pptx, "Plan")) == "Slide 2"
    assert repo.resolve_page(xlsx.id, _position(repo, xlsx, "B | 2")) == "Sheet: Wholesale"
    assert repo.resolve_page(located["txt"].id, 5) is None
    assert repo.resolve_page(pdf.id, None) is None


def test_resolve_page_at_a_marker_boundary(repo, located):
    """Regression: a position exactly at "[Page 2]" was reported as Page 1."""
    pdf, xlsx = located["pdf"], located["xlsx"]
    assert repo.resolve_page(pdf.id, _position(repo, pdf, "[Page 2]")) == "Page 2"
    assert repo.resolve_page(pdf.id, 0) == "Page 1"
    assert repo.resolve_page(xlsx.id, _position(repo, xlsx, "=== Sheet: Wholesale")) == (
        "Sheet: Wholesale"
    )
    # One character before the marker still belongs to the previous page.
    assert repo.resolve_page(pdf.id, _position(repo, pdf, "[Page 2]") - 1) == "Page 1"


def test_search_hits_carry_their_location(repo, located):
    hit = repo.search_fulltext("second")[0]
    assert hit["page"] == "Page 2"
    # Regression: the FTS match ignores case, the position lookup used not to.
    assert repo.search_fulltext("SECOND PAGE")[0]["page"] == "Page 2"
    assert repo.search_fulltext("wholesale")[0]["page"] == "Sheet: Wholesale"
    fuzzy = repo.search_fuzzy("wholesal")[0]
    assert fuzzy["page"] == "Sheet: Wholesale"


# --- content, listing, statistics ----------------------------------------------------------


def test_get_document_content_ranges(repo):
    doc = _add(repo, "a.txt", "0123456789")
    assert repo.get_document_content(doc.id)["content"] == "0123456789"
    window = repo.get_document_content(doc.id, 2, 5)
    assert window["content"] == "234"
    assert window["content_length"] == 10
    assert repo.get_document_content(doc.id, 8)["content"] == "89"
    assert repo.get_document_content(doc.id, 20)["content"] == ""
    assert repo.get_document_content(doc.id, 5, 3)["content"] == ""
    assert repo.get_document_content(doc.id, None, 2)["content"] == "01"
    assert repo.get_document_content("missing") is None
    # substr() counts characters, not bytes: o-umlaut, sharp s and the euro sign.
    umlauts = _add(repo, "u.txt", "Gr\xf6\xdfe: 5 \N{EURO SIGN}, Ma\xdfe")
    assert repo.get_document_content(umlauts.id, 7, 10)["content"] == "5 \N{EURO SIGN}"


def test_statistics(filled):
    stats = filled.get_statistics()
    assert stats["total_documents"] == stats["indexed_documents"] == 7
    assert stats["file_types"] == {"txt": 6, "md": 1}


# --- deletes and pruning --------------------------------------------------------------------


def test_delete_documents_removes_rows_fts_and_trigrams(filled, monkeypatch):
    monkeypatch.setattr(repository_module, "_DELETE_BATCH_SIZE", 2)  # exercise batching
    ids = [d["doc_id"] for d in filled.list_documents(connector_name="docs")]
    assert _count(filled, "document_trigrams", ids) > 0

    removed = filled.delete_documents([*ids, "not-a-doc"])

    assert removed == len(ids) == 6
    assert _count(filled, "documents_fts", ids) == 0
    assert _count(filled, "document_trigrams", ids) == 0
    assert _names(filled.list_documents()) == ["other.txt"]
    assert filled.delete_document("not-a-doc") is False


def test_delete_by_connector_also_deletes_trigrams(filled):
    other_id = filled.list_documents(connector_name="other")[0]["doc_id"]

    assert filled.delete_by_connector("other") == 1

    assert _count(filled, "document_trigrams", [other_id]) == 0
    assert "other.txt" not in _names(filled.search_fuzzy("lantren"))


def test_prune_connectors(filled):
    filled.update_sync_status("other", documents_total=1)
    filled.update_sync_status("ghost", documents_total=0)  # status row without documents

    removed = filled.prune_connectors({"docs"})

    assert removed == 1
    assert {s.connector_name for s in filled.get_all_sync_statuses()} == set()
    assert "other.txt" not in _names(filled.search_fulltext("lantern"))
    assert filled.prune_connectors({"docs"}) == 0
    assert filled.prune_connectors(set()) == 6
