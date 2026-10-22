from dataclasses import dataclass
from atoms_demo.contracts.identity import IdentityAPI
from atoms_demo.contracts.workspaces import WorkspaceAPI
from atoms_demo.contracts.profiles import ProfileAPI
from atoms_demo.contracts.projects import ProjectAPI
from atoms_demo.contracts.catalog import CatalogAPI
from atoms_demo.contracts.billing import BillingAPI
from atoms_demo.contracts.connectors import ConnectorAPI
from atoms_demo.contracts.storage import StorageAPI
from atoms_demo.contracts.conversations import ConversationAPI
from atoms_demo.contracts.tasks import TaskAPI
from atoms_demo.contracts.generation import GenerationAPI
from atoms_demo.contracts.builds import BuildAPI
from atoms_demo.contracts.versions import VersionAPI
from atoms_demo.contracts.preview import PreviewAPI
from atoms_demo.contracts.app_resources import AppResourceAPI
from atoms_demo.contracts.publication import PublicationAPI


@dataclass(frozen=True, kw_only=True)
class Services:
    identity: IdentityAPI
    workspaces: WorkspaceAPI
    profiles: ProfileAPI
    projects: ProjectAPI
    catalog: CatalogAPI
    billing: BillingAPI
    connectors: ConnectorAPI
    storage: StorageAPI
    conversations: ConversationAPI
    tasks: TaskAPI
    generation: GenerationAPI
    builds: BuildAPI
    versions: VersionAPI
    preview: PreviewAPI
    app_resources: AppResourceAPI
    publication: PublicationAPI


@dataclass(frozen=True, kw_only=True)
class GenerationServices:
    projects: ProjectAPI
    conversations: ConversationAPI
    tasks: TaskAPI
    generation: GenerationAPI
    builds: BuildAPI
    versions: VersionAPI
    storage: StorageAPI
    billing: BillingAPI


@dataclass(frozen=True, kw_only=True)
class LifecycleServices:
    projects: ProjectAPI
    catalog: CatalogAPI
    versions: VersionAPI


@dataclass(frozen=True, kw_only=True)
class PublicationServices:
    versions: VersionAPI
    builds: BuildAPI
    app_resources: AppResourceAPI
    publication: PublicationAPI


@dataclass(frozen=True, kw_only=True)
class DashboardServices:
    projects: ProjectAPI
    versions: VersionAPI
    billing: BillingAPI


@dataclass(frozen=True, kw_only=True)
class PreviewServices:
    versions: VersionAPI
    catalog: CatalogAPI
    preview: PreviewAPI
    app_resources: AppResourceAPI


@dataclass(frozen=True, kw_only=True)
class ProfileServices:
    profiles: ProfileAPI
    storage: StorageAPI
