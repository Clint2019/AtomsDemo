"""Only composition root. No provider access occurs at import time."""
from atoms_demo.config import Settings
from atoms_demo.contracts.app_resources import AppResource
from atoms_demo.core.types import Environment
from atoms_demo.api.application import Application
from atoms_demo.adapters import (
    attachment_access, authorization, blob_store, build_executor, captcha_provider, catalog_source, commit_fence, deployment_verifier, event_store,
    job_queue, mail_provider, model_provider, oauth_provider, payment_provider, persistence, platform_session_lookup, project_workspace,
    secret_store, storage_pricing, task_messages, usage_pricing, user_directory,
)
from atoms_demo.adapters.runtime import SystemClock, UUIDFactory
from atoms_demo.ports.dependencies import (
    IdentityDependencies, WorkspaceDependencies, ProfileDependencies, ProjectDependencies,
    CatalogDependencies, BillingDependencies, ConnectorDependencies, StorageDependencies,
    ConversationDependencies, TaskDependencies, GenerationDependencies, BuildDependencies,
    VersionDependencies, PreviewDependencies, AppResourceDependencies, PublicationDependencies,
)
from atoms_demo.services.identity import (
    IdentityService, AccountRecord, CurrentEmailChange, VerificationCooldown,
    VerificationRequestRecord, EmailLookup,
    SessionRecord, VerificationRecord, OAuthTransaction, OAuthLookup, decode_session,
    decode_verification, decode_verification_cooldown, decode_verification_request,
    decode_oauth_transaction,
)
from atoms_demo.services.workspaces import (
    WorkspaceService, UserWorkspaceIndex, InvitationRecord, WorkspaceSelection, WorkspaceMutation,
    decode_invitation,
)
from atoms_demo.services.profiles import (
    ProfileService, ProfileRecord, ProfileSlug, PreferencesRecord, ProfileMutation,
    PreferencesMutation, AvatarMutation, decode_profile, decode_profile_mutation,
    decode_preferences_mutation, decode_avatar_mutation,
)
from atoms_demo.contracts.workspaces import Workspace, Membership, Role
from atoms_demo.services.projects import (
    ProjectService, ProjectLocator, ProjectMutation, ProjectFavoriteIndex,
)
from atoms_demo.contracts.projects import Project, Visibility
from atoms_demo.services.catalog import (CatalogService, CatalogRecord, CatalogSaveRecord,
    CatalogMutation, decode_catalog_record)
from atoms_demo.services.billing import (
    BillingService, CheckoutRecord, CheckoutLocator, PaymentEventRecord,
    LedgerRecord, ReservationRecord, InvoiceRecord, SubscriptionRecord, WalletCurrency,
    StorageMeasurement, decode_checkout, decode_payment_event, decode_ledger,
    decode_reservation, decode_invoice, decode_storage_measurement,
)
from atoms_demo.services.connectors import (
    ConnectorService, ConnectionTransaction, decode_connection_transaction, decode_connector,
)
from atoms_demo.contracts.connectors import Connector
from atoms_demo.services.storage import (
    StorageService, StorageObjectRecord, StorageLocator, StorageMutation, StoragePolicy,
    StorageMeteringRecord, decode_storage_object, decode_storage_metering,
)
from atoms_demo.services.conversations import (
    ConversationService, ConversationSummary, MessageMutation, decode_message,
)
from atoms_demo.contracts.conversations import Message
from atoms_demo.services.tasks import TaskService, TaskLocator, TaskMutation, ActiveTask, ModelUsageRecord, decode_task, decode_approval
from atoms_demo.contracts.tasks import Task, TaskApproval
from atoms_demo.services.generation import GenerationService
from atoms_demo.services.builds import BuildService
from atoms_demo.services.versions import (
    VersionService, VersionLocator, VersionBuildRecord, HeadRecord, VersionMutation, PromotionMutation,
    decode_version, decode_build,
)
from atoms_demo.contracts.versions import Version
from atoms_demo.services.preview import (PreviewService, TicketRecord, CatalogTicketMarker, ShareRecord,
    ShareLookup, IssueRecord, decode_ticket, decode_share, decode_issue)
from atoms_demo.services.app_resources import (AppResourceService, ResourceLocator,
    AppUserRecord, AppUserLocator, AppSessionRecord, PublicAppSessionMarker, DataRecord, DataMutation,
    ResourceMutation, ResourceSecretMutation, decode_resource, decode_app_user,
    decode_app_session, decode_resource_mutation, decode_resource_secret_mutation,
    decode_data_mutation)
