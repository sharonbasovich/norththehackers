"""Durable system of record for the atlas, research campaigns, evidence, and publication.

Status dimensions are kept independent (see docs/plan.md §4.3): execution, scheduling, evidence,
review, novelty, formalization, problem status, and publication never collapse into one field.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from .db import Base


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware datetimes on every backend (SQLite drops tzinfo on read)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    def process_result_value(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


class Collaborator(Base):
    __tablename__ = "collaborators"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20))  # owner | collaborator
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Area(Base):
    __tablename__ = "areas"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    slug: Mapped[str] = mapped_column(String(120), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("areas.id"), nullable=True)
    depth: Mapped[int] = mapped_column(Integer, default=0)
    external_ids: Mapped[dict] = mapped_column(JSON, default=dict)  # e.g. {"msc2020": ["11P32"]}
    children: Mapped[list[Area]] = relationship(back_populates="parent")
    parent: Mapped[Area | None] = relationship(back_populates="children", remote_side=[id])


class ProblemArea(Base):
    __tablename__ = "problem_areas"
    problem_id: Mapped[str] = mapped_column(ForeignKey("problems.id"), primary_key=True)
    area_id: Mapped[str] = mapped_column(ForeignKey("areas.id"), primary_key=True)
    primary: Mapped[bool] = mapped_column(Boolean, default=False)


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    url: Mapped[str] = mapped_column(String(2000))
    title: Mapped[str] = mapped_column(String(500))
    authors: Mapped[str] = mapped_column(String(500), default="")
    publisher: Mapped[str] = mapped_column(String(300), default="")
    published_date: Mapped[str] = mapped_column(String(40), default="")
    retrieved_date: Mapped[str] = mapped_column(String(40), default="")
    reuse_policy: Mapped[str] = mapped_column(String(300), default="cite-only")
    assertions: Mapped[list[SourceAssertion]] = relationship(back_populates="source")


class SourceAssertion(Base):
    __tablename__ = "source_assertions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"))
    problem_id: Mapped[str] = mapped_column(ForeignKey("problems.id"))
    location: Mapped[str] = mapped_column(String(500), default="")  # section, entry, page
    asserted_status: Mapped[str] = mapped_column(String(40))  # open | resolved | disputed | unknown
    asserted_at: Mapped[str] = mapped_column(String(40), default="")
    review_state: Mapped[str] = mapped_column(String(30), default="unreviewed")
    notes: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[Source] = relationship(back_populates="assertions")
    problem: Mapped[Problem] = relationship(back_populates="assertions")


class Problem(Base):
    __tablename__ = "problems"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    slug: Mapped[str] = mapped_column(String(160), unique=True)
    title: Mapped[str] = mapped_column(String(300))
    statement: Mapped[str] = mapped_column(Text)
    definitions: Mapped[str] = mapped_column(Text, default="")
    assumptions: Mapped[str] = mapped_column(Text, default="")
    origin: Mapped[str] = mapped_column(String(30), default="literature")  # literature | generated
    attribution: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(40), default="unreviewed")
    status_checked_at: Mapped[str] = mapped_column(String(40), default="")
    formal_target: Mapped[str] = mapped_column(Text, default="")  # approved Lean statement
    formal_target_status: Mapped[str] = mapped_column(String(30), default="absent")
    coverage: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    areas: Mapped[list[Area]] = relationship(secondary="problem_areas")
    assertions: Mapped[list[SourceAssertion]] = relationship(back_populates="problem")
    campaigns: Mapped[list[Campaign]] = relationship(back_populates="problem")


class Portfolio(Base):
    __tablename__ = "portfolios"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    max_concurrent_sessions: Mapped[int] = mapped_column(Integer, default=2)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    policy: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Campaign(Base):
    __tablename__ = "campaigns"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    portfolio_id: Mapped[str] = mapped_column(ForeignKey("portfolios.id"))
    problem_id: Mapped[str] = mapped_column(ForeignKey("problems.id"))
    state: Mapped[str] = mapped_column(
        String(20), default="active"
    )  # active|paused|completed|failed
    generation: Mapped[int] = mapped_column(Integer, default=0)
    research_outcome: Mapped[str | None] = mapped_column(
        String(40), nullable=True, default="researching"
    )
    session_budget: Mapped[int] = mapped_column(Integer, default=6)
    sessions_used: Mapped[int] = mapped_column(Integer, default=0)
    policy_version: Mapped[str] = mapped_column(String(40), default="v1")
    # default_mode: Devin mode used when an assignment does not specify one
    policy: Mapped[dict] = mapped_column(JSON, default=dict)
    seed: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    problem: Mapped[Problem] = relationship(back_populates="campaigns")
    ideas: Mapped[list[Idea]] = relationship(back_populates="campaign")
    attempts: Mapped[list[Attempt]] = relationship(back_populates="campaign")


class IdeaParent(Base):
    __tablename__ = "idea_parents"
    child_id: Mapped[str] = mapped_column(ForeignKey("ideas.id"), primary_key=True)
    parent_id: Mapped[str] = mapped_column(ForeignKey("ideas.id"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(30), default="refined_from")


class Idea(Base):
    __tablename__ = "ideas"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"))
    title: Mapped[str] = mapped_column(String(300))
    approach: Mapped[str] = mapped_column(Text)
    mechanism: Mapped[str] = mapped_column(Text, default="")
    next_experiment: Mapped[str] = mapped_column(Text, default="")
    novelty_rationale: Mapped[str] = mapped_column(Text, default="")
    method_tags: Mapped[list] = mapped_column(JSON, default=list)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    depth: Mapped[int] = mapped_column(Integer, default=0)
    scheduling_status: Mapped[str] = mapped_column(String(20), default="active")
    evidence_status: Mapped[str] = mapped_column(String(40), default="untested")
    review_status: Mapped[str] = mapped_column(String(30), default="unreviewed")
    novelty_status: Mapped[str] = mapped_column(String(30), default="unchecked")
    formalization_status: Mapped[str] = mapped_column(String(30), default="absent")
    score: Mapped[float] = mapped_column(Float, default=0.0)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    produced_by_attempt_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    campaign: Mapped[Campaign] = relationship(back_populates="ideas")
    parents: Mapped[list[Idea]] = relationship(
        secondary="idea_parents",
        primaryjoin="Idea.id==IdeaParent.child_id",
        secondaryjoin="Idea.id==IdeaParent.parent_id",
    )
    claims: Mapped[list[Claim]] = relationship(back_populates="idea")
    evidence: Mapped[list[Evidence]] = relationship(back_populates="idea")


class Claim(Base):
    __tablename__ = "claims"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"))
    idea_id: Mapped[str | None] = mapped_column(ForeignKey("ideas.id"), nullable=True)
    statement: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(Text, default="")
    lean_declaration: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    content_hash: Mapped[str] = mapped_column(String(64))
    previous_version_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    formalization_status: Mapped[str] = mapped_column(String(30), default="absent")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    idea: Mapped[Idea | None] = relationship(back_populates="claims")
    evidence: Mapped[list[Evidence]] = relationship(back_populates="claim")


class Attempt(Base):
    __tablename__ = "attempts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"))
    idea_id: Mapped[str | None] = mapped_column(ForeignKey("ideas.id"), nullable=True)
    role: Mapped[str] = mapped_column(String(40))  # hypothesis_generator | experimenter | ...
    requested_mode: Mapped[str] = mapped_column(String(20))
    reported_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    model_metadata: Mapped[dict] = mapped_column(JSON, default=dict)  # unknown unless provider says
    provider: Mapped[str] = mapped_column(String(20), default="mock")
    provider_session_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    provider_session_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    prompt_hash: Mapped[str] = mapped_column(String(64), default="")
    prompt: Mapped[str] = mapped_column(Text, default="")  # private
    worker_token_hash: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(20), default="queued")
    status_detail: Mapped[str] = mapped_column(String(60), default="")
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    retries: Mapped[int] = mapped_column(Integer, default=0)
    usage: Mapped[dict] = mapped_column(JSON, default=dict)  # acus_consumed etc., as reported
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result_ingested: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str] = mapped_column(Text, default="")
    comparison_group: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    campaign: Mapped[Campaign] = relationship(back_populates="attempts")


class Artifact(Base):
    __tablename__ = "artifacts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    filename: Mapped[str] = mapped_column(String(300))
    media_type: Mapped[str] = mapped_column(String(100), default="text/plain")
    storage_uri: Mapped[str] = mapped_column(String(1000))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    producer_attempt_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    visibility: Mapped[str] = mapped_column(String(20), default="private")  # private|public
    manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Evidence(Base):
    __tablename__ = "evidence"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    idea_id: Mapped[str | None] = mapped_column(ForeignKey("ideas.id"), nullable=True)
    claim_id: Mapped[str | None] = mapped_column(ForeignKey("claims.id"), nullable=True)
    # problem-level evidence (status/literature checks) attaches here instead of to an idea
    problem_id: Mapped[str | None] = mapped_column(ForeignKey("problems.id"), nullable=True)
    claim_version: Mapped[int] = mapped_column(Integer, default=0)
    check_type: Mapped[str] = mapped_column(String(40))
    result: Mapped[str] = mapped_column(
        String(30)
    )  # supports|refutes|inconclusive|verified|rejected
    summary: Mapped[str] = mapped_column(Text, default="")
    coverage: Mapped[str] = mapped_column(Text, default="")
    verifier: Mapped[str] = mapped_column(String(80), default="")  # worker-submitted vs lab checker
    verifier_version: Mapped[str] = mapped_column(String(120), default="")
    certified: Mapped[bool] = mapped_column(Boolean, default=False)  # only lab checkers certify
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"), nullable=True)
    produced_by_attempt_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    idea: Mapped[Idea | None] = relationship(back_populates="evidence")
    claim: Mapped[Claim | None] = relationship(back_populates="evidence")


class SelectionDecision(Base):
    __tablename__ = "selection_decisions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"))
    generation: Mapped[int] = mapped_column(Integer)
    idea_id: Mapped[str] = mapped_column(ForeignKey("ideas.id"))
    decision: Mapped[str] = mapped_column(String(20))  # promoted|archived|kept|revived
    reason: Mapped[str] = mapped_column(Text, default="")
    score_components: Mapped[dict] = mapped_column(JSON, default=dict)
    cluster: Mapped[str] = mapped_column(String(120), default="")
    policy_version: Mapped[str] = mapped_column(String(40), default="v1")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Relation(Base):
    __tablename__ = "relations"
    __table_args__ = (
        UniqueConstraint("kind", "source_type", "source_id", "target_type", "target_id"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    layer: Mapped[str] = mapped_column(String(20))  # atlas|lineage|dependency|association
    kind: Mapped[str] = mapped_column(String(40))
    source_type: Mapped[str] = mapped_column(String(20))
    source_id: Mapped[str] = mapped_column(String(32))
    target_type: Mapped[str] = mapped_column(String(20))
    target_id: Mapped[str] = mapped_column(String(32))
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="proposed")  # proposed|checked
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Event(Base):
    """Transactional outbox. Every committed change that matters to the UI or publication
    projector is written here in the same transaction and replayed/published afterwards."""

    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(60), index=True)
    record_type: Mapped[str] = mapped_column(String(30), default="")
    record_id: Mapped[str] = mapped_column(String(32), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    visibility: Mapped[str] = mapped_column(String(20), default="private")
    processed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Publication(Base):
    __tablename__ = "publications"
    __table_args__ = (UniqueConstraint("record_type", "record_id", "record_version"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    record_type: Mapped[str] = mapped_column(String(30), index=True)
    record_id: Mapped[str] = mapped_column(String(32), index=True)
    record_version: Mapped[str] = mapped_column(String(64))
    public_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    evidence_label: Mapped[str] = mapped_column(String(120), default="")
    policy_version: Mapped[str] = mapped_column(String(40), default="v1")
    event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    published_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    withdrawn_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    withdrawal_reason: Mapped[str] = mapped_column(Text, default="")
