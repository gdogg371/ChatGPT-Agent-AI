from __future__ import annotations
from typing import Optional, Literal
from datetime import datetime

from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import Integer, Text, String, BLOB, TIMESTAMP, func, Index, UniqueConstraint, ForeignKey

# Use the same DeclarativeBase pattern as other models; if you already
# have a shared Base, import and reuse it instead of redefining.
class Base(DeclarativeBase):
    pass

EmbeddingFormat = Literal["array", "blob"]  # how you serialize client-side (informational)

class IntrospectionEmbedding(Base):
    """
    Stores dense vector embeddings for rows in introspection_index.
    One row per (item_id, model). Embedding stored as BLOB (e.g., float32 array).
    """
    __tablename__ = "introspection_index_embeddings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("introspection_index.id", ondelete="CASCADE"),
        nullable=False,
    )
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    dim: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[bytes] = mapped_column(BLOB, nullable=False)  # packed float32
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now(), nullable=False)

    # optional metadata
    note: Mapped[Optional[str]] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("item_id", "model", name="uq_ix_embed_item_model"),
        Index("idx_ix_embed_model", "model"),
        Index("idx_ix_embed_dim", "dim"),
    )
