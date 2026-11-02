"""packaging/bundle_tesseract.py: pinned downloads, the DLLs tesseract.exe loads, the folder."""

from __future__ import annotations

import hashlib
import importlib.util
import struct
import sys
from pathlib import Path

import pytest

PACKAGING = Path(__file__).resolve().parents[1] / "packaging"


def _load():
    spec = importlib.util.spec_from_file_location(
        "_packaging_bundle_tesseract", PACKAGING / "bundle_tesseract.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up
    spec.loader.exec_module(module)
    return module


bt = _load()


def make_pe(imports: list[str]) -> bytes:
    """A minimal PE32+ file whose import table names ``imports``."""
    section_rva, section_raw = 0x1000, 0x200
    descriptors = b""
    names = b""
    names_at = 20 * (len(imports) + 1)
    for dll in imports:
        descriptors += struct.pack("<IIIII", 0, 0, 0, section_rva + names_at + len(names), 0)
        names += dll.encode("ascii") + b"\0"
    descriptors += b"\0" * 20
    section = descriptors + names
    optional_size = 112 + 16 * 8
    pe = 0x40
    data = bytearray(section_raw + len(section))
    data[0:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, pe)
    data[pe : pe + 4] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", data, pe + 4, 0x8664, 1, 0, 0, 0, optional_size, 0x22)
    optional = pe + 24
    struct.pack_into("<H", data, optional, 0x20B)
    struct.pack_into(
        "<II", data, optional + 112 + 8, section_rva if imports else 0, len(descriptors)
    )
    table = optional + optional_size
    struct.pack_into(
        "<8sIIII", data, table, b".idata", len(section), section_rva, len(section), section_raw
    )
    data[section_raw:] = section
    return bytes(data)


def test_reads_the_import_table():
    assert bt.pe_imports(make_pe(["libtesseract-5.dll", "KERNEL32.dll"])) == [
        "libtesseract-5.dll",
        "KERNEL32.dll",
    ]
    assert bt.pe_imports(make_pe([])) == []


@pytest.mark.parametrize("data", [b"", b"MZ" + b"\0" * 100, b"PK\3\4 not a pe"])
def test_refuses_what_is_not_a_pe_file(data):
    with pytest.raises(bt.BundleError):
        bt.pe_imports(data)


def test_keeps_only_the_dlls_tesseract_loads(tmp_path):
    (tmp_path / "tesseract.exe").write_bytes(make_pe(["libtesseract-5.dll", "KERNEL32.dll"]))
    (tmp_path / "libleptonica-6.dll").write_bytes(make_pe([]))
    # Names are compared without case; Windows DLLs (msvcrt) are not bundled.
    (tmp_path / "libtesseract-5.dll").write_bytes(make_pe(["libleptonica-6.DLL", "msvcrt.dll"]))
    (tmp_path / "libpango-1.0-0.dll").write_bytes(make_pe([]))  # only the training tools use it
    (tmp_path / "text2image.exe").write_bytes(make_pe(["libpango-1.0-0.dll"]))
    assert bt.dll_closure(tmp_path) == ["libleptonica-6.dll", "libtesseract-5.dll", "tesseract.exe"]


def test_a_download_with_another_hash_is_refused(tmp_path):
    source = tmp_path / "model.traineddata"
    source.write_bytes(b"model")
    good = hashlib.sha256(b"model").hexdigest()
    cache = tmp_path / "cache"

    bad = bt.Download(source.as_uri(), "0" * 64, "model.traineddata")
    with pytest.raises(bt.BundleError, match="expected 0000"):
        bt.fetch(bad, cache)
    assert list(cache.iterdir()) == []

    item = bt.Download(source.as_uri(), good, "model.traineddata")
    assert bt.fetch(item, cache).read_bytes() == b"model"
    source.unlink()  # a cached copy with the right hash is not downloaded again
    assert bt.fetch(item, cache).read_bytes() == b"model"


@pytest.mark.skipif(sys.platform == "win32", reason="the fake 7-Zip is a shell script")
def test_bundles_program_models_and_licenses(tmp_path, monkeypatch):
    unpacked = tmp_path / "installer-content"
    (unpacked / "doc").mkdir(parents=True)
    (unpacked / "tesseract.exe").write_bytes(make_pe(["libtesseract-5.dll"]))
    (unpacked / "libtesseract-5.dll").write_bytes(make_pe([]))
    (unpacked / "combine_tessdata.exe").write_bytes(make_pe(["libtesseract-5.dll"]))
    (unpacked / "doc" / "LICENSE").write_text("Apache License\n", encoding="utf-8")
    sevenzip = tmp_path / "7z"
    # 7z x -y -o<dir> <archive>: "unpacks" the prepared folder.
    sevenzip.write_text(f'#!/bin/sh\nmkdir -p "${{3#-o}}" && cp -R "{unpacked}/." "${{3#-o}}"\n')
    sevenzip.chmod(0o755)

    cache = tmp_path / "cache"
    cache.mkdir()
    downloads = []
    for name, content in [
        ("setup.exe", b"installer"),
        ("deu.traineddata", b"de"),
        ("eng.traineddata", b"en"),
    ]:
        (cache / name).write_bytes(content)
        downloads.append(
            bt.Download(
                "https://example.invalid/" + name, hashlib.sha256(content).hexdigest(), name
            )
        )
    monkeypatch.setattr(bt, "INSTALLER", downloads[0])
    monkeypatch.setattr(bt, "MODELS", tuple(downloads[1:]))

    dest = tmp_path / "dist" / "tesseract"
    dest.mkdir(parents=True)
    (dest / "stale.dll").write_bytes(b"old")
    assert bt.bundle(dest, cache, str(sevenzip)) == ["libtesseract-5.dll", "tesseract.exe"]
    assert sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()) == [
        "LICENSE",
        "THIRD-PARTY.txt",
        "libtesseract-5.dll",
        "tessdata/LICENSE",
        "tessdata/deu.traineddata",
        "tessdata/eng.traineddata",
        "tesseract.exe",
    ]
    note = (dest / "THIRD-PARTY.txt").read_text(encoding="utf-8")
    assert "Apache License 2.0" in note and downloads[1].sha256 in note
