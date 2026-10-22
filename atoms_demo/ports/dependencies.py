from dataclasses import dataclass
from atoms_demo.ports.persistence import UnitOfWorkFactory
from atoms_demo.ports.runtime import (
    AttachmentAccess, Authorization, BlobStore, Clock, CommitFence, EventStore, IdFactory, JobQueue, ProjectWorkspaceLookup, PublicCatalogSource, SecretStore, TaskMessageWriter, UserDirectory,
)
from atoms_demo.ports.providers import (
    BuildExecutor, CaptchaProvider, MailProvider, ModelProvider, OAuthProvider,
    PaymentProvider, DeploymentVerifier,
)
from atoms_demo.contracts.billing import Plan


@dataclass(frozen=True, kw_only=True)
class IdentityDependencies:
    uow: UnitOfWorkFactory
    authorization: Authorization
    clock: Clock
    ids: IdFactory
    secrets: SecretStore
    mail: MailProvider
    captcha: CaptchaProvider
    oauth: OAuthProvider
    public_origin: str


@dataclass(frozen=True, kw_only=True)
class WorkspaceDependencies:
    uow: UnitOfWorkFactory
    authorization: Authorization
    clock: Clock
    ids: IdFactory
    mail: MailProvider
    users: UserDirectory
    public_origin: str


@dataclass(frozen=True, kw_only=True)
class ProfileDependencies:
    uow: UnitOfWorkFactory
    authorization: Authorization
    clock: Clock
    ids: IdFactory
    users: UserDirectory
    blobs: BlobStore


@dataclass(frozen=True, kw_only=True)
class ProjectDependencies:
    uow: UnitOfWorkFactory
    authorization: Authorization
    clock: Clock
    ids: IdFactory


@dataclass(frozen=True, kw_only=True)
class CatalogDependencies:
    uow: UnitOfWorkFactory
    authorization: Authorization
    clock: Clock
    ids: IdFactory
    source: PublicCatalogSource


@dataclass(frozen=True, kw_only=True)
class BillingDependencies:
    uow: UnitOfWorkFactory
    authorization: Authorization
    clock: Clock
    ids: IdFactory
    payments: PaymentProvider
    plans: tuple[Plan, ...] = ()
    initial_build_credits: int = 0


@dataclass(frozen=True, kw_only=True)
class ConnectorDependencies:
    uow: UnitOfWorkFactory
    authorization: Authorization
    clock: Clock
    ids: IdFactory
    oauth: OAuthProvider
    secrets: SecretStore


@dataclass(frozen=True, kw_only=True)
class StorageDependencies:
    uow: UnitOfWorkFactory
    authorization: Authorization
    clock: Clock
    ids: IdFactory
    blobs: BlobStore
    quota_bytes: int = 1_073_741_824


@dataclass(frozen=True, kw_only=True)
class ConversationDependencies:
    uow: UnitOfWorkFactory
    authorization: Authorization
    clock: Clock
    ids: IdFactory
    attachments: AttachmentAccess


@dataclass(frozen=True, kw_only=True)
class TaskDependencies:
    uow: UnitOfWorkFactory
    authorization: Authorization
    clock: Clock
    ids: IdFactory
    events: EventStore
    queue: JobQueue
    commit_fence: CommitFence
    messages: TaskMessageWriter


@dataclass(frozen=True, kw_only=True)
class GenerationDependencies:
    uow: UnitOfWorkFactory
    authorization: Authorization
    clock: Clock
    ids: IdFactory
    model: ModelProvider
    model_name: str = ""
    model_configured: bool = False


@dataclass(frozen=True, kw_only=True)
class BuildDependencies:
    uow: UnitOfWorkFactory
    authorization: Authorization
    clock: Clock
    ids: IdFactory
    builder: BuildExecutor


@dataclass(frozen=True, kw_only=True)
class VersionDependencies:
    uow: UnitOfWorkFactory
    authorization: Authorization
    clock: Clock
    ids: IdFactory
    blobs: BlobStore
    commit_fence: CommitFence


@dataclass(frozen=True, kw_only=True)
class PreviewDependencies:
    uow: UnitOfWorkFactory
    authorization: Authorization
    clock: Clock
    ids: IdFactory
    blobs: BlobStore
    secrets: SecretStore
    source: PublicCatalogSource
    projects: ProjectWorkspaceLookup
    preview_origin: str


@dataclass(frozen=True, kw_only=True)
class AppResourceDependencies:
    uow: UnitOfWorkFactory
    authorization: Authorization
    clock: Clock
    ids: IdFactory
    blobs: BlobStore
    secrets: SecretStore
    projects: ProjectWorkspaceLookup
    source: PublicCatalogSource


@dataclass(frozen=True, kw_only=True)
class PublicationDependencies:
    uow: UnitOfWorkFactory
    authorization: Authorization
    clock: Clock
    ids: IdFactory
    blobs: BlobStore
    verifier: DeploymentVerifier
