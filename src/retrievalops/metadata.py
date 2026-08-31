import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    create_engine,
    delete,
    func,
    inspect,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from retrievalops.contracts import (
    Document,
    IngestionJob,
    JobState,
    Judgment,
    Sandbox,
    SupportedMediaType,
)


class Base(DeclarativeBase):
    pass


class SandboxRecord(Base):
    __tablename__ = "sandboxes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(97), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DocumentRecord(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    sandbox_id: Mapped[str] = mapped_column(ForeignKey("sandboxes.id"), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(128), nullable=False)


class JobRecord(Base):
    __tablename__ = "ingestion_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    sandbox_id: Mapped[str] = mapped_column(ForeignKey("sandboxes.id"), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    traceparent: Mapped[str | None] = mapped_column(String(55))


class JudgmentRecord(Base):
    __tablename__ = "judgments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    sandbox_id: Mapped[str] = mapped_column(ForeignKey("sandboxes.id"), nullable=False)
    query: Mapped[str] = mapped_column(String(1000), nullable=False)
    relevant_chunk_id: Mapped[str] = mapped_column(String(128), nullable=False)
    relevance: Mapped[int] = mapped_column(Integer, nullable=False)
    reviewed: Mapped[int] = mapped_column(Integer, nullable=False)


class DeletionAuditRecord(Base):
    __tablename__ = "deletion_audits"

    sandbox_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)


@dataclass(frozen=True, slots=True)
class ClaimedIngestion:
    job: IngestionJob
    document: Document
    storage_key: str
    traceparent: str | None


def create_capability_token() -> str:
    return secrets.token_urlsafe(32)


def _hash_token(token: str, salt: bytes | None = None) -> str:
    token_salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(token.encode("utf-8"), salt=token_salt, n=2**14, r=8, p=1, dklen=32)
    return f"{token_salt.hex()}${digest.hex()}"


def _verify_token(token: str, encoded: str) -> bool:
    salt_hex, expected_hex = encoded.split("$", maxsplit=1)
    actual = _hash_token(token, bytes.fromhex(salt_hex)).split("$", maxsplit=1)[1]
    return hmac.compare_digest(actual, expected_hex)


