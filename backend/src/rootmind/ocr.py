"""Text recognition (OCR) for scanned PDF pages and images, with Tesseract.

OCR is off by default and turned on per folder (``ocr: true`` on a connector).
Even then it runs only where it is needed:

- PDF pages are checked from the PDF structure, without rendering
  (:func:`scanned_pages`). A page with a text layer is never OCR'd, even when
  it holds images; a page with (almost) no text and an image covering most of
  it is a scan. Only those pages are rendered and recognized.
- Images (:data:`IMAGE_TYPES`) above a minimum size.

The indexer stores a document with its text layer first and plans the OCR
work; :mod:`rootmind.indexer.ocr_worker` does it in the background and merges
the recognized pages into the document. Results are cached by file content
hash, so moving, renaming or reindexing a file never repeats OCR.

Tesseract runs as a separate process (no Python binding needed): the bundled
copy next to the frozen backend, ``RLM_TESSERACT``, or ``tesseract`` on PATH.
"""

from __future__ import annotations

import io
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: Image files that are OCR'd in folders with OCR on (HEIC needs a decoder
#: Pillow does not ship, so it is not included).
IMAGE_TYPES = frozenset({".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"})
#: Smaller images (icons, thumbnails) are not OCR'd: width or height below this.
MIN_IMAGE_SIDE = 300

#: A PDF page with fewer non-space characters in its text layer counts as having none.
MIN_PAGE_TEXT_CHARS = 25
#: ... and is a scan when images cover at least this share of the page.
MIN_IMAGE_COVERAGE = 0.3

#: Pages are rendered at this resolution for recognition.
RENDER_DPI = 300
#: Recognition languages, used as far as they are installed.
LANGUAGES = ("deu", "eng")
#: One page or image may take this long.
TESSERACT_TIMEOUT_SECONDS = 300

_PAGE_MARKER = re.compile(r"^\[Page (\d+)\]$", re.MULTILINE)


def is_image(file_path: str) -> bool:
    return Path(file_path).suffix.lower() in IMAGE_TYPES


def ocr_candidate(file_path: str) -> bool:
    """Whether a file in a folder with OCR on can need OCR (a PDF or an image)."""
    return Path(file_path).suffix.lower() == ".pdf" or is_image(file_path)


# --- which pages need OCR ----------------------------------------------------------


def scanned_pages(pdf: bytes) -> list[int]:
    """The 1-based numbers of the pages that are scans: (almost) no text, mostly image.

    Reads only the PDF structure (text layer and image placements); nothing is
    rendered. An unreadable PDF has no scanned pages.
    """
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c

    try:
        document = pdfium.PdfDocument(pdf)
    except Exception as e:
        logger.warning("OCR: cannot open the PDF to look for scanned pages: %s", e)
        return []
    pages: list[int] = []
    try:
        for index in range(len(document)):
            page = document[index]
            try:
                textpage = page.get_textpage()
                try:
                    chars = len(re.sub(r"\s", "", textpage.get_text_range()))
                finally:
                    textpage.close()
                if chars >= MIN_PAGE_TEXT_CHARS:
                    continue
                width, height = page.get_size()
                area = width * height
                if area <= 0:
                    continue
                covered = 0.0
                for obj in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE], max_depth=3):
                    left, bottom, right, top = obj.get_bounds()
                    w = max(0.0, min(right, width) - max(left, 0.0))
                    h = max(0.0, min(top, height) - max(bottom, 0.0))
                    covered += w * h
                if covered / area >= MIN_IMAGE_COVERAGE:
                    pages.append(index + 1)
            finally:
                page.close()
    finally:
        document.close()
    return pages


def large_enough(image: bytes) -> bool:
    """Whether an image is big enough to hold readable text (not an icon or thumbnail)."""
    from PIL import Image

    try:
        with Image.open(io.BytesIO(image)) as img:
            width, height = img.size
    except Exception:
        return False
    return min(width, height) >= MIN_IMAGE_SIDE


def render_page(pdf: bytes, page_number: int) -> bytes:
    """Page ``page_number`` (1-based) as a grayscale PNG at :data:`RENDER_DPI`."""
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(pdf)
    try:
        page = document[page_number - 1]
        try:
            bitmap = page.render(scale=RENDER_DPI / 72)
            image = bitmap.to_pil().convert("L")
        finally:
            page.close()
    finally:
        document.close()
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def as_png(image: bytes) -> bytes:
    """An image file in a form Tesseract reads (PNG, grayscale; EXIF rotation applied)."""
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(image)) as img:
        converted = ImageOps.exif_transpose(img).convert("L")
    out = io.BytesIO()
    converted.save(out, format="PNG")
    return out.getvalue()


