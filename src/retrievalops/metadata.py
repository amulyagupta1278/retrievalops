import hashlib
import hmac
import secrets
from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Integer, String, create_engine, delete, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from retrievalops.contracts import Document, IngestionJob, Sandbox


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


class DeletionAuditRecord(Base):
    __tablename__ = "deletion_audits"

    sandbox_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)


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

    def create_upload(
        self,
        sandbox: Sandbox,
        document: Document,
        job: IngestionJob,
        token: str,
        storage_key: str,
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
                )
            )

    def token_matches(self, sandbox_id: str | UUID, token: str) -> bool:
        with Session(self._engine) as session:
            record = session.get(SandboxRecord, str(sandbox_id))
            return record is not None and _verify_token(token, record.token_hash)

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
