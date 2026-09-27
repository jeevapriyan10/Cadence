"""SQLAlchemy 2.0 declarative domain models for Cadence."""

from datetime import datetime, timezone
from typing import Any, Optional
import uuid

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base declarative class for Cadence domain models."""
    pass


def generate_uuid() -> str:
    """Generate a string UUID."""
    return str(uuid.uuid4())


def utc_now() -> datetime:
    """Return UTC datetime for database timestamp defaults."""
    return datetime.now(timezone.utc)


class TrackSection(Base):
    """Represents a segment of railway track."""

    __tablename__ = "track_sections"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=generate_uuid)
    name: Mapped[str] = mapped_column(String, nullable=False)
    length_meters: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    def __repr__(self) -> str:
        return f"<TrackSection(id='{self.id}', name='{self.name}')>"


class SectionAdjacency(Base):
    """Represents an adjacency edge between two TrackSections in the track graph."""

    __tablename__ = "section_adjacencies"
    __table_args__ = (
        UniqueConstraint("section_a_id", "section_b_id", name="uq_section_adjacency"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=generate_uuid)
    section_a_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("track_sections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    section_b_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("track_sections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    section_a: Mapped["TrackSection"] = relationship(
        "TrackSection",
        foreign_keys=[section_a_id],
    )
    section_b: Mapped["TrackSection"] = relationship(
        "TrackSection",
        foreign_keys=[section_b_id],
    )

    def __repr__(self) -> str:
        return f"<SectionAdjacency({self.section_a_id} <-> {self.section_b_id})>"


class TrainSlot(Base):
    """Represents a scheduled train service path through the railway network."""

    __tablename__ = "train_slots"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=generate_uuid)
    name: Mapped[str] = mapped_column(String, nullable=False)
    scheduled_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    scheduled_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    route: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    def __repr__(self) -> str:
        return f"<TrainSlot(id='{self.id}', name='{self.name}', priority={self.priority})>"


class MaintenanceTask(Base):
    """Represents maintenance work requested on a track section."""

    __tablename__ = "maintenance_tasks"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=generate_uuid)
    name: Mapped[str] = mapped_column(String, nullable=False)
    section_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("track_sections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    earliest_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    latest_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    is_emergency: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    section: Mapped["TrackSection"] = relationship("TrackSection")

    def __repr__(self) -> str:
        return f"<MaintenanceTask(id='{self.id}', name='{self.name}', duration={self.duration_minutes}m)>"


class ScheduledBlock(Base):
    """Represents a finalized, scheduled maintenance possession block."""

    __tablename__ = "scheduled_blocks"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=generate_uuid)
    task_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("maintenance_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    section_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("track_sections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    method: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    task: Mapped["MaintenanceTask"] = relationship("MaintenanceTask")
    section: Mapped["TrackSection"] = relationship("TrackSection")

    def __repr__(self) -> str:
        return f"<ScheduledBlock(id='{self.id}', task_id='{self.task_id}', method='{self.method}')>"


class NetworkRun(Base):
    """Represents a generated railway network instance."""

    __tablename__ = "network_runs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=generate_uuid)
    profile_name: Mapped[str] = mapped_column(String, nullable=False)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    section_count: Mapped[int] = mapped_column(Integer, nullable=False)
    train_count: Mapped[int] = mapped_column(Integer, nullable=False)
    task_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    decisions: Mapped[list["DecisionRecord"]] = relationship(
        "DecisionRecord",
        back_populates="network_run",
        cascade="all, delete-orphan",
        order_by="DecisionRecord.created_at",
    )

    def __repr__(self) -> str:
        return f"<NetworkRun(id='{self.id}', profile='{self.profile_name}', seed={self.seed})>"


class DecisionRecord(Base):
    """Represents a persisted solve or replan decision for a railway network run."""

    __tablename__ = "decision_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    network_run_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("network_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    strategy: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    objective_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    wall_time_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    rl_fallback_triggered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    scheduled_blocks_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    is_replan: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)

    network_run: Mapped["NetworkRun"] = relationship(
        "NetworkRun",
        back_populates="decisions",
    )

    def __repr__(self) -> str:
        return f"<DecisionRecord(id={self.id}, run_id='{self.network_run_id}', strategy='{self.strategy}', is_replan={self.is_replan})>"

