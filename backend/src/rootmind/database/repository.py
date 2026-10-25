"""Database repository for document operations with FTS5 support."""

import hashlib
import logging
import math
import re
from collections.abc import Callable, Collection
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import bindparam, create_engine, delete, event, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from .models import Base, Document, SyncStatus

logger = logging.getLogger(__name__)

# Page/slide/sheet markers inserted at index time (see indexer/parser.py) so a
# character position within stored content can be mapped back to a citable
# location. PDF pages get an explicit "[Page N]" marker; PPTX/XLSX use
# "=== Slide N ===" / "=== Sheet: Name ===" section headers, reused here as-is.
# Plain text/docx have no page concept and simply won't match.
_PAGE_MARKER_RE = re.compile(r"\[Page (\d+)\]")
_SLIDE_MARKER_RE = re.compile(r"=== Slide (\d+) ===")
_SHEET_MARKER_RE = re.compile(r"=== Sheet: (.+?) ===")
_LOCATION_PATTERNS = (
    (_PAGE_MARKER_RE, lambda m: f"Page {m.group(1)}"),
    (_SLIDE_MARKER_RE, lambda m: f"Slide {m.group(1)}"),
    (_SHEET_MARKER_RE, lambda m: f"Sheet: {m.group(1)}"),
)

# Longest location marker ("=== Sheet: <name> ===", Excel sheet names have at
# most 31 characters), so a marker starting exactly at a position is read whole.
_MARKER_LOOKAHEAD = 64

# FTS5 snippet() markers. Control characters that extracted text practically
# never contains, so the highlights can be told apart from the text and the
# snippet, with the markers removed, can be found again in the stored content.
# They are replaced by "**" / "..." before a snippet is returned.
_HIT_OPEN, _HIT_CLOSE, _SNIPPET_ELLIPSIS = "\x02", "\x03", "\x1f"

# A hit's position is the start of the densest cluster of highlighted query
# terms in its snippet: matches at most this many characters apart, with no
# page/slide/sheet marker between them, count as one place in the document.
_CLUSTER_CHARS = 500

# Upper bound for ids per "IN (...)" statement, well below SQLite's variable limit.
_DELETE_BATCH_SIZE = 500
_META_BATCH_SIZE = 500

# Candidates each search strategy ranks, independent of the requested limit
# (the REST API's maximum), so the result order never depends on the limit.
_RANK_CANDIDATES = 1000

# Unicode non-breaking hyphen; often used in typeset brand or product names.
_NB_HYPHEN = "‑"

# Fuzzy search: trigrams come from the first characters and distinct words of
# each document, so the trigram table stays small.
_TRIGRAM_SAMPLE_CHARS = 50_000
_TRIGRAM_MAX_WORDS = 1000
_TRIGRAM_REBUILD_BATCH = 100


def _label_from_window(window: str, upto: int | None = None) -> str | None:
    """Return the label of the last location marker in ``window``, if any.

    ``window`` should be the text preceding a position, so the *last* marker
    found is the one whose section the position falls in. With ``upto``, the
    window may extend past the position (index ``upto`` in ``window``): markers
    that start after it are ignored, while a marker that starts exactly at the
    position counts, because that character already belongs to the new section.
    """
    best: tuple[int, str] | None = None
    for pattern, fmt in _LOCATION_PATTERNS:
        last = None
        for match in pattern.finditer(window):
            if upto is not None and match.start() > upto:
                break
            last = match
        if last is not None and (best is None or last.start() > best[0]):
            best = (last.start(), fmt(last))
    return best[1] if best else None


def _word_key(value: str) -> str:
    """Case-folded words of ``value`` joined by single spaces (for matching terms to highlights)."""
    return " ".join(re.findall(r"\w+", value.casefold()))


def _parse_snippet(raw: str) -> tuple[str, list[tuple[int, int]]]:
    """Split an FTS5 snippet into its plain text and the highlighted spans.

    The plain text is the snippet without markers and ellipses, so it is a
    verbatim slice of the indexed content. Spans are ``(start, end)`` offsets
    into that plain text.
    """
    if raw.startswith(_SNIPPET_ELLIPSIS):
        raw = raw[len(_SNIPPET_ELLIPSIS) :]
    if raw.endswith(_SNIPPET_ELLIPSIS):
        raw = raw[: -len(_SNIPPET_ELLIPSIS)]
    plain: list[str] = []
    spans: list[tuple[int, int]] = []
    length = 0
    start: int | None = None
    for char in raw:
        if char == _HIT_OPEN:
            start = length
        elif char == _HIT_CLOSE:
            if start is not None and length > start:
                spans.append((start, length))
            start = None
        else:
            plain.append(char)
            length += 1
    return "".join(plain), spans


def _best_cluster(plain: str, spans: list[tuple[int, int]], search_terms: list[str]) -> int | None:
    """Offset (into ``plain``) of the place where the most query terms meet.

    Each highlighted span counts for the query terms it matches (a phrase
    highlight such as "Ultralight 1P tent" matches three). From every span,
    the following spans within :data:`_CLUSTER_CHARS` characters and on the
    same page, slide or sheet are pooled; the span whose pool holds the most
    distinct terms wins, the earliest one on a tie. A two-page invoice
    searched for "INV-2026-0142 total due" thus points at page 2, where all
    three terms are, not at the invoice number at the top of page 1.
    """
    if not spans:
        return None
    term_keys = [key for key in (_word_key(t) for t in search_terms) if key]
    span_terms: list[frozenset[Any]] = []
    for index, (start, end) in enumerate(spans):
        words = " " + _word_key(plain[start:end])
        matched = frozenset(i for i, key in enumerate(term_keys) if " " + key in words)
        # A highlight no term explains (e.g. a different Unicode spelling)
        # still counts as one matched term.
        span_terms.append(matched or frozenset({("span", index)}))
    markers = sorted(
        match.start() for pattern, _ in _LOCATION_PATTERNS for match in pattern.finditer(plain)
    )

    def section(offset: int) -> int:
        return sum(1 for marker in markers if marker <= offset)

    best_offset, best_score = None, 0
    for i, (start, _) in enumerate(spans):
        pooled: set[Any] = set()
        for j in range(i, len(spans)):
            other = spans[j][0]
            if other - start > _CLUSTER_CHARS or section(other) != section(start):
                break
            pooled |= span_terms[j]
        if len(pooled) > best_score:
            best_offset, best_score = start, len(pooled)
    return best_offset


