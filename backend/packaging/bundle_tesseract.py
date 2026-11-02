"""Put Tesseract (OCR) next to the Windows backend build, as ``tesseract/``.

``rootmind.ocr`` looks for ``<folder of rootmind-backend.exe>/tesseract/
tesseract.exe`` with its language data in ``tesseract/tessdata``. This script
fills that folder from pinned downloads, each checked against its SHA-256:

- the Windows build of Tesseract from the Tesseract project's release (the
  UB Mannheim installer, an NSIS archive that 7-Zip unpacks). Only
  ``tesseract.exe`` and the DLLs it loads are kept; the training tools,
  the viewer and the documentation pages are left out.
- the "best" (most accurate) models for German and English from
  ``tesseract-ocr/tessdata_best``.

The licenses go along (``LICENSE``, ``THIRD-PARTY.txt``). Downloads are kept
in ``--cache`` and reused when their hash matches. Standard library only::

    python packaging/bundle_tesseract.py --dest dist/rootmind-backend/tesseract
    python packaging/bundle_tesseract.py --dest DIR --cache DIR --sevenzip "C:/Program Files/7-Zip/7z.exe"

To update: change the URLs and hashes below, run it, and check that
``rootmind-backend.exe ocr-check`` still passes (the smoke test runs it).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Download:
    url: str
    sha256: str
    name: str


TESSERACT_VERSION = "5.5.3"
INSTALLER = Download(
    url=(
        "https://github.com/tesseract-ocr/tesseract/releases/download/5.5.3/"
        "tesseract-ocr-w64-setup-5.5.3.20260724.exe"
    ),
    # pragma: allowlist nextline secret (a file hash)
    sha256="bee9e3434bd94fd65387d9be28cd467a41f61b1275383b55b0f59a1331270ae4",
    name="tesseract-ocr-w64-setup-5.5.3.20260724.exe",
)
TESSDATA_TAG = "4.1.0"
MODELS = (
    Download(
        url=f"https://raw.githubusercontent.com/tesseract-ocr/tessdata_best/{TESSDATA_TAG}/deu.traineddata",
        # pragma: allowlist nextline secret (a file hash)
        sha256="8407331d6aa0229dc927685c01a7938fc5a641d1a9524f74838cdac599f0d06e",
        name="deu.traineddata",
    ),
    Download(
        url=f"https://raw.githubusercontent.com/tesseract-ocr/tessdata_best/{TESSDATA_TAG}/eng.traineddata",
        # pragma: allowlist nextline secret (a file hash)
        sha256="8280aed0782fe27257a68ea10fe7ef324ca0f8d85bd2fd145d1c2b560bcb66ba",
        name="eng.traineddata",
    ),
)

PROGRAM = "tesseract.exe"
DOWNLOAD_TIMEOUT_SECONDS = 300


class BundleError(Exception):
    """A download, the unpacking or a check failed; the message says which."""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(item: Download, cache: Path) -> Path:
    """The file of ``item`` in ``cache``, downloaded unless a copy with its hash is there."""
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / item.name
    if path.is_file() and sha256_of(path) == item.sha256:
        return path
    partial = path.with_name(path.name + ".part")
    print(f"Downloading {item.url}")
    with urllib.request.urlopen(item.url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:  # noqa: S310 (fixed https URLs)
        with open(partial, "wb") as out:
            shutil.copyfileobj(response, out)
    actual = sha256_of(partial)
    if actual != item.sha256:
        partial.unlink()
        raise BundleError(f"{item.name}: SHA-256 {actual}, expected {item.sha256}")
    partial.replace(path)
    return path


# --- which DLLs tesseract.exe loads ----------------------------------------------------


def pe_imports(data: bytes) -> list[str]:
    """The DLL names in the import table of a PE file (an .exe or .dll).

    Raises:
        BundleError: not a PE file this parser understands.
    """
    try:
        if data[:2] != b"MZ":
            raise BundleError("not a PE file (no MZ header)")
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe : pe + 4] != b"PE\0\0":
            raise BundleError("not a PE file (no PE signature)")
        sections, optional_size = struct.unpack_from("<H12xH", data, pe + 6)
        optional = pe + 24
        magic = struct.unpack_from("<H", data, optional)[0]
        directories = optional + {0x10B: 96, 0x20B: 112}[magic]
        import_rva = struct.unpack_from("<I", data, directories + 8)[0]
        table = optional + optional_size
        spans = [struct.unpack_from("<8xIIII", data, table + 40 * i) for i in range(sections)]
    except (struct.error, KeyError) as e:
        raise BundleError(f"unreadable PE header: {e}") from None

    def offset(rva: int) -> int:
        for virtual_size, address, raw_size, raw in spans:
            if address <= rva < address + max(virtual_size, raw_size):
                return raw + rva - address
        raise BundleError(f"RVA {rva:#x} is in no section")

    names: list[str] = []
    if not import_rva:
        return names
    entry = offset(import_rva)
    while True:
        descriptor = struct.unpack_from("<IIIII", data, entry)
        if not any(descriptor):
            return names
        start = offset(descriptor[3])
        names.append(data[start : data.index(b"\0", start)].decode("ascii"))
        entry += 20


def dll_closure(folder: Path, program: str = PROGRAM) -> list[str]:
    """``program`` and every DLL in ``folder`` that it loads, directly or through others.

    DLLs that are not in ``folder`` come from Windows and are not bundled.
    """
    present = {p.name.lower(): p.name for p in folder.iterdir() if p.is_file()}
    if program.lower() not in present:
        raise BundleError(f"{program} is not in {folder}")
    needed: set[str] = set()
    todo = [present[program.lower()]]
    while todo:
        name = todo.pop()
        if name in needed:
            continue
        needed.add(name)
        for dll in pe_imports((folder / name).read_bytes()):
            if dll.lower() in present:
                todo.append(present[dll.lower()])
    return sorted(needed, key=str.lower)


# --- assembling the folder ---------------------------------------------------------------


def unpack(installer: Path, into: Path, sevenzip: str) -> Path:
    result = subprocess.run(  # noqa: S603 (7-Zip with fixed arguments)
        [sevenzip, "x", "-y", f"-o{into}", str(installer)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise BundleError(
            f"7-Zip could not unpack {installer.name}: {result.stderr or result.stdout}"
        )
    if not (into / PROGRAM).is_file():
        raise BundleError(f"{installer.name} holds no {PROGRAM}")
    return into


def third_party_note(files: list[str]) -> str:
    lines = [
        "Text recognition (OCR) in RootMind uses Tesseract, bundled in this folder.",
        "",
        f"Tesseract {TESSERACT_VERSION} (Apache License 2.0, see LICENSE),",
        "Windows build by the Universitätsbibliothek Mannheim, from the Tesseract release:",
        f"  {INSTALLER.url}",
        f"  SHA-256 {INSTALLER.sha256}",
        "",
        "The DLLs are the MSYS2 MinGW-w64 builds of the libraries Tesseract uses",
        "(Leptonica, libarchive, curl, libpng, libjpeg-turbo, libtiff, libwebp, OpenJPEG,",
        "giflib, zlib, zstd, xz, bzip2, lz4, brotli, libdeflate, LERC, libb2, expat, libiconv,",
        "gettext, libidn2, libpsl, libunistring, libssh2, GCC runtime, winpthreads), each",
        "under its own open source license, as shipped in the installer above.",
        "",
        f"Language models from tessdata_best {TESSDATA_TAG} (Apache License 2.0, tessdata/LICENSE):",
    ]
    lines += [f"  {m.url}\n  SHA-256 {m.sha256}" for m in MODELS]
    lines += ["", "Files:"] + [f"  {name}" for name in files]
    return "\n".join(lines) + "\n"


def bundle(dest: Path, cache: Path, sevenzip: str) -> list[str]:
    """Fill ``dest`` (replaced if it exists); the bundled program files."""
    installer = fetch(INSTALLER, cache)
    models = [fetch(m, cache) for m in MODELS]
    if dest.exists():
        shutil.rmtree(dest)
    with tempfile.TemporaryDirectory(prefix="tesseract-") as tmp:
        unpacked = unpack(installer, Path(tmp) / "unpacked", sevenzip)
        files = dll_closure(unpacked)
        dest.mkdir(parents=True)
        for name in files:
            shutil.copy2(unpacked / name, dest / name)
        license_file = unpacked / "doc" / "LICENSE"
        if not license_file.is_file():
            raise BundleError("the installer holds no doc/LICENSE")
        shutil.copy2(license_file, dest / "LICENSE")
    tessdata = dest / "tessdata"
    tessdata.mkdir()
    for model in models:
        shutil.copy2(model, tessdata / model.name)
    shutil.copy2(dest / "LICENSE", tessdata / "LICENSE")  # tessdata_best: Apache 2.0 too
    (dest / "THIRD-PARTY.txt").write_text(third_party_note(files), encoding="utf-8", newline="\n")
    return files


def check(dest: Path) -> None:
    """Run the bundled program: it starts and lists both languages (Windows only)."""
    if sys.platform != "win32":
        print("Not on Windows: the bundled tesseract.exe was not run.")
        return
    env = dict(os.environ, TESSDATA_PREFIX=str(dest / "tessdata"))
    result = subprocess.run(  # noqa: S603 (the program just bundled)
        [str(dest / PROGRAM), "--list-langs"], capture_output=True, text=True, env=env, check=False
    )
    languages = set(result.stdout.split())
    if result.returncode != 0 or not {"deu", "eng"} <= languages:
        raise BundleError(
            f"{PROGRAM} --list-langs: {result.returncode} {result.stdout} {result.stderr}"
        )
    print(f"{PROGRAM} runs; languages: deu, eng")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dest", type=Path, required=True, help="the tesseract folder to create")
    parser.add_argument(
        "--cache", type=Path, default=Path(tempfile.gettempdir()) / "rootmind-tesseract-cache"
    )
    parser.add_argument("--sevenzip", default=shutil.which("7z") or "7z", help="7-Zip program")
    args = parser.parse_args(argv)
    try:
        files = bundle(args.dest, args.cache, args.sevenzip)
        check(args.dest)
    except (BundleError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    size = sum(p.stat().st_size for p in args.dest.rglob("*") if p.is_file())
    print(
        f"Tesseract {TESSERACT_VERSION}: {len(files)} program files, {size / 1e6:.1f} MB in {args.dest}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