from atoms_demo.services.publication import (PublicationService, PublicationLocator,
    PublicationIndex, PublicationMutation, decode_publication,
    decode_publication_mutation)
from atoms_demo.contracts.publication import Publication
from atoms_demo.workflows.registry import (
    Services, GenerationServices, LifecycleServices, PublicationServices, DashboardServices, PreviewServices, ProfileServices,
)
from atoms_demo.workflows.generation import GenerationWorkflow
from atoms_demo.workflows.project_lifecycle import ProjectLifecycle
from atoms_demo.workflows.publication import PublicationWorkflow
from atoms_demo.workflows.dashboard import DashboardQueries
from atoms_demo.workflows.preview import PreviewWorkflow
from atoms_demo.workflows.storage_metering import StorageMeteringWorkflow
from atoms_demo.workflows.profile_avatar import ProfileWorkflow


def compose_application(settings: Settings) -> Application:
    uow = persistence.create_adapter(settings)
    uow.register(AccountRecord, "aggregate_identity_accounts", lambda data: AccountRecord(**data))
    uow.register(CurrentEmailChange, "aggregate_identity_current_email_changes",
                 lambda data: CurrentEmailChange(**data))
    uow.register(EmailLookup, "aggregate_identity_emails", lambda data: EmailLookup(**data))
    uow.register(SessionRecord, "aggregate_identity_sessions", decode_session)
    uow.register(VerificationRecord, "aggregate_identity_verifications", decode_verification)
    uow.register(VerificationCooldown, "aggregate_identity_verification_cooldowns",
                 decode_verification_cooldown)
    uow.register(VerificationRequestRecord, "aggregate_identity_verification_requests",
                 decode_verification_request)
    uow.register(OAuthTransaction, "aggregate_identity_oauth_transactions", decode_oauth_transaction)
    uow.register(OAuthLookup, "aggregate_identity_oauth_links", lambda data: OAuthLookup(**data))
    uow.register(Workspace, "aggregate_workspaces", lambda data: Workspace(**data))
    uow.register(Membership, "aggregate_workspace_memberships",
                 lambda data: Membership(**{**data, "role": Role(data["role"])}))
    uow.register(UserWorkspaceIndex, "aggregate_workspace_user_index",
                 lambda data: UserWorkspaceIndex(**{**data, "workspace_ids": tuple(data["workspace_ids"])}))
    uow.register(InvitationRecord, "aggregate_workspace_invitations", decode_invitation)
    uow.register(WorkspaceSelection, "aggregate_workspace_selections", lambda data: WorkspaceSelection(**data))
    uow.register(WorkspaceMutation, "aggregate_workspace_mutations", lambda data: WorkspaceMutation(**data))
    uow.register(ProfileRecord, "aggregate_profiles", decode_profile)
    uow.register(ProfileSlug, "aggregate_profile_slugs", lambda data: ProfileSlug(**data))
    uow.register(PreferencesRecord, "aggregate_preferences", lambda data: PreferencesRecord(**data))
    uow.register(ProfileMutation, "aggregate_profile_mutations", decode_profile_mutation)
    uow.register(PreferencesMutation, "aggregate_preference_mutations", decode_preferences_mutation)
    uow.register(AvatarMutation, "aggregate_avatar_mutations", decode_avatar_mutation)
    uow.register(Project, "aggregate_projects",
                 lambda data: Project(**{**data, "visibility": Visibility(data["visibility"])}))
    uow.register(ProjectLocator, "aggregate_project_locators", lambda data: ProjectLocator(**data))
    uow.register(ProjectMutation, "aggregate_project_mutations", lambda data: ProjectMutation(**data))
    uow.register(ProjectFavoriteIndex, "aggregate_project_favorites",
                 lambda data: ProjectFavoriteIndex(**{**data, "project_ids": tuple(data["project_ids"])}))
    uow.register(Message, "aggregate_conversation_messages", decode_message)
    uow.register(MessageMutation, "aggregate_conversation_mutations", lambda data: MessageMutation(**data))
    uow.register(ConversationSummary, "aggregate_conversation_summaries",
                 lambda data: ConversationSummary(**data))
    uow.register(Version, "aggregate_versions", decode_version)
    uow.register(VersionLocator, "aggregate_version_locators", lambda data: VersionLocator(**data))
    uow.register(VersionBuildRecord, "aggregate_version_builds", decode_build)
    uow.register(HeadRecord, "aggregate_version_heads", lambda data: HeadRecord(**data))
    uow.register(VersionMutation, "aggregate_version_mutations", lambda data: VersionMutation(**data))
    uow.register(PromotionMutation, "aggregate_version_promotions", lambda data: PromotionMutation(**data))
    uow.register(StorageObjectRecord, "aggregate_storage_objects", decode_storage_object)
    uow.register(StorageLocator, "aggregate_storage_locators", lambda data: StorageLocator(**data))
    uow.register(StorageMutation, "aggregate_storage_mutations", lambda data: StorageMutation(**data))
    uow.register(StoragePolicy, "aggregate_storage_policies", lambda data: StoragePolicy(**data))
    uow.register(StorageMeteringRecord, "aggregate_storage_metering", decode_storage_metering)
    uow.register(ConnectionTransaction, "aggregate_connector_oauth_transactions",
                 decode_connection_transaction)
    uow.register(Connector, "aggregate_connectors", decode_connector)
    uow.register(CheckoutRecord, "aggregate_billing_checkouts", decode_checkout)
    uow.register(CheckoutLocator, "aggregate_billing_checkout_locators", lambda data: CheckoutLocator(**data))
    uow.register(PaymentEventRecord, "aggregate_billing_payment_events", decode_payment_event)
    uow.register(LedgerRecord, "aggregate_billing_ledger", decode_ledger)
    uow.register(ReservationRecord, "aggregate_billing_reservations", decode_reservation)
    uow.register(InvoiceRecord, "aggregate_billing_invoices", decode_invoice)
    uow.register(SubscriptionRecord, "aggregate_billing_subscriptions", lambda data: SubscriptionRecord(**data))
    uow.register(WalletCurrency, "aggregate_billing_wallet_currencies", lambda data: WalletCurrency(**data))
    uow.register(StorageMeasurement, "aggregate_billing_storage_measurements", decode_storage_measurement)
    uow.register(Task, "aggregate_tasks", decode_task)
    uow.register(TaskApproval, "aggregate_task_approvals", decode_approval)
    uow.register(TaskLocator, "aggregate_task_locators", lambda data: TaskLocator(**data))
    uow.register(TaskMutation, "aggregate_task_mutations", lambda data: TaskMutation(**data))
    uow.register(ActiveTask, "aggregate_task_active", lambda data: ActiveTask(**data))
    uow.register(ModelUsageRecord, "aggregate_task_model_usage", lambda data: ModelUsageRecord(**data))
    uow.register(TicketRecord, "aggregate_preview_tickets", decode_ticket)
    uow.register(CatalogTicketMarker, "aggregate_preview_catalog_tickets", lambda data: CatalogTicketMarker(**data))
    uow.register(ShareRecord, "aggregate_preview_shares", decode_share)
    uow.register(ShareLookup, "aggregate_preview_share_lookup", lambda data: ShareLookup(**data))
    uow.register(IssueRecord, "aggregate_preview_issues", decode_issue)
    uow.register(AppResource, "aggregate_app_resources", decode_resource)
    uow.register(ResourceLocator, "aggregate_app_resource_locators", lambda data: ResourceLocator(**data))
    uow.register(ResourceMutation, "aggregate_app_resource_mutations", decode_resource_mutation)
    uow.register(ResourceSecretMutation, "aggregate_app_resource_secret_mutations", decode_resource_secret_mutation)
    uow.register(AppUserRecord, "aggregate_app_users", decode_app_user)
    uow.register(AppUserLocator, "aggregate_app_user_locators", lambda data: AppUserLocator(
        **{**data, "environment": Environment(data.get("environment", "development"))}))
    uow.register(AppSessionRecord, "aggregate_app_sessions", decode_app_session)
    uow.register(PublicAppSessionMarker, "aggregate_app_public_sessions", lambda data: PublicAppSessionMarker(**data))
    uow.register(DataRecord, "aggregate_app_data", lambda data: DataRecord(
        **{**data, "environment": Environment(data.get("environment", "development"))}))
    uow.register(DataMutation, "aggregate_app_data_mutations", decode_data_mutation)
    uow.register(CatalogRecord, "aggregate_catalog_entries", decode_catalog_record)
    uow.register(CatalogSaveRecord, "aggregate_catalog_saves", lambda data: CatalogSaveRecord(**data))
    uow.register(CatalogMutation, "aggregate_catalog_mutations", lambda data: CatalogMutation(**data))
    uow.register(Publication, "aggregate_publications", decode_publication)
    uow.register(PublicationLocator, "aggregate_publication_locators", lambda data: PublicationLocator(**data))
    uow.register(PublicationIndex, "aggregate_publication_indexes", lambda data: PublicationIndex(**data))
    uow.register(PublicationMutation, "aggregate_publication_mutations", decode_publication_mutation)
    blobs = blob_store.create_adapter(settings)
    secrets = secret_store.create_adapter(settings)
    events = event_store.create_adapter(settings)
    queue = job_queue.create_adapter(settings)
    auth = authorization.create_adapter(settings)
    users = user_directory.create_adapter(settings)
    mail = mail_provider.create_adapter(settings)
    captcha = captcha_provider.create_adapter(settings)
    oauth = oauth_provider.create_adapter(settings)
    clock = SystemClock()
    ids = UUIDFactory()
    common = dict(uow=uow, authorization=auth, clock=clock, ids=ids)

    selected_model = model_provider.create_adapter(settings, blobs)
    fence = commit_fence.create_adapter()
    services = Services(
        identity=IdentityService(IdentityDependencies(**common, secrets=secrets, mail=mail,
                                                     captcha=captcha, oauth=oauth,
                                                     public_origin=settings.public_origin)),
        workspaces=WorkspaceService(WorkspaceDependencies(**common, mail=mail,
                                                         users=users, public_origin=settings.public_origin)),
        profiles=ProfileService(ProfileDependencies(**common, users=users, blobs=blobs)),
        projects=ProjectService(ProjectDependencies(**common)),
        catalog=CatalogService(CatalogDependencies(**common,
            source=catalog_source.create_adapter(settings))),
        billing=BillingService(BillingDependencies(**common, payments=payment_provider.create_adapter(settings),
            initial_build_credits=settings.initial_build_credits)),
        connectors=ConnectorService(ConnectorDependencies(**common, oauth=oauth, secrets=secrets)),
        storage=StorageService(StorageDependencies(**common, blobs=blobs,
                                                   quota_bytes=settings.storage_quota_bytes)),
        conversations=ConversationService(ConversationDependencies(
            **common, attachments=attachment_access.create_adapter(settings))),
        tasks=TaskService(TaskDependencies(**common, events=events, queue=queue,
                                           commit_fence=fence,
                                           messages=task_messages.RepositoryTaskMessageWriter())),
        generation=GenerationService(GenerationDependencies(**common,
            model=selected_model, model_name=settings.model_name,
            model_configured=isinstance(selected_model, model_provider.BailianTokenPlanProvider))),
        builds=BuildService(BuildDependencies(**common,
            builder=build_executor.create_adapter(settings, blobs))),
        versions=VersionService(VersionDependencies(**common, blobs=blobs,
            commit_fence=fence)),
        preview=PreviewService(PreviewDependencies(**common, blobs=blobs, secrets=secrets,
            source=catalog_source.create_adapter(settings),
            projects=project_workspace.create_adapter(settings),
            preview_origin=settings.preview_origin)),
        app_resources=AppResourceService(AppResourceDependencies(**common, blobs=blobs,
            secrets=secrets, projects=project_workspace.create_adapter(settings),
            source=catalog_source.create_adapter(settings),
            platform_sessions=platform_session_lookup.create_adapter(settings))),
        publication=PublicationService(PublicationDependencies(**common, blobs=blobs,
            verifier=deployment_verifier.create_adapter(settings))),
    )
    return Application(
        queue=queue,
        services=services,
        generation=GenerationWorkflow(GenerationServices(
            projects=services.projects, conversations=services.conversations, tasks=services.tasks,
            generation=services.generation, builds=services.builds, versions=services.versions,
            storage=services.storage, billing=services.billing),
            usage_pricing.create_adapter(settings), settings.max_repairs, clock),
        lifecycle=ProjectLifecycle(LifecycleServices(
            projects=services.projects, catalog=services.catalog, versions=services.versions)),
        publication=PublicationWorkflow(PublicationServices(
            versions=services.versions, builds=services.builds,
            app_resources=services.app_resources, publication=services.publication)),
        dashboard=DashboardQueries(DashboardServices(
            projects=services.projects, versions=services.versions, billing=services.billing)),
        preview=PreviewWorkflow(PreviewServices(
            versions=services.versions, catalog=services.catalog, preview=services.preview,
            app_resources=services.app_resources), clock),
        metering=StorageMeteringWorkflow(services.storage, services.billing,
                                        storage_pricing.create_adapter(settings)),
        profile=ProfileWorkflow(ProfileServices(profiles=services.profiles,
                                                storage=services.storage)),
    )
