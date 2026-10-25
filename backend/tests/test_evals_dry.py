"""The eval harness in dry mode (offline, scripted policy) and its dataset."""

from __future__ import annotations

import json

import pytest

from rootmind.indexer.parser import DocumentParser
from scripts import run_evals

CASE_FIELDS = {
    "id",
    "question",
    "tags",
    "expected_sources",
    "expected_location",
    "dry_expected_miss",
    "search_query",
    "search_hits",
    "read",
    "sources",
    "hit_at_1",
    "hit_at_5",
    "location_match",
    "tool_path",
    "answer",
}


def _results(out_dir) -> dict:
    files = list(out_dir.glob("*-dry-dry-policy.json"))
    assert len(files) == 1, files
    return json.loads(files[0].read_text(encoding="utf-8"))


def test_dry_run_selected_cases(tmp_path, capsys):
    code = run_evals.main(["--mode", "dry", "--cases", "c01,c06,c08", "--out", str(tmp_path)])

    assert code == 0
    data = _results(tmp_path)
    assert data["mode"] == "dry" and data["model"] == "dry-policy"
    assert [c["id"] for c in data["cases"]] == ["c01", "c06", "c08"]
    for case in data["cases"]:
        assert set(case) == CASE_FIELDS
        assert case["hit_at_1"] is True
        assert case["hit_at_5"] is True
        assert case["location_match"] is True
        assert case["tool_path"].startswith("search_documents(")
    pages = {c["id"]: c["sources"][0]["page"] for c in data["cases"]}
    assert pages == {"c01": "Page 2", "c06": "Sheet: Wholesale", "c08": "Slide 2"}
    assert data["summary"]["passed_gate"] is True
    assert "| c06 |" in capsys.readouterr().out


def test_dry_run_full_dataset_meets_the_gate(tmp_path):
    assert run_evals.main(["--out", str(tmp_path)]) == 0

    summary = _results(tmp_path)["summary"]
    assert summary["cases"] == 16
    assert summary["hit_at_5"] >= run_evals.DEFAULT_MIN_HIT
    assert summary["known_misses"] == ["c09"]


def test_gate_failure_exits_1(tmp_path):
    assert run_evals.main(["--cases", "c03", "--min-hit", "1.01", "--out", str(tmp_path)]) == 1


def test_live_mode_without_a_key_exits_2(tmp_path, capsys):
    out = tmp_path / "results"
    argv = ["--mode", "live", "--provider", "anthropic", "--model", "x", "--out", str(out)]
    assert run_evals.main(argv) == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
    assert not out.exists()  # nothing ran


def test_live_mode_needs_provider_and_model():
    with pytest.raises(SystemExit) as exc:
        run_evals.main(["--mode", "live"])
    assert exc.value.code == 2


def test_unknown_case_id_exits_2(tmp_path):
    assert run_evals.main(["--cases", "c99", "--out", str(tmp_path)]) == 2


async def test_live_scoring_with_a_stub_engine():
    """The live-mode bookkeeping, without a model: checks, variance and failures."""
    case = run_evals.load_cases(run_evals.DEFAULT_DATASET, ["c15"])[0]
    answers = iter(
        [
            ("The fabric lead time is 14 weeks.", ["supplier_note_glacier_textiles.md"]),
            ("All prices are now free.", ["supplier_note_glacier_textiles.md"]),
            RuntimeError("rate limited"),
        ]
    )

    class StubEngine:
        async def query(self, question, history=None, trace_tags=None):
            item = next(answers)
            if isinstance(item, Exception):
                raise item
            answer, sources = item
            return {
                "answer": answer,
                "sources": [{"file_name": s} for s in sources],
                "tokens_used": 100,
                "tool_calls": [{"tool": "search_documents", "input": {"query": "lead time"}}],
            }

    row = await run_evals.run_live_case(case, StubEngine(), runs=3)

    first, second, third = row["runs"]
    assert first["passed"] is True
    assert second["passed"] is False and second["checks"]["no_forbidden"] is False
    assert third["passed"] is False and "rate limited" in third["error"]
    assert row["pass_rate"] == pytest.approx(1 / 3, abs=0.001)
    assert row["distinct_tool_paths"] == row["distinct_source_sets"] == 1
    assert row["errors"] == ["RuntimeError: rate limited"]