class MetadataStore:
    def __init__(self, database_url: str) -> None:
        connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
        self._engine = create_engine(database_url, connect_args=connect_args)

    def initialize(self) -> None:
        Base.metadata.create_all(self._engine)
        columns = {column["name"] for column in inspect(self._engine).get_columns("ingestion_jobs")}
        if "traceparent" not in columns:
            with self._engine.begin() as connection:
                connection.exec_driver_sql(
                    "ALTER TABLE ingestion_jobs ADD COLUMN traceparent VARCHAR(55)"
                )

    def create_upload(
        self,
        sandbox: Sandbox,
        document: Document,
        job: IngestionJob,
        token: str,
        storage_key: str,
        traceparent: str | None = None,
    ) -> None:
        with Session(self._engine) as session, session.begin():
            session.add(
                SandboxRecord(
                    id=str(sandbox.id),
                    token_hash=_hash_token(token),
                    created_at=sandbox.created_at,
                    expires_at=sandbox.expires_at,
                )
            )
            session.add(
                DocumentRecord(
                    id=str(document.id),
                    sandbox_id=str(document.sandbox_id),
                    filename=document.filename,
                    media_type=document.media_type,
                    size_bytes=document.size_bytes,
                    sha256=document.sha256,
                    storage_key=storage_key,
                )
            )
            session.add(
                JobRecord(
                    id=str(job.id),
                    sandbox_id=str(job.sandbox_id),
                    state=job.state,
                    error_code=job.error_code,
                    traceparent=traceparent,
                )
            )

    def token_matches(self, sandbox_id: str | UUID, token: str) -> bool:
        with Session(self._engine) as session:
            record = session.get(SandboxRecord, str(sandbox_id))
            return record is not None and _verify_token(token, record.token_hash)

    def claim_next_ingestion(self) -> ClaimedIngestion | None:
        """Atomically claim one durable queued job.

        PostgreSQL translates this to row locking with SKIP LOCKED. SQLite ignores the
        locking clause, which is sufficient for the single-worker local profile.
        """
        with Session(self._engine) as session, session.begin():
            job_record = session.scalar(
                select(JobRecord)
                .where(JobRecord.state == JobState.queued)
                .order_by(JobRecord.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if job_record is None:
                return None
            job_record.state = JobState.validating
            document_record = session.scalar(
                select(DocumentRecord).where(DocumentRecord.sandbox_id == job_record.sandbox_id)
            )
            if document_record is None:
                job_record.state = JobState.failed
                job_record.error_code = "DOCUMENT_METADATA_MISSING"
                return None
            return ClaimedIngestion(
                job=IngestionJob(
                    id=UUID(job_record.id),
                    sandbox_id=UUID(job_record.sandbox_id),
                    state=JobState.validating,
                ),
                document=Document(
                    id=UUID(document_record.id),
                    sandbox_id=UUID(document_record.sandbox_id),
                    filename=document_record.filename,
                    media_type=cast(SupportedMediaType, document_record.media_type),
                    size_bytes=document_record.size_bytes,
                    sha256=document_record.sha256,
                ),
                storage_key=document_record.storage_key,
                traceparent=job_record.traceparent,
            )

    def transition_job(
        self, job_id: UUID, expected: JobState, target: JobState, error_code: str | None = None
    ) -> None:
        IngestionJob(id=job_id, sandbox_id=UUID(int=0), state=expected).transition_to(target)
        with Session(self._engine) as session, session.begin():
            record = session.get(JobRecord, str(job_id))
            if record is None or record.state != expected:
                raise RuntimeError(f"job is not in expected state {expected}")
            record.state = target
            record.error_code = error_code

    def job_for_sandbox(self, job_id: UUID, sandbox_id: UUID) -> IngestionJob | None:
        with Session(self._engine) as session:
            record = session.get(JobRecord, str(job_id))
            if record is None or record.sandbox_id != str(sandbox_id):
                return None
            return IngestionJob(
                id=UUID(record.id),
                sandbox_id=UUID(record.sandbox_id),
                state=JobState(record.state),
                error_code=record.error_code,
            )

    def get_job(self, job_id: UUID) -> IngestionJob | None:
        with Session(self._engine) as session:
            record = session.get(JobRecord, str(job_id))
            if record is None:
                return None
            return IngestionJob(
                id=UUID(record.id),
                sandbox_id=UUID(record.sandbox_id),
                state=JobState(record.state),
                error_code=record.error_code,
            )

    def sandbox_state(self, sandbox_id: UUID) -> JobState | None:
        with Session(self._engine) as session:
            state = session.scalar(
                select(JobRecord.state).where(JobRecord.sandbox_id == str(sandbox_id))
            )
            return JobState(state) if state is not None else None

    def contains_token(self, token: str) -> bool:
        with Session(self._engine) as session:
            hashes = session.scalars(select(SandboxRecord.token_hash)).all()
            return any(token in value for value in hashes)

    def expired_sandbox_ids(self, now: datetime) -> list[UUID]:
        with Session(self._engine) as session:
            identifiers = session.scalars(
                select(SandboxRecord.id).where(SandboxRecord.expires_at <= now)
            ).all()
            return [UUID(identifier) for identifier in identifiers]

    def delete_sandbox(self, sandbox_id: UUID, *, deleted_at: datetime, reason: str) -> bool:
        identifier = str(sandbox_id)
        with Session(self._engine) as session, session.begin():
            sandbox = session.get(SandboxRecord, identifier)
            if sandbox is None:
                return False
            session.execute(delete(JobRecord).where(JobRecord.sandbox_id == identifier))
            session.execute(delete(JudgmentRecord).where(JudgmentRecord.sandbox_id == identifier))
            session.execute(delete(DocumentRecord).where(DocumentRecord.sandbox_id == identifier))
            session.delete(sandbox)
            if session.get(DeletionAuditRecord, identifier) is None:
                session.add(
                    DeletionAuditRecord(
                        sandbox_id=identifier,
                        deleted_at=deleted_at,
                        reason=reason,
                    )
                )
            return True

    def deletion_audit_count(self, sandbox_id: str | UUID) -> int:
        with Session(self._engine) as session:
            count = session.scalar(
                select(func.count())
                .select_from(DeletionAuditRecord)
                .where(DeletionAuditRecord.sandbox_id == str(sandbox_id))
            )
            return int(count or 0)

    def replace_judgments(self, sandbox_id: UUID, judgments: list[Judgment]) -> None:
        with Session(self._engine) as session, session.begin():
            session.execute(
                delete(JudgmentRecord).where(JudgmentRecord.sandbox_id == str(sandbox_id))
            )
            session.add_all(
                JudgmentRecord(
                    id=str(judgment.id),
                    sandbox_id=str(sandbox_id),
                    query=judgment.query,
                    relevant_chunk_id=judgment.relevant_chunk_id,
                    relevance=judgment.relevance,
                    reviewed=int(judgment.reviewed),
                )
                for judgment in judgments
            )

    def judgments(self, sandbox_id: UUID, *, reviewed_only: bool = False) -> list[Judgment]:
        with Session(self._engine) as session:
            statement = (
                select(JudgmentRecord)
                .where(JudgmentRecord.sandbox_id == str(sandbox_id))
                .order_by(JudgmentRecord.id)
            )
            if reviewed_only:
                statement = statement.where(JudgmentRecord.reviewed == 1)
            records = session.scalars(statement).all()
            return [
                Judgment(
                    id=UUID(record.id),
                    sandbox_id=UUID(record.sandbox_id),
                    query=record.query,
                    relevant_chunk_id=record.relevant_chunk_id,
                    relevance=record.relevance,
                    reviewed=bool(record.reviewed),
                )
                for record in records
            ]
