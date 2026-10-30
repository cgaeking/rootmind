"""DocumentRepository: FTS5 strategies, fuzzy search, locations, deletes and pruning."""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import event, text

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


def test_snippets_are_computed_only_for_returned_results(repo):
    """Regression: snippet() ran for every row ranked above the deepest result.

    With a file type filter that included documents the filter drops, and on
    an index with huge spreadsheet exports one search took over nine minutes.
    """
    for i in range(30):
        _add(repo, f"export{i}.csv", "Sehr geehrte Damen und Herren, Schulz " * 50, file_type="csv")
    _add(
        repo,
        "letter.pdf",
        "[Page 1]\nSehr geehrte Frau Schulz, anbei die Unterlagen.",
        "docs",
        "pdf",
    )
    rowids_per_snippet_query: list[int] = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if "snippet(" in statement:
            assert "LIMIT" not in statement
            # open, close, ellipsis and the MATCH query; the rest are rowids.
            rowids_per_snippet_query.append(len(parameters) - 4)

    event.listen(repo.engine, "before_cursor_execute", capture)
    try:
        results = repo.search_fulltext("Sehr geehrte Schulz", limit=10, file_type="pdf")
    finally:
        event.remove(repo.engine, "before_cursor_execute", capture)

    assert _names(results) == ["letter.pdf"]
    assert (
        results[0]["snippet"]
        == "[Page 1]\n**Sehr** **geehrte** Frau **Schulz**, anbei die Unterlagen."
    )
    assert results[0]["page"] == "Page 1"
    assert sum(rowids_per_snippet_query) == len(results)


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


# Small stand-ins for the eval corpus: the first match of a query term is on an
# earlier page/slide/sheet than the place where most of the terms meet.
_INVOICE = (
    "[Page 1]\nINVOICE INV-7\nInvoice date: 2026-03-31\nItem | Qty | Amount\n"
    "Trailhead 2P tent | 3 | 747.00\nContinued on page 2.\n\n"
    "[Page 2]\nINVOICE INV-7 (page 2 of 2)\nSubtotal: 1,092.00\nTotal due: 1,179.36\n"
    "Payment terms: Net 30\nDue date: 2026-04-30\n"
)
_ROADMAP = (
    "=== Slide 1 ===\n\nTent roadmap, second half of 2026\n\n"
    "=== Slide 2 ===\n\nUltralight 1P tent\n\nLaunch: October 2026\nTarget weight: 1.1 kg\n\n"
    "=== Slide 3 ===\n\nSupplier lead times\n"
)
_PRICES = (
    "=== Sheet: Retail ===\nSKU | Product | Retail price (USD)\n"
    "FH-TENT-2P | Trailhead 2P tent | 249\nFH-LAMP-R | Ridge headlamp | 34.5\n"
    "=== Sheet: Wholesale ===\nSKU | Product | Wholesale price (USD) | Minimum quantity\n"
    "FH-TENT-2P | Trailhead 2P tent | 174.3 | 20\nFH-LAMP-R | Ridge headlamp | 24.15 | 50\n"
)


@pytest.mark.parametrize(
    ("name", "file_type", "content", "query", "page", "at"),
    [
        ("invoice.pdf", "pdf", _INVOICE, "INV-7 total due", "Page 2", "INV-7 (page 2"),
        ("invoice.pdf", "pdf", _INVOICE, "total amount due", "Page 2", "Total due"),
        ("invoice.pdf", "pdf", _INVOICE, "INV-7 payment due date", "Page 2", "INV-7 (page 2"),
        ("invoice.pdf", "pdf", _INVOICE, "INV-7", "Page 1", "INV-7\nInvoice date"),
        ("roadmap.pptx", "pptx", _ROADMAP, "Ultralight 1P tent target weight", "Slide 2", "Ultra"),
        ("roadmap.pptx", "pptx", _ROADMAP, "tent launch", "Slide 2", "tent\n\nLaunch"),
        (
            "prices.xlsx",
            "xlsx",
            _PRICES,
            "Trailhead 2P wholesale price",
            "Sheet: Wholesale",
            "Whol",
        ),
        (
            "prices.xlsx",
            "xlsx",
            _PRICES,
            "Ridge headlamp retail price",
            "Sheet: Retail",
            "Retail =",
        ),
    ],
    ids=lambda value: value if isinstance(value, str) and len(value) < 40 else "",
)
def test_multi_word_queries_locate_where_the_terms_meet(
    repo, name, file_type, content, query, page, at
):
    """Regression (#55): a hit only got a location when the whole query occurred literally.

    Models search with several words ("Ultralight 1P tent target weight"); the
    location now comes from the FTS5 match, on the page/slide/sheet where most
    of the terms occur together.
    """
    doc = _add(repo, name, content, file_type=file_type)

    hit = repo.search_fulltext(query)[0]

    assert hit["doc_id"] == doc.id
    assert hit["page"] == page
    assert hit["position"] == content.index(at)
    # The position is a highlighted match inside the snippet that is shown.
    assert content[hit["position"] : hit["position"] + 5] in hit["snippet"].replace("**", "")