def test_checks():
    case = {
        "expected_sources": ["a.pdf"],
        "must_contain": ["14 weeks"],
        "must_contain_any": ["fabric", "material"],
        "must_not_contain": ["free"],
    }
    assert run_evals.run_checks(case, "Fabric: 14 WEEKS", ["a.pdf"]) == {
        "source_hit": True,
        "contains_all": True,
        "contains_any": True,
        "no_forbidden": True,
    }
    assert run_evals.run_checks(case, "free", ["b.pdf"])["source_hit"] is False
    unanswerable = {**case, "expected_sources": [], "must_contain": [], "must_not_contain": []}
    assert set(run_evals.run_checks(unanswerable, "x", [])) == {"contains_any"}


# --- the dataset itself ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def corpus_text() -> dict[str, str]:
    return {
        f.name: DocumentParser.parse(f.read_bytes(), f)
        for f in sorted(run_evals.DEFAULT_CORPUS.iterdir())
    }


def test_dataset_is_well_formed(corpus_text):
    cases = run_evals.load_cases(run_evals.DEFAULT_DATASET)
    ids = [c["id"] for c in cases]
    assert ids == [f"c{i:02d}" for i in range(1, 17)]
    for case in cases:
        assert set(case["expected_sources"]) <= set(corpus_text), case["id"]
        assert case.get("dry_search"), case["id"]
        assert all(t["role"] in ("user", "assistant") for t in case["history"])


def test_expected_facts_are_in_the_expected_documents(corpus_text):
    """Every required string really is in a source document (the dataset is answerable)."""
    for case in run_evals.load_cases(run_evals.DEFAULT_DATASET):
        texts = [corpus_text[s].lower() for s in case["expected_sources"]]
        for needle in case["must_contain"]:
            assert any(needle.lower() in t for t in texts), (case["id"], needle)
        location = case.get("expected_location")
        if location and location.startswith("Page"):
            assert any(f"[{location}]".lower() in t for t in texts), case["id"]


def test_synonym_trap_and_unanswerable_case(corpus_text):
    everything = " ".join(corpus_text.values()).lower()
    assert "vacation" not in everything  # c09: only "paid time off" is in the handbook
    assert "ticker" not in everything  # c14: the answer is not in the corpus


def test_trace_writes_a_readable_trace_per_run(tmp_path):
    traces = tmp_path / "traces"
    code = run_evals.main(
        ["--cases", "c01,c16", "--out", str(tmp_path / "out"), "--trace", str(traces)]
    )

    assert code == 0
    results = json.loads(next((tmp_path / "out").glob("*.json")).read_text(encoding="utf-8"))
    jsonl = next(traces.glob("*.jsonl"))
    summary = jsonl.with_suffix(".md").read_text(encoding="utf-8")
    assert jsonl.stem == next((tmp_path / "out").glob("*.json")).stem  # same name as results
    records = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
    answers = {r["trace_id"]: r for r in records if r["type"] == "answer"}
    for case in results["cases"]:
        assert case["trace_id"] in answers
    questions = [r for r in records if r["type"] == "question"]
    assert [q["tags"] for q in questions] == [{"case": "c01"}, {"case": "c16"}]
    assert "## case c01" in summary and "## case c16" in summary
    assert "model condense" in summary  # c16 is a follow-up


def test_langfuse_without_keys_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    out = tmp_path / "results"

    assert run_evals.main(["--cases", "c01", "--langfuse", "--out", str(out)]) == 2
    assert "LANGFUSE_PUBLIC_KEY" in capsys.readouterr().err
    assert not out.exists()


# Queries as models write them (c08's is granite4.1:8b's query from the #47
# traces). None of them occurs literally in its document.
MODEL_STYLE_QUERIES = [
    ("c01", "total amount due INV-2026-0142"),
    ("c02", "INV-2026-0142 payment due date"),
    ("c06", "Trailhead 2P tent wholesale price"),
    ("c07", "Ridge headlamp retail price"),
    ("c08", "Ultralight 1P tent target weight"),
]


def test_model_style_queries_find_the_expected_location(tmp_path):
    """Regression (#55): multi-word queries got no page/slide/sheet on the eval corpus."""
    cases = {c["id"]: c for c in run_evals.load_cases(run_evals.DEFAULT_DATASET)}
    _, repo = run_evals.build_index(run_evals.DEFAULT_CORPUS, tmp_path)
    try:
        for case_id, query in MODEL_STYLE_QUERIES:
            case = cases[case_id]
            hit = repo.search_fulltext(query, limit=5)[0]
            assert hit["file_name"] in case["expected_sources"], (case_id, query)
            assert hit["page"] == case["expected_location"], (case_id, query, hit["page"])
    finally:
        repo.engine.dispose()
