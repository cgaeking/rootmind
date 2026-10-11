"""The committed demo corpus matches its generator and stays obviously fictional."""

from __future__ import annotations

import re

import pytest

from rootmind.indexer.parser import DocumentParser
from scripts import make_demo_corpus

COMMITTED = make_demo_corpus.DEFAULT_OUT
DETERMINISTIC_SUFFIXES = {".md", ".txt", ".csv", ".pdf"}


@pytest.fixture(scope="module")
def regenerated(tmp_path_factory):
    out = tmp_path_factory.mktemp("corpus")
    make_demo_corpus.build_corpus(out)
    return out


def _parsed(path):
    return DocumentParser.parse(path.read_bytes(), path)


def test_same_files(regenerated):
    assert sorted(p.name for p in COMMITTED.iterdir()) == sorted(
        p.name for p in regenerated.iterdir()
    )
    assert len(list(COMMITTED.iterdir())) == 10


def test_committed_corpus_is_current(regenerated):
    """Office files are zips (bytes vary with zlib), so compare the text the indexer sees."""
    for fresh in sorted(regenerated.iterdir()):
        committed = COMMITTED / fresh.name
        assert _parsed(committed) == _parsed(fresh), (
            f"{fresh.name} is stale: run `uv run python scripts/make_demo_corpus.py`"
        )
        if fresh.suffix in DETERMINISTIC_SUFFIXES:
            assert committed.read_bytes() == fresh.read_bytes(), fresh.name


def test_corpus_is_small():
    assert sum(p.stat().st_size for p in COMMITTED.iterdir()) < 150 * 1024


def test_corpus_is_obviously_fictional():
    for path in COMMITTED.iterdir():
        text = _parsed(path)
        assert "Fictional sample data" in text, path.name
        for email in re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text):
            assert email.endswith(".example.com"), (path.name, email)
        for phone in re.findall(r"\+\d[\d-]{6,}", text):
            assert phone.startswith("+1-555-01"), (path.name, phone)