def test_location_of_a_hit_deep_in_a_long_document(repo):
    """The snippet is a window of a long text; its position maps back to the right page."""
    pages = [f"[Page {n}]\n" + "filler words about nothing in particular. " * 60 for n in (1, 2)]
    pages.append("[Page 3]\nThe pump pressure limit is 6 bar.\n")
    pages.append("[Page 4]\n" + "more filler text without the terms. " * 60)
    content = "\n".join(pages)
    _add(repo, "manual.pdf", content, file_type="pdf")

    hit = repo.search_fulltext("pump pressure limit bar")[0]

    assert hit["page"] == "Page 3"
    assert hit["position"] == content.index("pump pressure")
    assert hit["snippet"].startswith("...")


def test_terms_on_different_pages_do_not_pool(repo):
    """Matches pool only within one page: two terms on page 2 beat one on page 1."""
    content = "[Page 1]\nalpha\n[Page 2]\nbeta gamma\n"
    _add(repo, "split.pdf", content, file_type="pdf")

    hit = repo.search_fulltext("alpha beta gamma")[0]

    assert hit["page"] == "Page 2"
    assert hit["position"] == content.index("beta")


def test_position_falls_back_to_the_phrase_when_the_snippet_is_not_found(repo):
    """Text that contains the internal snippet markers still gets a position."""
    content = "[Page 1]\nintro\n[Page 2]\nodd \x02 control \x03 chars, then the lantern hook"
    _add(repo, "odd.pdf", content, file_type="pdf")

    hit = repo.search_fulltext("lantern hook")[0]

    assert hit["position"] == content.index("lantern hook")
    assert hit["page"] == "Page 2"


def test_match_in_the_file_name_only_has_no_position(filled):
    hit = next(
        r for r in filled.search_fulltext("manual") if r["file_name"] == "lantern_manual.txt"
    )
    assert hit["position"] is None
    assert hit["page"] is None
    assert "\x02" not in hit["snippet"] and "\x1f" not in hit["snippet"]


def test_best_cluster_prefers_most_distinct_terms_then_the_earliest():
    best = repository_module._best_cluster
    far = " " * 600  # beyond the cluster reach
    # Two places with two distinct terms each: the earlier one wins.
    plain = "aa bb" + far + "aa cc"
    assert best(plain, [(0, 2), (3, 5), (605, 607), (608, 610)], ["aa", "bb", "cc"]) == 0
    # Three distinct terms beat two, even later in the text.
    plain = "aa bb" + far + "aa bb cc"
    spans = [(0, 2), (3, 5), (605, 607), (608, 610), (611, 613)]
    assert best(plain, spans, ["aa", "bb", "cc"]) == 605
    # A phrase highlight counts for every term it contains.
    assert best("x tent 1P", [(2, 9)], ["tent", "1P"]) == 2
    assert best("text", [], ["aa"]) is None


def test_parse_snippet_strips_markers_and_ellipses():
    raw = "\x1fsome \x02hit\x03 and \x02another\x03\x1f"
    plain, spans = repository_module._parse_snippet(raw)
    assert plain == "some hit and another"
    assert [plain[s:e] for s, e in spans] == ["hit", "another"]


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
