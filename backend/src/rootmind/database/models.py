"""SQLAlchemy database models."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class for all models."""

    pass


class Document(Base):
    """Indexed document with full text content."""

    __tablename__ = "documents"

    # Primary key - hash of connector_name + file_path
    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    # Source & Path
    connector_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_type: Mapped[str] = mapped_column(String(50), nullable=False)

    # Metadata
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    modified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Indexer-generated
    indexed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=func.now(), onupdate=func.now()
    )
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Full text content (can be very large)
    content_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Character count for quick size check
    content_length: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Status
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Indexes
    __table_args__ = (
        Index("idx_documents_connector", "connector_name"),
        Index("idx_documents_modified", "modified_at"),
        Index("idx_documents_status", "status"),
        Index("idx_documents_file_type", "file_type"),
        Index("idx_documents_file_name", "file_name"),
        # Speeds up "recently indexed" queries (ORDER BY indexed_at DESC).
        Index("idx_documents_indexed_at", "indexed_at"),
    )

    def to_dict(self, include_content: bool = False) -> dict[str, Any]:
        """Convert to dictionary for API responses."""
        result = {
            "id": self.id,
            "connector_name": self.connector_name,
            "file_path": self.file_path,
            "file_name": self.file_name,
            "file_type": self.file_type,
            "size_bytes": self.size_bytes,
            "page_count": self.page_count,
            "content_length": self.content_length,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "modified_at": self.modified_at.isoformat() if self.modified_at else None,
            "indexed_at": self.indexed_at.isoformat() if self.indexed_at else None,
            "status": self.status,
            "error_message": self.error_message,
        }
        if include_content:
            result["content_text"] = self.content_text
        return result

    def __repr__(self) -> str:
        return f"<Document {self.file_name} ({self.status})>"


class SyncStatus(Base):
    """Track sync status and timestamps."""

    __tablename__ = "sync_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    connector_name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_full_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    documents_total: Mapped[int] = mapped_column(Integer, default=0)
    documents_indexed: Mapped[int] = mapped_column(Integer, default=0)
    documents_error: Mapped[int] = mapped_column(Integer, default=0)
    is_syncing: Mapped[bool] = mapped_column(Boolean, default=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "connector_name": self.connector_name,
            "last_sync_at": self.last_sync_at.isoformat() if self.last_sync_at else None,
            "last_full_sync_at": self.last_full_sync_at.isoformat()
            if self.last_full_sync_at
            else None,
            "documents_total": self.documents_total,
            "documents_indexed": self.documents_indexed,
            "documents_error": self.documents_error,
            "is_syncing": self.is_syncing,
            "error_message": self.error_message,
        }