# --- merging recognized pages into the stored text ---------------------------------


def merge_pages(content: str, recognized: dict[int, str]) -> str:
    """Add recognized pages to a PDF's text, in page order, with ``[Page N]`` markers.

    ``content`` is the text layer as the PDF parser writes it (pages without text
    have no marker). A recognized page replaces a page of the same number, so
    merging again gives the same result.
    """
    pages: dict[int, str] = {}
    preamble = ""
    matches = list(_PAGE_MARKER.finditer(content))
    if matches:
        preamble = content[: matches[0].start()].strip()
        for i, match in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
            pages[int(match.group(1))] = content[match.end() : end].strip()
    else:
        preamble = content.strip()
    for number, text in recognized.items():
        if text.strip():
            pages[number] = text.strip()
    parts = [preamble] if preamble else []
    parts += [f"[Page {n}]\n{pages[n]}" for n in sorted(pages)]
    return "\n\n".join(parts)


# --- Tesseract ----------------------------------------------------------------------


@dataclass(frozen=True)
class Tesseract:
    """A Tesseract installation: the program, its language data and the languages to use."""

    program: str
    tessdata: str | None
    languages: tuple[str, ...]
    version: str


_lock = threading.Lock()
_found: Tesseract | None = None
_looked = False


def _candidates() -> list[tuple[str, str | None]]:
    """``(program, tessdata folder or None)`` in the order they are tried."""
    found: list[tuple[str, str | None]] = []
    env = os.environ.get("RLM_TESSERACT", "").strip()
    if env:
        found.append((env, os.environ.get("TESSDATA_PREFIX") or None))
    if getattr(sys, "frozen", False):
        # The copy bundled next to the backend (packaging puts it there).
        bundled = Path(sys.executable).resolve().parent / "tesseract"
        exe = bundled / ("tesseract.exe" if sys.platform == "win32" else "tesseract")
        found.append((str(exe), str(bundled / "tessdata")))
    on_path = shutil.which("tesseract")
    if on_path:
        found.append((on_path, None))
    return found


def _run(
    program: str, tessdata: str | None, args: list[str], **kwargs
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # One core per page: OCR must never take the whole machine.
    env["OMP_THREAD_LIMIT"] = "1"
    if tessdata:
        env["TESSDATA_PREFIX"] = tessdata
    command = [program, *args]
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS
    elif shutil.which("nice"):
        command = ["nice", "-n", "10", *command]
    return subprocess.run(  # noqa: S603 (a fixed program found above, no shell)
        command, env=env, capture_output=True, creationflags=creationflags, check=False, **kwargs
    )


def tesseract() -> Tesseract | None:
    """The Tesseract to use, or None when none is installed (looked up once)."""
    global _found, _looked
    with _lock:
        if _looked:
            return _found
        _looked = True
        for program, tessdata in _candidates():
            if not Path(program).is_file():
                continue
            try:
                version = _run(program, tessdata, ["--version"], timeout=30)
                listed = _run(program, tessdata, ["--list-langs"], timeout=30)
            except (OSError, subprocess.TimeoutExpired) as e:
                logger.warning("OCR: %s does not run: %s", program, e)
                continue
            if version.returncode != 0:
                continue
            installed = set(listed.stdout.decode("utf-8", "replace").split())
            languages = tuple(lang for lang in LANGUAGES if lang in installed)
            if not languages:
                logger.warning(
                    "OCR: %s has none of the languages %s", program, ", ".join(LANGUAGES)
                )
                continue
            first = (version.stdout or version.stderr).decode("utf-8", "replace").splitlines()
            _found = Tesseract(program, tessdata, languages, first[0].strip() if first else "")
            logger.info("OCR: %s (%s), languages %s", program, _found.version, "+".join(languages))
            return _found
        logger.info("OCR: no Tesseract found; folders with OCR on wait until one is installed")
        return None


def reset_tesseract_lookup() -> None:
    """Look Tesseract up again on the next call (tests, or after an install)."""
    global _found, _looked
    with _lock:
        _found, _looked = None, False


def recognize(png: bytes, engine: Tesseract) -> str:
    """The text Tesseract recognizes in a PNG image.

    Raises:
        RuntimeError: Tesseract failed or timed out.
    """
    try:
        result = _run(
            engine.program,
            engine.tessdata,
            ["stdin", "stdout", "-l", "+".join(engine.languages), "--psm", "3"],
            input=png,
            timeout=TESSERACT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Tesseract took longer than {TESSERACT_TIMEOUT_SECONDS} s") from None
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(f"Tesseract failed: {message[-1] if message else result.returncode}")
    return result.stdout.decode("utf-8", "replace").strip()
