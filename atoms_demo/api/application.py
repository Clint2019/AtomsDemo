from dataclasses import dataclass
from typing import Any
from atoms_demo.workflows.registry import Services
from atoms_demo.workflows.generation import GenerationWorkflow
from atoms_demo.workflows.project_lifecycle import ProjectLifecycle
from atoms_demo.workflows.publication import PublicationWorkflow
from atoms_demo.workflows.dashboard import DashboardQueries
from atoms_demo.workflows.preview import PreviewWorkflow
from atoms_demo.workflows.storage_metering import StorageMeteringWorkflow
from atoms_demo.workflows.profile_avatar import ProfileWorkflow


@dataclass(frozen=True, kw_only=True)
class Application:
    queue: Any
    services: Services
    generation: GenerationWorkflow
    lifecycle: ProjectLifecycle
    publication: PublicationWorkflow
    dashboard: DashboardQueries
    preview: PreviewWorkflow
    metering: StorageMeteringWorkflow
    profile: ProfileWorkflow
