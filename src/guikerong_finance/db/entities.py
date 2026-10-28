"""SQLModel mappings compatible with existing SQLite files and event payloads."""

from sqlalchemy import CheckConstraint, Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


class Event(SQLModel, table=True):
    __tablename__ = "events"
    stream: str = Field(primary_key=True)
    version: int = Field(primary_key=True)
    kind: str
    payload: str = Field(sa_column=Column(Text, nullable=False))
    created_at: str


class CatalogRevision(SQLModel, table=True):
    __tablename__ = "catalog_revisions"
    id: int | None = Field(default=None, primary_key=True)
    content_hash: str = Field(unique=True)
    catalog_version: str
    source_name: str
    raw_record_count: int
    warnings_json: str = Field(sa_column=Column(Text, nullable=False))
    product_count: int
    created_at: str


class CatalogProduct(SQLModel, table=True):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("revision_id", "position"),)
    revision_id: int = Field(primary_key=True, foreign_key="catalog_revisions.id")
    product_id: str = Field(primary_key=True)
    position: int
    product_name: str
    institution: str
    application_conditions: str = Field(sa_column=Column(Text, nullable=False))
    metadata_json: str = Field(sa_column=Column(Text, nullable=False))


class CatalogActive(SQLModel, table=True):
    __tablename__ = "catalog_active"
    __table_args__ = (CheckConstraint("singleton=1"),)
    singleton: int = Field(default=1, primary_key=True)
    revision_id: int = Field(foreign_key="catalog_revisions.id")