def generate_trigrams(text: str) -> set[str]:
    """Generate trigrams from text for fuzzy matching.

    Args:
        text: Input text to generate trigrams from.

    Returns:
        Set of trigrams (3-character sequences).
    """
    if not text or len(text) < 3:
        return set()

    # Normalize to lowercase and pad with spaces for edge trigrams.
    text = f"  {text.lower()}  "

    trigrams = set()
    for i in range(len(text) - 2):
        trigram = text[i : i + 3]
        # Skip trigrams that are all spaces
        if trigram.strip():
            trigrams.add(trigram)

    return trigrams


def trigram_similarity(text1: str, text2: str) -> float:
    """Calculate trigram similarity between two strings.

    Returns a value between 0.0 (no match) and 1.0 (identical).
    """
    trigrams1 = generate_trigrams(text1)
    trigrams2 = generate_trigrams(text2)

    if not trigrams1 or not trigrams2:
        return 0.0

    intersection = len(trigrams1 & trigrams2)
    union = len(trigrams1 | trigrams2)

    return intersection / union if union > 0 else 0.0


def _fts_quote(term: str) -> str:
    """Quote a search term as an FTS5 string, so operators and '-' are literal."""
    return '"' + term.replace('"', '""') + '"'


class DocumentRepository:
    """Repository for document database operations with FTS5 full-text search."""

    def __init__(self, db_path: str | Path = "./data/index.db"):
        """Initialize the repository.

        Args:
            db_path: Path to SQLite database file.
        """
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        self.db_path = db_path
        self.engine = create_engine(
            f"sqlite:///{db_path}",
            # Connections are shared between the event loop and worker threads.
            connect_args={"check_same_thread": False},
        )

        # WAL mode for concurrent readers while one writer indexes.
        @event.listens_for(self.engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA cache_size=-64000")  # 64MB cache
            cursor.close()

        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)

        # Create FTS5 virtual table if not exists
        self._setup_fts5()

    def _setup_fts5(self):
        """Create FTS5 virtual table for full-text search."""
        with self.engine.connect() as conn:
            # Create FTS5 table (standalone, stores its own content)
            conn.execute(
                text("""
                CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
                    doc_id UNINDEXED,
                    file_name,
                    content,
                    tokenize='unicode61 remove_diacritics 0'
                )
            """)
            )

            # Create trigram table for fuzzy search
            conn.execute(
                text("""
                CREATE TABLE IF NOT EXISTS document_trigrams (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    doc_id TEXT NOT NULL,
                    trigram TEXT NOT NULL,
                    source TEXT NOT NULL,
                    UNIQUE(doc_id, trigram, source)
                )
            """)
            )

            # Create index for fast trigram lookups
            conn.execute(
                text("""
                CREATE INDEX IF NOT EXISTS idx_trigram ON document_trigrams(trigram)
            """)
            )

            conn.execute(
                text("""
                CREATE INDEX IF NOT EXISTS idx_doc_id_trigrams ON document_trigrams(doc_id)
            """)
            )

            # Index for "recently indexed" queries (ORDER BY indexed_at DESC).
            # create_all() won't add indexes to a pre-existing table, so ensure it here.
            conn.execute(
                text("""
                CREATE INDEX IF NOT EXISTS idx_documents_indexed_at ON documents(indexed_at)
            """)
            )

            conn.commit()

    def _get_session(self) -> Session:
        """Get a new database session."""
        return self.SessionLocal()

    @staticmethod
    def generate_doc_id(connector_name: str, file_path: str) -> str:
        """Generate a unique document ID from connector and path."""
        combined = f"{connector_name}:{file_path}"
        return hashlib.sha256(combined.encode()).hexdigest()[:32]

    def get_document(self, doc_id: str) -> Document | None:
        """Get a document by ID (loads the full row, including the extracted text)."""
        with self._get_session() as session:
            return session.get(Document, doc_id)

    def get_document_by_path(self, connector_name: str, file_path: str) -> Document | None:
        """Get a document by connector and path."""
        doc_id = self.generate_doc_id(connector_name, file_path)
        return self.get_document(doc_id)

    def upsert_document(
        self,
        connector_name: str,
        file_path: str,
        file_name: str,
        file_type: str,
        size_bytes: int | None = None,
        created_at: datetime | None = None,
        modified_at: datetime | None = None,
        content_hash: str | None = None,
        content_text: str | None = None,
        page_count: int | None = None,
        status: str = "indexed",
        error_message: str | None = None,
    ) -> Document:
        """Insert or update a document."""
        doc_id = self.generate_doc_id(connector_name, file_path)

        with self._get_session() as session:
            doc = session.get(Document, doc_id)
            is_new = doc is None

            if is_new:
                doc = Document(
                    id=doc_id,
                    connector_name=connector_name,
                    file_path=file_path,
                    file_name=file_name,
                    file_type=file_type,
                )
                session.add(doc)

            # Update fields
            doc.size_bytes = size_bytes
            doc.created_at = created_at
            doc.modified_at = modified_at
            doc.content_hash = content_hash
            doc.content_text = content_text
            doc.content_length = len(content_text) if content_text else None
            doc.page_count = page_count
            doc.status = status
            doc.error_message = error_message
            doc.indexed_at = datetime.now()

            session.commit()

            # Update FTS5 index
            if content_text and status == "indexed":
                self._update_fts_index(doc_id, file_name, content_text, is_new)

            session.refresh(doc)
            return doc

    def _update_fts_index(self, doc_id: str, file_name: str, content: str, is_new: bool):
        """Update the FTS5 index for a document."""
        with self.engine.connect() as conn:
            if not is_new:
                # Delete old entry
                conn.execute(
                    text("DELETE FROM documents_fts WHERE doc_id = :doc_id"), {"doc_id": doc_id}
                )

            # Insert new entry
            conn.execute(
                text(
                    "INSERT INTO documents_fts (doc_id, file_name, content) "
                    "VALUES (:doc_id, :file_name, :content)"
                ),
                {"doc_id": doc_id, "file_name": file_name, "content": content},
            )
            conn.commit()

        # Update trigram index
        self._update_trigram_index(doc_id, file_name, content, is_new)

    def _update_trigram_index(self, doc_id: str, file_name: str, content: str, is_new: bool):
        """Update the trigram index for fuzzy search."""
        with self.engine.connect() as conn:
            if not is_new:
                # Delete old trigrams
                conn.execute(
                    text("DELETE FROM document_trigrams WHERE doc_id = :doc_id"),
                    {"doc_id": doc_id},
                )

            # Generate trigrams from filename
            filename_trigrams = generate_trigrams(file_name)

            # Words (3+ chars) from the start of the content. The first distinct
            # words in document order, so the selection is the same on every
            # run (a set's order depends on hash randomization).
            content_sample = content[:_TRIGRAM_SAMPLE_CHARS]
            words = list(dict.fromkeys(re.findall(r"\b\w{3,}\b", content_sample.lower())))
            words = words[:_TRIGRAM_MAX_WORDS]

            content_trigrams = set()
            for word in words:
                content_trigrams.update(generate_trigrams(word))

            # Batch insert all trigrams at once
            all_trigrams = []
            for trigram in filename_trigrams:
                all_trigrams.append({"doc_id": doc_id, "trigram": trigram, "source": "filename"})
            for trigram in content_trigrams:
                all_trigrams.append({"doc_id": doc_id, "trigram": trigram, "source": "content"})

            # Use executemany for batch insert
            if all_trigrams:
                conn.execute(
                    text(
                        "INSERT OR IGNORE INTO document_trigrams (doc_id, trigram, source) "
                        "VALUES (:doc_id, :trigram, :source)"
                    ),
                    all_trigrams,
                )

            conn.commit()

    def delete_document(self, doc_id: str) -> bool:
        """Delete a document by ID."""
        return self.delete_documents([doc_id]) > 0

    def delete_documents(self, doc_ids: list[str]) -> int:
        """Delete multiple documents in a small number of transactions.

        Batches the documents/documents_fts/document_trigrams deletes instead
        of committing per document, so pruning many files (e.g. after removing
        indexed folders) doesn't monopolize the single SQLite writer with
        thousands of sequential small transactions.
        """
        removed = 0
        for i in range(0, len(doc_ids), _DELETE_BATCH_SIZE):
            removed += self._delete_batch(doc_ids[i : i + _DELETE_BATCH_SIZE])
        return removed

    def _delete_batch(self, doc_ids: list[str]) -> int:
        if not doc_ids:
            return 0

        with self._get_session() as session:
            existing = (
                session.execute(select(Document.id).where(Document.id.in_(doc_ids))).scalars().all()
            )
            if not existing:
                return 0
            session.execute(delete(Document).where(Document.id.in_(existing)))
            session.commit()

        with self.engine.connect() as conn:
            conn.execute(
                text("DELETE FROM documents_fts WHERE doc_id IN :doc_ids").bindparams(
                    bindparam("doc_ids", expanding=True)
                ),
                {"doc_ids": existing},
            )
            conn.execute(
                text("DELETE FROM document_trigrams WHERE doc_id IN :doc_ids").bindparams(
                    bindparam("doc_ids", expanding=True)
                ),
                {"doc_ids": existing},
            )
            conn.commit()

        return len(existing)

    def delete_by_connector(self, connector_name: str) -> int:
        """Delete all documents (rows, FTS entries and trigrams) of a connector."""
        with self._get_session() as session:
            doc_ids = list(
                session.execute(
                    select(Document.id).where(Document.connector_name == connector_name)
                )
                .scalars()
                .all()
            )
        return self.delete_documents(doc_ids)

    def prune_connectors(self, keep: Collection[str]) -> int:
        """Delete everything that belongs to connectors not listed in ``keep``.

        Called with the connector names from the current config, so documents
        of a folder that was removed from the config stop being searchable,
        readable and citable. Matching is by configured name, not by whether a
        connector could be initialized (an unreachable folder keeps its index).

        Returns:
            Number of documents removed.
        """
        keep = set(keep)
        with self._get_session() as session:
            names = set(session.execute(select(Document.connector_name).distinct()).scalars())
            names |= set(session.execute(select(SyncStatus.connector_name)).scalars())
        orphans = sorted(names - keep)
        removed = 0
        for name in orphans:
            removed += self.delete_by_connector(name)
        if orphans:
            with self._get_session() as session:
                session.execute(delete(SyncStatus).where(SyncStatus.connector_name.in_(orphans)))
                session.commit()
            logger.info(
                "Pruned %d document(s) of %d connector(s) no longer in the config",
                removed,
                len(orphans),
            )
        return removed

    def list_document_paths(self, connector_name: str) -> list[tuple[str, str]]:
        """Return ``(doc_id, file_path)`` for all documents of a connector (no content)."""
        with self._get_session() as session:
            rows = session.execute(
                select(Document.id, Document.file_path).where(
                    Document.connector_name == connector_name
                )
            ).all()
        return [(row.id, row.file_path) for row in rows]

    def get_document_meta(self, doc_id: str) -> dict[str, Any] | None:
        """Lightweight document metadata (no content_text).

        Loading the full Document entity pulls in content_text (often megabytes),
        which makes search very slow when done per match.
        """
        with self._get_session() as session:
            row = session.execute(
                select(
                    Document.file_name,
                    Document.file_path,
                    Document.file_type,
                    Document.connector_name,
                    Document.status,
                    Document.content_length,
                    Document.page_count,
                ).where(Document.id == doc_id)
            ).first()
        if not row:
            return None
        return {
            "doc_id": doc_id,
            "file_name": row.file_name,
            "file_path": row.file_path,
            "file_type": row.file_type,
            "connector_name": row.connector_name,
            "status": row.status,
            "content_length": row.content_length,
            "page_count": row.page_count,
        }

    def _metas(self, doc_ids: Collection[str]) -> dict[str, dict[str, Any]]:
        """Lightweight metadata (no content) for many documents, in batched queries."""
        ids = list(dict.fromkeys(doc_ids))
        metas: dict[str, dict[str, Any]] = {}
        with self._get_session() as session:
            for i in range(0, len(ids), _META_BATCH_SIZE):
                rows = session.execute(
                    select(
                        Document.id,
                        Document.file_name,
                        Document.file_path,
                        Document.file_type,
                        Document.connector_name,
                        Document.status,
                        Document.content_length,
                        Document.page_count,
                    ).where(Document.id.in_(ids[i : i + _META_BATCH_SIZE]))
                ).all()
                for row in rows:
                    metas[row.id] = {
                        "file_name": row.file_name,
                        "file_path": row.file_path,
                        "file_type": row.file_type,
                        "connector_name": row.connector_name,
                        "status": row.status,
                        "content_length": row.content_length,
                        "page_count": row.page_count,
                    }
        return metas

    def search_fulltext(
        self,
        query: str,
        limit: int = 20,
        connector_name: str | None = None,
        file_type: str | None = None,
        with_snippets: bool = True,
        connector_names: Collection[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Full-text search using FTS5 with a multi-strategy approach.

        Uses multiple search strategies for better recall:
        1. Exact phrase match (highest weight)
        2. AND search with exact terms
        3. AND search with prefix matching
        4. OR search with prefix matching
        5. Filename search as fallback

        Ranking runs first, without snippets, over a fixed number of candidates
        per strategy. The order therefore does not depend on ``limit``: a
        larger limit only appends results, so "load more" paging is stable.
        Snippets are computed afterwards, for the returned results only.

        ``with_snippets=False`` skips the FTS5 snippet() highlight, which scans
        the (often huge) document content and dominates the query time. Use it
        when only the result list is needed.

        ``connector_names`` restricts results to documents of those connectors.
        """
        # Strip FTS5 syntax characters; every remaining term is quoted below,
        # so words like AND/OR/NEAR and hyphenated terms are matched literally.
        safe_query = re.sub(r"[^\w\s\-]", " ", query)
        search_terms = [t for t in safe_query.split() if len(t) >= 2]

        if not search_terms:
            return []

        allowed = set(connector_names) if connector_names is not None else None

        # Each strategy: (label, MATCH column, MATCH query, weight, snippet column).
        # Snippet column 2 = content, column 1 = file_name.
        strategies: list[tuple[str, str, str, float, int]] = []
        if len(search_terms) > 1:
            phrase = '"' + " ".join(search_terms) + '"'
            strategies.append(("phrase", "documents_fts", phrase, 3.0, 2))
        strategies.append(
            ("and", "documents_fts", " AND ".join(_fts_quote(t) for t in search_terms), 2.5, 2)
        )
        strategies.append(
            (
                "and-prefix",
                "documents_fts",
                " AND ".join(_fts_quote(t) + "*" for t in search_terms),
                2.0,
                2,
            )
        )
        strategies.append(
            (
                "or-prefix",
                "documents_fts",
                " OR ".join(_fts_quote(t) + "*" for t in search_terms),
                1.0,
                2,
            )
        )
        for term in search_terms:
            strategies.append(("filename", "file_name", _fts_quote(term) + "*", 0.5, 1))

        candidates = max(limit, _RANK_CANDIDATES)
        # doc_id -> best weighted score, plus where the doc was found first:
        # (strategy index, rank within that strategy). Strategies run in
        # descending precision order (phrase -> AND -> prefix -> OR ->
        # filename), so the first one to find a doc is its most precise match
        # and provides the snippet.
        scores: dict[str, float] = {}
        first_hit: dict[str, tuple[int, int]] = {}

        with self.engine.connect() as conn:
            for index, (label, column, match_query, weight, _) in enumerate(strategies):
                # Only fixed SQL fragments are interpolated; the user query is bound.
                sql = text(
                    "SELECT doc_id, bm25(documents_fts) FROM documents_fts "  # noqa: S608
                    f"WHERE {column} MATCH :query "
                    "ORDER BY bm25(documents_fts), rowid LIMIT :limit"
                )
                try:
                    rows = conn.execute(sql, {"query": match_query, "limit": candidates}).fetchall()
                except Exception as e:  # malformed MATCH expression: try the next strategy
                    logger.debug("FTS strategy %s failed for %r: %s", label, match_query, e)
                    continue
                for rank, (doc_id, bm25) in enumerate(rows):
                    # bm25 magnitudes aren't comparable across MATCH query shapes
                    # (a broad OR query can score higher than a precise phrase
                    # purely because it matches more terms), so the weights
                    # favour precise strategies and each doc keeps its best score.
                    weighted = abs(bm25) * weight
                    if doc_id not in scores:
                        scores[doc_id] = weighted
                        first_hit[doc_id] = (index, rank)
                    elif weighted > scores[doc_id]:
                        scores[doc_id] = weighted

        metas = self._metas(scores)
        kept = []
        for doc_id, score in scores.items():  # insertion order = discovery order
            meta = metas.get(doc_id)
            if not meta or meta["status"] != "indexed":
                continue
            if connector_name and meta["connector_name"] != connector_name:
                continue
            if allowed is not None and meta["connector_name"] not in allowed:
                continue
            if file_type and meta["file_type"] != file_type:
                continue
            kept.append((doc_id, score, meta))

        # Highest score first; the sort is stable, so ties keep discovery order.
        kept.sort(key=lambda item: -item[1])
        top_results = [
            {
                "doc_id": doc_id,
                "file_name": meta["file_name"],
                "file_path": meta["file_path"],
                "file_type": meta["file_type"],
                "snippet": "",
                "score": score,
                "content_length": meta["content_length"],
                "page_count": meta["page_count"],
                "position": None,
                "page": None,
            }
            for doc_id, score, meta in kept[:limit]
        ]

        if with_snippets and top_results:
            content_snippets = self._attach_snippets(top_results, strategies, first_hit)
            self._attach_positions(top_results, search_terms, content_snippets)

        return top_results

    def _attach_snippets(
        self,
        results: list[dict[str, Any]],
        strategies: list[tuple[str, str, str, float, int]],
        first_hit: dict[str, tuple[int, int]],
    ) -> dict[str, str]:
        """Highlight each result with the strategy that found it first.

        One query per strategy, in the same order as the ranking query and
        limited to the deepest rank still needed, so snippet() runs for about
        as many rows as are returned (a per-row ``rowid =`` lookup would
        re-evaluate a broad prefix MATCH once per result).

        Returns the raw content snippets (with the internal markers) by doc id,
        for :meth:`_attach_positions`. Snippets of the file name are not
        included: they say nothing about a place in the text.
        """
        needed: dict[int, int] = {}  # strategy index -> rows to fetch
        for r in results:
            index, rank = first_hit[r["doc_id"]]
            needed[index] = max(needed.get(index, 0), rank + 1)

        snippets: dict[str, str] = {}
        content_snippets: dict[str, str] = {}
        with self.engine.connect() as conn:
            for index, depth in sorted(needed.items()):
                _, column, match_query, _, snippet_column = strategies[index]
                # Content snippets use a wider token window than the default:
                # short windows often cut off table headers/labels a few lines
                # above a matched data row, leaving a bare number the model
                # can't interpret.
                tokens = 200 if snippet_column == 2 else 64
                # Only fixed SQL fragments are interpolated; the query and the
                # markers are bound.
                sql = text(
                    f"SELECT doc_id, snippet(documents_fts, {snippet_column}, :open, :close, "  # noqa: S608
                    f":ellipsis, {tokens}) FROM documents_fts WHERE {column} MATCH :query "
                    "ORDER BY bm25(documents_fts), rowid LIMIT :limit"
                )
                params = {
                    "query": match_query,
                    "limit": depth,
                    "open": _HIT_OPEN,
                    "close": _HIT_CLOSE,
                    "ellipsis": _SNIPPET_ELLIPSIS,
                }
                for doc_id, snippet in conn.execute(sql, params):
                    if first_hit.get(doc_id, (None,))[0] == index and snippet:
                        if doc_id not in snippets and snippet_column == 2:
                            content_snippets[doc_id] = snippet
                        snippets.setdefault(doc_id, snippet)

        for r in results:
            if r["doc_id"] in snippets:
                r["snippet"] = (
                    snippets[r["doc_id"]]
                    .replace(_HIT_OPEN, "**")
                    .replace(_HIT_CLOSE, "**")
                    .replace(_SNIPPET_ELLIPSIS, "...")
                )
        return content_snippets

    def _attach_positions(
        self,
        results: list[dict[str, Any]],
        search_terms: list[str],
        content_snippets: dict[str, str] | None = None,
    ) -> None:
        """Find each result's char position in its document, and its location there.

        The position is taken from the FTS5 match itself: the snippet (without
        markers) is a verbatim slice of the stored text, so ``instr()`` finds
        where it starts, and its highlights give the matched terms' offsets.
        Of those, :func:`_best_cluster` picks the place where the most query
        terms occur together on one page, slide or sheet. The position thus
        always lies inside the snippet shown with the hit, and the page/slide/
        sheet is the one the snippet's best match is on. Before, only a
        literal occurrence of the whole query counted, which multi-word
        queries rarely have, so most hits had no position and no location.

        When the snippet has no highlight (e.g. a match in the file name only)
        or cannot be found again, the literal search phrase is looked up as
        before: also with '-' swapped for U+2011 (non-breaking hyphen), since
        hyphenated names are often typeset with that character, and then
        case-insensitively (SQLite's ``lower()`` folds ASCII only and keeps
        character offsets).

        Uses SQLite's ``instr()``/``substr()`` on the returned rows only, so
        this stays cheap even on huge documents.
        """
        content_snippets = content_snippets or {}
        phrase = " ".join(search_terms)
        candidates = [phrase]
        if "-" in phrase:
            candidates.append(phrase.replace("-", _NB_HYPHEN))
        # The documents table holds the same text as the FTS table but is keyed
        # by id; doc_id is an unindexed FTS column, so a lookup there would scan
        # the whole index once per result.
        lookups = (
            "SELECT instr(content_text, :p) FROM documents WHERE id = :d",
            "SELECT instr(lower(content_text), lower(:p)) FROM documents WHERE id = :d",
        )

        with self.engine.connect() as conn:
            for r in results:
                if r.get("position") is None and r["doc_id"] in content_snippets:
                    r["position"] = self._snippet_position(
                        conn, r["doc_id"], content_snippets[r["doc_id"]], search_terms
                    )
                if r.get("position") is None:
                    for sql, candidate in ((s, c) for s in lookups for c in candidates):
                        row = conn.execute(text(sql), {"p": candidate, "d": r["doc_id"]}).fetchone()
                        if row and row[0]:
                            r["position"] = row[0] - 1  # instr() is 1-based
                            break
                if r.get("position") is not None:
                    r["page"] = self._page_for_position(conn, r["doc_id"], r["position"])

    @staticmethod
    def _snippet_position(conn, doc_id: str, raw: str, search_terms: list[str]) -> int | None:
        """Char position of the best highlighted match of snippet ``raw`` in the document."""
        plain, spans = _parse_snippet(raw)
        offset = _best_cluster(plain, spans, search_terms)
        if offset is None or not plain:
            return None
        row = conn.execute(
            text("SELECT instr(content_text, :p) FROM documents WHERE id = :d"),
            {"p": plain, "d": doc_id},
        ).fetchone()
        if not row or not row[0]:
            return None
        return row[0] - 1 + offset  # instr() is 1-based

    def _page_for_position(
        self, conn, doc_id: str, position: int, lookback: int = 6000
    ) -> str | None:
        """Resolve a char position to a page/slide/sheet label using ``conn``.

        Fetches only a small window of content (via ``substr``, not the whole
        document) immediately preceding the position and looks for the
        closest location marker in it.
        """
        if position is None or position < 0:
            return None
        start = max(1, position - lookback + 1)  # substr() is 1-based
        # Read a little past the position so a marker that starts exactly there
        # is seen whole; _label_from_window ignores markers that start later.
        length = position - start + 1 + _MARKER_LOOKAHEAD
        row = conn.execute(
            text("SELECT substr(content_text, :start, :length) FROM documents WHERE id = :d"),
            {"start": start, "length": length, "d": doc_id},
        ).fetchone()
        if not row or not row[0]:
            return None
        return _label_from_window(row[0], upto=position - (start - 1))

    def resolve_page(self, doc_id: str, position: int | None) -> str | None:
        """Public entry point: resolve a char position to a page/slide/sheet label.

        Returns None for file types without location markers (txt, docx), or
        for documents indexed before markers existed (needs a re-index).
        """
        if position is None:
            return None
        with self.engine.connect() as conn:
            return self._page_for_position(conn, doc_id, position)

    def search_fuzzy(
        self,
        query: str,
        limit: int = 20,
        connector_name: str | None = None,
        file_type: str | None = None,
        min_similarity: float = 0.3,
        connector_names: Collection[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Fuzzy search using trigram matching for typo tolerance.

        This is more expensive than FTS5 but handles:
        - Typos ("lantren" vs "lantern")
        - Partial matches
        - Character transpositions

        Args:
            query: Search query (can contain typos).
            limit: Maximum results to return.
            connector_name: Optional filter by connector.
            file_type: Optional filter by file type.
            min_similarity: Minimum trigram similarity (0.0-1.0).
            connector_names: Optional set of allowed connectors.

        Returns:
            List of matching documents with similarity scores.
        """
        query_trigrams = sorted(generate_trigrams(query))

        if not query_trigrams:
            return []
        if connector_names is not None and not connector_names:
            return []

        # Similarity is the share of the query's trigrams a document contains.
        # Status, folder and type filters run in SQL, before the LIMIT, so a
        # filtered search still finds every match. Only fixed SQL fragments
        # are interpolated; all values are bound.
        min_matches = max(1, math.ceil(min_similarity * len(query_trigrams) - 1e-9))
        conditions = ["t.trigram IN :trigrams", "d.status = 'indexed'"]
        params: dict[str, Any] = {
            "trigrams": query_trigrams,
            "min_matches": min_matches,
            "limit": limit,
        }
        if connector_name:
            conditions.append("d.connector_name = :connector_name")
            params["connector_name"] = connector_name
        if connector_names is not None:
            conditions.append("d.connector_name IN :connector_names")
            params["connector_names"] = sorted(connector_names)
        if file_type:
            conditions.append("d.file_type = :file_type")
            params["file_type"] = file_type
        sql = text(
            "SELECT t.doc_id, COUNT(DISTINCT t.trigram) AS matched "  # noqa: S608
            "FROM document_trigrams t JOIN documents d ON d.id = t.doc_id "
            f"WHERE {' AND '.join(conditions)} "
            "GROUP BY t.doc_id HAVING COUNT(DISTINCT t.trigram) >= :min_matches "
            "ORDER BY matched DESC, t.doc_id LIMIT :limit"
        ).bindparams(bindparam("trigrams", expanding=True))
        if connector_names is not None:
            sql = sql.bindparams(bindparam("connector_names", expanding=True))

        with self.engine.connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        metas = self._metas([row[0] for row in rows])
        kept = [
            ({"doc_id": doc_id, "similarity": matched / len(query_trigrams)}, metas[doc_id])
            for doc_id, matched in rows
            if doc_id in metas
        ]

        results = []
        for candidate, meta in kept:
            content_text = self._content_text(candidate["doc_id"])

            # Get a snippet from content
            snippet = ""
            position = None
            if content_text:
                content_lower = content_text.lower()
                query_lower = query.lower()

                # Find best position for snippet
                pos = content_lower.find(query_lower[:5]) if len(query_lower) >= 5 else -1
                if pos == -1:
                    # Try first word
                    first_word = query.split()[0].lower() if query.split() else ""
                    pos = content_lower.find(first_word) if first_word else -1

                if pos >= 0:
                    position = pos
                    start = max(0, pos - 50)
                    end = min(len(content_text), pos + 150)
                    snippet = "..." + content_text[start:end] + "..."
                else:
                    snippet = content_text[:200] + "..."

            # The text is already loaded here, so resolve the page label
            # directly in Python instead of another DB round-trip.
            page = None
            if position is not None and content_text:
                lo = max(0, position - 6000)
                window = content_text[lo : position + _MARKER_LOOKAHEAD]
                page = _label_from_window(window, upto=position - lo)

            results.append(
                {
                    "doc_id": candidate["doc_id"],
                    "file_name": meta["file_name"],
                    "file_path": meta["file_path"],
                    "file_type": meta["file_type"],
                    "snippet": snippet,
                    "position": position,
                    "page": page,
                    "score": candidate["similarity"],
                    "similarity": f"{candidate['similarity'] * 100:.0f}%",
                    "content_length": meta["content_length"],
                    "page_count": meta["page_count"],
                }
            )

        return results

    def _content_text(self, doc_id: str) -> str | None:
        """Load only the extracted text of one document."""
        with self._get_session() as session:
            return session.execute(
                select(Document.content_text).where(Document.id == doc_id)
            ).scalar_one_or_none()

    def get_document_content(
        self,
        doc_id: str,
        start_char: int | None = None,
        end_char: int | None = None,
    ) -> dict[str, Any] | None:
        """Get document content, optionally a character range ``[start_char, end_char)``.

        A range is cut out in SQL (``substr``), so reading a window of a large
        document does not load its whole text.
        """
        with self._get_session() as session:
            row = session.execute(
                select(
                    Document.file_name,
                    Document.file_path,
                    Document.file_type,
                    Document.connector_name,
                    Document.page_count,
                    func.length(Document.content_text).label("total"),
                ).where(Document.id == doc_id)
            ).first()
            if not row:
                return None
            total_length = row.total or 0

            if start_char is None and end_char is None:
                selected = Document.content_text
            else:
                start = max(0, start_char or 0)
                end = end_char if end_char is not None else total_length
                # substr() is 1-based and takes a length.
                selected = func.substr(Document.content_text, start + 1, max(0, end - start))
            content = session.execute(select(selected).where(Document.id == doc_id)).scalar()

        return {
            "doc_id": doc_id,
            "file_name": row.file_name,
            "file_path": row.file_path,
            "file_type": row.file_type,
            "connector_name": row.connector_name,
            "content": content or "",
            "content_length": total_length,
            "range_start": start_char,
            "range_end": end_char,
            "page_count": row.page_count,
        }

    def list_documents(
        self,
        connector_name: str | None = None,
        file_type: str | None = None,
        search_filename: str | None = None,
        limit: int = 100,
        offset: int = 0,
        connector_names: Collection[str] | None = None,
    ) -> list[dict[str, Any]]:
        """List documents with metadata (no content)."""
        with self._get_session() as session:
            query = select(
                Document.id,
                Document.file_name,
                Document.file_path,
                Document.file_type,
                Document.size_bytes,
                Document.content_length,
                Document.page_count,
                Document.modified_at,
            ).where(Document.status == "indexed")

            if connector_name:
                query = query.where(Document.connector_name == connector_name)
            if connector_names is not None:
                query = query.where(Document.connector_name.in_(list(connector_names)))
            if file_type:
                query = query.where(Document.file_type == file_type)
            if search_filename:
                query = query.where(Document.file_name.ilike(f"%{search_filename}%"))

            # Stable order (id as tiebreak) so limit/offset pages never overlap.
            query = query.order_by(Document.modified_at.desc(), Document.id)
            query = query.offset(offset).limit(limit)

            rows = session.execute(query).all()

            return [
                {
                    "doc_id": row.id,
                    "file_name": row.file_name,
                    "file_path": row.file_path,
                    "file_type": row.file_type,
                    "size_bytes": row.size_bytes,
                    "content_length": row.content_length,
                    "page_count": row.page_count,
                    "modified_at": row.modified_at.isoformat() if row.modified_at else None,
                }
                for row in rows
            ]

    def recent_documents(self, limit: int = 20) -> list[dict[str, Any]]:
        """List the most recently indexed documents (newest first).

        Selects only the small metadata columns; loading the full Document
        entity would also fetch ``content_text`` (the entire extracted document
        text, often megabytes) for every row, which makes this query very slow.
        """
        with self._get_session() as session:
            query = (
                select(
                    Document.id,
                    Document.file_name,
                    Document.file_path,
                    Document.file_type,
                    Document.size_bytes,
                    Document.connector_name,
                    Document.indexed_at,
                )
                .where(Document.status == "indexed")
                .order_by(Document.indexed_at.desc(), Document.id)
                .limit(limit)
            )
            rows = session.execute(query).all()
            return [
                {
                    "doc_id": row.id,
                    "file_name": row.file_name,
                    "file_path": row.file_path,
                    "file_type": row.file_type,
                    "size_bytes": row.size_bytes,
                    "connector_name": row.connector_name,
                    "indexed_at": row.indexed_at.isoformat() if row.indexed_at else None,
                }
                for row in rows
            ]

    def count_documents(
        self,
        connector_name: str | None = None,
        status: str | None = None,
    ) -> int:
        """Count documents with optional filters."""
        with self._get_session() as session:
            query = select(func.count(Document.id))

            if connector_name:
                query = query.where(Document.connector_name == connector_name)
            if status:
                query = query.where(Document.status == status)

            return session.scalar(query) or 0

    def get_statistics(self, connector_names: Collection[str] | None = None) -> dict[str, Any]:
        """Get database statistics, optionally only of the documents of ``connector_names``."""

        def scoped(query):
            if connector_names is None:
                return query
            return query.where(Document.connector_name.in_(sorted(connector_names)))

        with self._get_session() as session:
            total = session.scalar(scoped(select(func.count(Document.id)))) or 0
            indexed = (
                session.scalar(
                    scoped(select(func.count(Document.id)).where(Document.status == "indexed"))
                )
                or 0
            )
            errors = (
                session.scalar(
                    scoped(select(func.count(Document.id)).where(Document.status == "error"))
                )
                or 0
            )

            total_size = (
                session.scalar(
                    scoped(
                        select(func.sum(Document.size_bytes)).where(Document.status == "indexed")
                    )
                )
                or 0
            )

            total_content = (
                session.scalar(
                    scoped(
                        select(func.sum(Document.content_length)).where(
                            Document.status == "indexed"
                        )
                    )
                )
                or 0
            )

            # File types breakdown
            file_types = session.execute(
                scoped(
                    select(Document.file_type, func.count(Document.id)).where(
                        Document.status == "indexed"
                    )
                ).group_by(Document.file_type)
            ).all()

            return {
                "total_documents": total,
                "indexed_documents": indexed,
                "error_documents": errors,
                "total_size_bytes": total_size,
                "total_content_chars": total_content,
                "file_types": dict(file_types),
            }

    # Sync Status Methods

    def get_sync_status(self, connector_name: str) -> SyncStatus | None:
        """Get sync status for a connector."""
        with self._get_session() as session:
            return session.scalars(
                select(SyncStatus).where(SyncStatus.connector_name == connector_name)
            ).first()

    def update_sync_status(
        self,
        connector_name: str,
        last_sync_at: datetime | None = None,
        last_full_sync_at: datetime | None = None,
        documents_total: int | None = None,
        documents_indexed: int | None = None,
        documents_error: int | None = None,
        is_syncing: bool | None = None,
        error_message: str | None = None,
    ) -> SyncStatus:
        """Update sync status for a connector."""
        with self._get_session() as session:
            status = session.scalars(
                select(SyncStatus).where(SyncStatus.connector_name == connector_name)
            ).first()

            if status is None:
                status = SyncStatus(connector_name=connector_name)
                session.add(status)

            if last_sync_at is not None:
                status.last_sync_at = last_sync_at
            if last_full_sync_at is not None:
                status.last_full_sync_at = last_full_sync_at
            if documents_total is not None:
                status.documents_total = documents_total
            if documents_indexed is not None:
                status.documents_indexed = documents_indexed
            if documents_error is not None:
                status.documents_error = documents_error
            if is_syncing is not None:
                status.is_syncing = is_syncing
            if error_message is not None:
                status.error_message = error_message or None  # "" clears it

            session.commit()
            session.refresh(status)
            return status

    def get_all_sync_statuses(self) -> list[SyncStatus]:
        """Get sync status for all connectors."""
        with self._get_session() as session:
            return list(session.scalars(select(SyncStatus)).all())

    def rebuild_trigram_index(
        self, progress_callback: Callable[[int, int], None] | None = None
    ) -> int:
        """Rebuild the trigram index for all documents.

        Args:
            progress_callback: Optional callback(current, total) for progress updates.

        Returns:
            Number of documents processed.
        """
        # Clear existing trigrams
        with self.engine.connect() as conn:
            conn.execute(text("DELETE FROM document_trigrams"))
            conn.commit()

        with self._get_session() as session:
            ids = list(
                session.execute(
                    select(Document.id).where(Document.status == "indexed").order_by(Document.id)
                ).scalars()
            )
        total = len(ids)
        count = 0

        # In batches, and only the part of the text the trigrams are built
        # from, so the whole index never has to fit in memory.
        sample = func.substr(Document.content_text, 1, _TRIGRAM_SAMPLE_CHARS)
        for i in range(0, total, _TRIGRAM_REBUILD_BATCH):
            batch = ids[i : i + _TRIGRAM_REBUILD_BATCH]
            with self._get_session() as session:
                rows = session.execute(
                    select(Document.id, Document.file_name, sample.label("sample")).where(
                        Document.id.in_(batch)
                    )
                ).all()
            for row in rows:
                if row.sample:
                    self._update_trigram_index(
                        doc_id=row.id, file_name=row.file_name, content=row.sample, is_new=True
                    )
                    count += 1
            if progress_callback:
                progress_callback(min(i + len(batch), total), total)

        # Final progress update
        if progress_callback:
            progress_callback(total, total)

        return count
