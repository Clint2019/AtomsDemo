from dataclasses import dataclass
from atoms_demo.core.types import EntityId, RequestContext
from atoms_demo.contracts.projects import Project
from atoms_demo.contracts.versions import ProjectHead
from atoms_demo.contracts.billing import Balance
from atoms_demo.workflows.registry import DashboardServices


@dataclass(frozen=True, kw_only=True)
class ProjectOverview:
    project: Project
    head: ProjectHead
    balances: tuple[Balance, ...]


class DashboardQueries:
    def __init__(self, services: DashboardServices) -> None:
        self.services = services

    async def project(self, ctx: RequestContext, project_id: EntityId) -> ProjectOverview:
        project = await self.services.projects.get(ctx, project_id)
        head = await self.services.versions.head(ctx, project_id)
        balances = await self.services.billing.balances(ctx)
        return ProjectOverview(project=project, head=head, balances=tuple(balances))
