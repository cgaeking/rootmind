"""Text decoding and the file-type defaults (every default pattern has a parser)."""

from __future__ import annotations

import codecs
from pathlib import Path

import pytest
import yaml

from rootmind.config import DEFAULT_EXCLUDE, DEFAULT_INCLUDE, AppConfig
from rootmind.indexer.parser import DocumentParser

BACKEND = Path(__file__).resolve().parents[1]

# Text with umlauts and the euro sign, as a German Excel or Notepad export
# writes it (Windows-1252). Escapes keep this file ASCII: \xfc is u-umlaut,
# \xf6 o-umlaut, \xdf sharp s.
EURO = "\N{EURO SIGN}"
CP1252_TEXTS = [
    f"Invoice M\xfcller GmbH: 100 {EURO}",  # even length in cp1252
    f"Price: 5 {EURO};Gr\xf6\xdfe",  # even length
    f"Price: 5 {EURO}; Gr\xf6\xdfe M",  # odd length
]
SAMPLE = f"Gr\xf6\xdfe {EURO}"


def test_cp1252_samples_cover_both_byte_lengths():
    """UTF-16 only accepts even lengths, so the regression needs both parities."""
    assert {len(text.encode("cp1252")) % 2 for text in CP1252_TEXTS} == {0, 1}


@pytest.mark.parametrize("text", CP1252_TEXTS)
def test_windows_1252_text_keeps_umlauts_and_euro(text):
    """Regression: even-length cp1252 bytes were decoded as UTF-16 (CJK characters)."""
    raw = text.encode("cp1252")
    assert DocumentParser.parse(raw, "export.csv") == text
    assert DocumentParser.parse(raw, "notes.txt") == text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (SAMPLE.encode(), SAMPLE),
        (codecs.BOM_UTF8 + SAMPLE.encode(), SAMPLE),  # UTF-8 BOM (Excel, Notepad)
        (SAMPLE.encode("utf-16"), SAMPLE),  # with BOM
        (codecs.BOM_UTF16_BE + SAMPLE.encode("utf-16-be"), SAMPLE),  # big-endian BOM
        (b"caf\xe9 \x81", "caf\xe9 \x81"),  # not valid cp1252 either: Latin-1, never fails
    ],
)
def test_text_encodings(raw, expected):
    assert DocumentParser.parse(raw, "a.txt") == expected


def test_every_default_include_pattern_has_a_parser():
    for pattern in DEFAULT_INCLUDE:
        assert pattern.startswith("*."), pattern
        assert DocumentParser.is_supported("x" + pattern[1:]), pattern
    assert AppConfig().indexer.include == list(DEFAULT_INCLUDE)
    assert AppConfig().indexer.exclude == list(DEFAULT_EXCLUDE)


def test_example_config_uses_the_code_defaults():
    example = yaml.safe_load((BACKEND / "config.example.yaml").read_text(encoding="utf-8"))
    assert example["indexer"]["include"] == list(DEFAULT_INCLUDE)
    assert set(DEFAULT_EXCLUDE) <= set(example["indexer"]["exclude"])
