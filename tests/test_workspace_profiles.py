"""T02 persistence, role, invitation and profile isolation checks."""
import asyncio
from io import BytesIO
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from cryptography.fernet import Fernet
from PIL import Image

from atoms_demo.bootstrap import compose_application
from atoms_demo.config import Settings
from atoms_demo.contracts.identity import Credentials, VerificationRequest
from atoms_demo.contracts.profiles import Profile
from atoms_demo.contracts.workspaces import Role
from atoms_demo.contracts.projects import CreateProject, ProjectQuery, Visibility
from atoms_demo.contracts.storage import Upload
from atoms_demo.core.errors import DomainError, ErrorCode
from atoms_demo.core.types import EntityId, Mutation, PageQuery, RequestContext, Scope


class CaptchaStub:
    async def verify(self, token, request_id):
        return token == "ok"


class MailStub:
    def __init__(self):
        self.urls = []

    async def send_verification(self, address, url, key):
        self.urls.append((address, url))
        return "stub"

    async def send_invitation(self, address, url, key):
        self.urls.append((address, url))
        return "stub"


class WorkspaceProfileTests(unittest.TestCase):
    def test_cross_user_invitation_role_and_preferences_survive_restart(self):
        async def run():
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                settings = Settings(database_url=f"sqlite:///{root / 'db.sqlite'}", blob_directory=root / "blobs",
                                    public_origin="http://localhost:5173", preview_origin="http://localhost:8001",
                                    model_provider="", model_secret_name="", task_timeout_seconds=180)
                encryption_key = Fernet.generate_key().decode()
                with patch.dict(os.environ, {"ATOMS_SECRET_ENCRYPTION_KEY": encryption_key}):
                    app = compose_application(settings)
                mail = MailStub()
                identity = app.services.identity
                identity.dependencies = replace(identity.dependencies, captcha=CaptchaStub(), mail=mail)
                workspaces = app.services.workspaces
                workspaces.dependencies = replace(workspaces.dependencies, mail=mail)
                anonymous = RequestContext(actor_id=None, session_id=None, workspace_id=None, request_id="r")

                async def signup(email):
                    await identity.register(anonymous, Credentials(email=email, password="strong-password-123",
                                                                   captcha_token="ok", accepted_terms=True))
                    await identity.request_verification(anonymous, VerificationRequest(
                        email=email, mutation=Mutation(idempotency_key=email)))
                    token = parse_qs(urlsplit(mail.urls[-1][1]).fragment)["token"][0]
                    session = await identity.verify(anonymous, token)
                    return await identity.authenticate(str(session.id))

                owner = await signup("owner@example.com")
                invited = await signup("invited@example.com")
                other = await signup("other@example.com")
                workspace = await workspaces.create(owner, "Team", Mutation(idempotency_key="create"))
                await workspaces.select(owner, workspace.id)
                owner = replace(owner, workspace_id=workspace.id)
                project = await app.services.projects.create(owner, CreateProject(
                    name="Private project", visibility=Visibility.PRIVATE,
                    mutation=Mutation(idempotency_key="project-create")))
                repeated = await app.services.projects.create(owner, CreateProject(
                    name="Private project", visibility=Visibility.PRIVATE,
                    mutation=Mutation(idempotency_key="project-create")))
                self.assertEqual(repeated.id, project.id)
                with self.assertRaises(DomainError):
                    await app.services.projects.create(owner, CreateProject(
                        name="Different project", visibility=Visibility.PRIVATE,
                        mutation=Mutation(idempotency_key="project-create")))
                with self.assertRaises(DomainError):
                    await app.services.projects.get(other, project.id)
                await app.services.projects.set_favorite(owner, project.id, True,
                                                         Mutation(idempotency_key="fav"))
                self.assertEqual((await app.services.projects.list_projects(owner, ProjectQuery(
                    page=PageQuery(), favorites_only=True))).items[0].id, project.id)
                project = await app.services.projects.rename(owner, project.id, "Renamed",
                                                             Mutation(idempotency_key="rename", expected_revision=1))
                self.assertEqual(project.name, "Renamed")
                project = await app.services.projects.set_visibility(owner, project.id, Visibility.PUBLIC,
                                                                     Mutation(idempotency_key="public", expected_revision=2))
                self.assertEqual((await app.services.projects.get(other, project.id)).visibility, Visibility.PUBLIC)
                self.assertEqual((await workspaces.list_workspaces(owner, PageQuery())).items[0].id, workspace.id)
                self.assertEqual((await workspaces.list_workspaces(other, PageQuery())).items, ())
                with self.assertRaises(DomainError) as denied:
                    await workspaces.select(other, workspace.id)
                self.assertEqual(denied.exception.code, ErrorCode.FORBIDDEN)
                await workspaces.invite(owner, "invited@example.com", Role.VIEWER,
                                        Mutation(idempotency_key="invite"))
                token = parse_qs(urlsplit(mail.urls[-1][1]).fragment)["token"][0]
                with self.assertRaises(DomainError):
                    await workspaces.accept_invitation(other, token)
                membership = await workspaces.accept_invitation(invited, token)
                with self.assertRaises(DomainError):
                    await workspaces.accept_invitation(invited, token)
                await workspaces.select(invited, workspace.id)
                invited = replace(invited, workspace_id=workspace.id)
                with self.assertRaises(DomainError):
                    await workspaces.invite(invited, "other@example.com", Role.MEMBER,
                                            Mutation(idempotency_key="bad"))
                with self.assertRaises(DomainError):
                    await app.services.identity.dependencies.authorization.require(invited, "workspace:manage", None)
                promoted = await workspaces.change_role(owner, membership.id, Role.ADMIN,
                                                         Mutation(idempotency_key="promote"))
                self.assertEqual(promoted.role, Role.ADMIN)
                await app.services.identity.dependencies.authorization.require(invited, "workspace:manage", None)
                await workspaces.invite(invited, "other@example.com", Role.VIEWER,
                                        Mutation(idempotency_key="admin-invite"))
                admin_token = parse_qs(urlsplit(mail.urls[-1][1]).fragment)["token"][0]
                admin_invitee = await workspaces.accept_invitation(other, admin_token)
                self.assertEqual(admin_invitee.role, Role.VIEWER)
                with self.assertRaises(DomainError) as admin_promotion:
                    await workspaces.change_role(invited, admin_invitee.id, Role.ADMIN,
                                                 Mutation(idempotency_key="admin-promote"))
                self.assertEqual(admin_promotion.exception.code, ErrorCode.FORBIDDEN)
                self.assertEqual(next(item.role for item in (
                    await workspaces.members(owner, PageQuery())).items
                    if item.id == admin_invitee.id), Role.VIEWER)
                await workspaces.select(other, workspace.id)
                selected_invitee = replace(other, workspace_id=workspace.id)
                await workspaces.remove_member(invited, admin_invitee.id,
                                               Mutation(idempotency_key="admin-remove"))
                with self.assertRaises(DomainError) as revoked_session:
                    await workspaces.members(selected_invitee, PageQuery())
                self.assertEqual(revoked_session.exception.code, ErrorCode.FORBIDDEN)
                with self.assertRaises(DomainError) as removed_selection:
                    await workspaces.select(other, workspace.id)
                self.assertEqual(removed_selection.exception.code, ErrorCode.FORBIDDEN)
                with self.assertRaises(DomainError):
                    await workspaces.remove_member(owner, EntityId(
                        __import__("hashlib").sha256(f"{workspace.id}:{owner.actor_id}".encode()).hexdigest()),
                        Mutation(idempotency_key="last-owner"))
                profile = await app.services.profiles.update(owner, Profile(
                    user_id=owner.actor_id, display_name="Owner", avatar=None, public_slug="owner-demo", revision=0),
                    Mutation(idempotency_key="profile"))
                self.assertEqual(profile.revision, 1)
                def png(color):
                    data = BytesIO()
                    Image.new("RGB", (2, 2), color).save(data, format="PNG")
                    return data.getvalue()
                avatar_bytes = png((220, 50, 80))
                avatar_object = await app.services.storage.upload(owner, Upload(
                    scope=Scope(workspace_id=workspace.id), purpose="avatar",
                    display_name="avatar.png", media_type="image/png", content=avatar_bytes,
                    mutation=Mutation(idempotency_key="avatar-upload")))
                profile = await app.profile.set_avatar(owner, avatar_object.id,
                                                       Mutation(idempotency_key="avatar-attach"))
                self.assertEqual(profile.avatar, avatar_object.blob)
                second_object = await app.services.storage.upload(owner, Upload(
                    scope=Scope(workspace_id=workspace.id), purpose="avatar",
                    display_name="avatar-two.png", media_type="image/png", content=png((20, 120, 200)),
                    mutation=Mutation(idempotency_key="avatar-upload-2")))
                newer = await app.profile.set_avatar(owner, second_object.id,
                                                     Mutation(idempotency_key="avatar-attach-2"))
                self.assertEqual(newer.avatar, second_object.blob)
                repeated = await app.profile.set_avatar(owner, avatar_object.id,
                                                        Mutation(idempotency_key="avatar-attach"))
                self.assertEqual(repeated.avatar, avatar_object.blob)
                self.assertEqual((await app.services.profiles.get(owner, owner.actor_id)).avatar,
                                 second_object.blob)
                self.assertEqual((await app.services.profiles.avatar(owner, owner.actor_id)).content,
                                 await app.services.storage.download(owner, second_object.id))
                profile = await app.services.profiles.update(owner, replace(newer,
                    display_name="Owner Updated", avatar=None), Mutation(idempotency_key="profile-name"))
                self.assertEqual(profile.avatar, second_object.blob)
                replay_after_name_change = await app.profile.set_avatar(owner, avatar_object.id,
                    Mutation(idempotency_key="avatar-attach"))
                self.assertEqual(replay_after_name_change.avatar, avatar_object.blob)
                current_profile = await app.services.profiles.get(owner, owner.actor_id)
                self.assertEqual((current_profile.display_name, current_profile.avatar),
                                 ("Owner Updated", second_object.blob))
                with self.assertRaises(DomainError) as changed_avatar_key:
                    await app.profile.set_avatar(owner, second_object.id,
                        Mutation(idempotency_key="avatar-attach"))
                self.assertEqual(changed_avatar_key.exception.code, ErrorCode.CONFLICT)
                invalid_object = await app.services.storage.upload(owner, Upload(
                    scope=Scope(workspace_id=workspace.id), purpose="avatar",
                    display_name="fake.png", media_type="image/png", content=b"not a png",
                    mutation=Mutation(idempotency_key="invalid-avatar-upload")))
                with self.assertRaises(DomainError) as bad_avatar:
                    await app.profile.set_avatar(owner, invalid_object.id,
                                                 Mutation(idempotency_key="invalid-avatar-attach"))
                self.assertEqual(bad_avatar.exception.code, ErrorCode.INVALID_INPUT)
                with self.assertRaises(DomainError):
                    await app.services.profiles.update(invited, Profile(
                        user_id=owner.actor_id, display_name="Hijack", avatar=None,
                        public_slug="owner-demo", revision=1), Mutation(idempotency_key="hijack"))
                preferences = await app.services.profiles.preferences(owner)
                preferences = await app.services.profiles.save_preferences(
                    owner, replace(preferences, default_model="selected-model"), Mutation(idempotency_key="prefs"))
                self.assertEqual(preferences.default_model, "selected-model")
                with patch.dict(os.environ, {"ATOMS_SECRET_ENCRYPTION_KEY": encryption_key}):
                    restarted = compose_application(settings)
                self.assertEqual((await restarted.services.profiles.preferences(owner)).default_model, "selected-model")
                self.assertEqual((await restarted.services.projects.get(owner, project.id)).name, "Renamed")
                self.assertIn(Role.ADMIN,
                              {item.role for item in (await restarted.services.workspaces.members(owner, PageQuery())).items})
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
