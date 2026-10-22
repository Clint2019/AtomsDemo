"""Public profile and private preference ownership."""
from __future__ import annotations

import re
import hashlib
import json
from dataclasses import dataclass

from atoms_demo.contracts.profiles import Profile, Preferences, AvatarImage, ProfileAPI
from atoms_demo.core.errors import DomainError, ErrorCode
from atoms_demo.core.types import BlobRef, EntityId, Mutation, RequestContext, Scope
from atoms_demo.ports.dependencies import ProfileDependencies

_GLOBAL = Scope(workspace_id=None)


@dataclass(frozen=True)
class ProfileRecord:
    id: EntityId
    display_name: str
    avatar: BlobRef | None
    public_slug: str
    revision: int = 0


@dataclass(frozen=True)
class ProfileSlug:
    id: EntityId
    user_id: EntityId
    revision: int = 0


@dataclass(frozen=True)
class PreferencesRecord:
    id: EntityId
    locale: str
    default_model: str
    default_visibility: str
    credit_reminders: bool
    sound: bool
    email_notifications: bool
    branding: bool
    revision: int = 0


@dataclass(frozen=True)
class ProfileMutation:
    id: EntityId
    input_digest: EntityId
    result: Profile
    revision: int = 0


@dataclass(frozen=True)
class PreferencesMutation:
    id: EntityId
    input_digest: EntityId
    result: Preferences
    revision: int = 0


@dataclass(frozen=True)
class AvatarMutation:
    id: EntityId
    object_id: EntityId
    result: Profile
    revision: int = 0


def decode_profile(data: dict) -> ProfileRecord:
    avatar = data["avatar"]
    if avatar is not None:
        avatar = BlobRef(**avatar)
    return ProfileRecord(**{**data, "avatar": avatar})


def decode_profile_mutation(data: dict) -> ProfileMutation:
    result = data["result"]
    avatar = BlobRef(**result["avatar"]) if result["avatar"] else None
    return ProfileMutation(**{**data, "result": Profile(**{**result, "avatar": avatar})})


def decode_preferences_mutation(data: dict) -> PreferencesMutation:
    return PreferencesMutation(**{**data, "result": Preferences(**data["result"])})


def decode_avatar_mutation(data: dict) -> AvatarMutation:
    result = data["result"]
    avatar = BlobRef(**result["avatar"]) if result["avatar"] else None
    return AvatarMutation(**{**data, "result": Profile(**{**result, "avatar": avatar})})


def _hash(value: str) -> EntityId:
    return EntityId(hashlib.sha256(value.encode("utf-8")).hexdigest())


class ProfileService:
    def __init__(self, dependencies: ProfileDependencies) -> None:
        self.dependencies = dependencies

    @staticmethod
    def _actor(ctx: RequestContext) -> EntityId:
        if ctx.actor_id is None:
            raise DomainError(ErrorCode.UNAUTHENTICATED, "Sign in required")
        return ctx.actor_id

    async def get(self, ctx: RequestContext, user_id: EntityId) -> Profile:
        if await self.dependencies.users.email_for(user_id) is None:
            raise DomainError(ErrorCode.NOT_FOUND, "Profile not found")
        async with self.dependencies.uow() as uow:
            record = await uow.repository(ProfileRecord).get(_GLOBAL, user_id)
        if record is None:
            return Profile(user_id=user_id, display_name="", avatar=None, public_slug="", revision=0)
        return Profile(user_id=user_id, display_name=record.display_name,
                       avatar=record.avatar, public_slug=record.public_slug, revision=record.revision)

    async def avatar(self, ctx: RequestContext, user_id: EntityId) -> AvatarImage:
        profile = await self.get(ctx, user_id)
        if profile.avatar is None:
            raise DomainError(ErrorCode.NOT_FOUND, "Avatar not found")
        if profile.avatar.media_type not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
            raise DomainError(ErrorCode.FORBIDDEN, "Unsupported avatar type")
        return AvatarImage(content=await self.dependencies.blobs.read(profile.avatar),
                           media_type=profile.avatar.media_type)

    async def avatar_result(self, ctx: RequestContext, object_id: EntityId,
                            mutation: Mutation) -> Profile | None:
        actor = self._actor(ctx)
        if not mutation.idempotency_key:
            raise DomainError(ErrorCode.INVALID_INPUT, "Idempotency key required")
        key = _hash(f"profile:avatar:{actor}:{mutation.idempotency_key}")
        async with self.dependencies.uow() as uow:
            prior = await uow.repository(AvatarMutation).get(_GLOBAL, key)
        if prior and prior.object_id != object_id:
            raise DomainError(ErrorCode.CONFLICT, "Avatar key changed input")
        return prior.result if prior else None

    async def set_avatar(self, ctx: RequestContext, object_id: EntityId,
                         blob: BlobRef, mutation: Mutation) -> Profile:
        actor = self._actor(ctx)
        if not mutation.idempotency_key:
            raise DomainError(ErrorCode.INVALID_INPUT, "Idempotency key required")
        key = _hash(f"profile:avatar:{actor}:{mutation.idempotency_key}")
        async with self.dependencies.uow() as uow:
            prior = await uow.repository(AvatarMutation).get(_GLOBAL, key)
            if prior:
                if prior.object_id != object_id:
                    raise DomainError(ErrorCode.CONFLICT, "Avatar key changed input")
                return prior.result
            repo = uow.repository(ProfileRecord)
            current = await repo.get(_GLOBAL, actor)
            if current is None or not current.display_name or not current.public_slug:
                raise DomainError(ErrorCode.CONFLICT, "Save profile details before setting an avatar")
            saved = await repo.save(_GLOBAL, ProfileRecord(id=actor,
                display_name=current.display_name, avatar=blob,
                public_slug=current.public_slug), current.revision)
            result = Profile(user_id=actor, display_name=saved.display_name,
                avatar=saved.avatar, public_slug=saved.public_slug, revision=saved.revision)
            await uow.repository(AvatarMutation).save(_GLOBAL, AvatarMutation(
                id=key, object_id=object_id, result=result), None)
            await uow.commit()
        return result

    async def update(self, ctx: RequestContext, profile: Profile, mutation: Mutation) -> Profile:
        if self._actor(ctx) != profile.user_id:
            raise DomainError(ErrorCode.FORBIDDEN, "Cannot edit another profile")
        if not mutation.idempotency_key:
            raise DomainError(ErrorCode.INVALID_INPUT, "Idempotency key required")
        name = profile.display_name.strip()
        slug = profile.public_slug.strip().lower()
        if not 1 <= len(name) <= 80 or not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,39}", slug):
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid profile name or public slug")
        key = _hash(f"profile:update:{profile.user_id}:{mutation.idempotency_key}")
        digest = _hash(json.dumps({"name": name, "slug": slug,
            "avatar": profile.avatar.id if profile.avatar else None,
            "expected_revision": mutation.expected_revision}, sort_keys=True))
        async with self.dependencies.uow() as uow:
            prior = await uow.repository(ProfileMutation).get(_GLOBAL, key)
            if prior:
                if prior.input_digest != digest:
                    raise DomainError(ErrorCode.CONFLICT, "Profile key changed input")
                return prior.result
            repo = uow.repository(ProfileRecord)
            current = await repo.get(_GLOBAL, profile.user_id)
            if mutation.expected_revision is not None and mutation.expected_revision != (current.revision if current else 0):
                raise DomainError(ErrorCode.CONFLICT, "Profile revision changed")
            owner = await uow.repository(ProfileSlug).get(_GLOBAL, EntityId(slug))
            if owner and owner.user_id != profile.user_id:
                raise DomainError(ErrorCode.CONFLICT, "Public slug is already used")
            if current and current.public_slug != slug:
                await uow.repository(ProfileSlug).delete(_GLOBAL, EntityId(current.public_slug), 1)
            if owner is None:
                await uow.repository(ProfileSlug).save(
                    _GLOBAL, ProfileSlug(id=EntityId(slug), user_id=profile.user_id), None)
            saved = await repo.save(_GLOBAL, ProfileRecord(id=profile.user_id, display_name=name,
                                                            avatar=profile.avatar or (current.avatar if current else None),
                                                            public_slug=slug),
                                    current.revision if current else None)
            result = Profile(user_id=profile.user_id, display_name=saved.display_name,
                             avatar=saved.avatar, public_slug=saved.public_slug, revision=saved.revision)
            await uow.repository(ProfileMutation).save(_GLOBAL, ProfileMutation(
                id=key, input_digest=digest, result=result), None)
            await uow.commit()
        return result

    async def preferences(self, ctx: RequestContext) -> Preferences:
        actor = self._actor(ctx)
        async with self.dependencies.uow() as uow:
            record = await uow.repository(PreferencesRecord).get(_GLOBAL, actor)
        if record is None:
            return Preferences(user_id=actor, locale="zh-CN", default_model="",
                               default_visibility="private", credit_reminders=True, sound=True,
                               email_notifications=True, branding=True, revision=0)
        return Preferences(user_id=actor, locale=record.locale, default_model=record.default_model,
                           default_visibility=record.default_visibility,
                           credit_reminders=record.credit_reminders, sound=record.sound,
                           email_notifications=record.email_notifications, branding=record.branding,
                           revision=record.revision)

    async def save_preferences(self, ctx: RequestContext, preferences: Preferences,
                               mutation: Mutation) -> Preferences:
        actor = self._actor(ctx)
        if actor != preferences.user_id:
            raise DomainError(ErrorCode.FORBIDDEN, "Cannot edit another user's preferences")
        if not mutation.idempotency_key:
            raise DomainError(ErrorCode.INVALID_INPUT, "Idempotency key required")
        if preferences.locale not in {"zh-CN", "en-US"} or preferences.default_visibility not in {"private", "public"}:
            raise DomainError(ErrorCode.INVALID_INPUT, "Unsupported preference value")
        if len(preferences.default_model) > 100:
            raise DomainError(ErrorCode.INVALID_INPUT, "Model name is too long")
        key = _hash(f"profile:preferences:{actor}:{mutation.idempotency_key}")
        digest = _hash(json.dumps({"locale": preferences.locale,
            "default_model": preferences.default_model,
            "default_visibility": preferences.default_visibility,
            "credit_reminders": preferences.credit_reminders,
            "sound": preferences.sound,
            "email_notifications": preferences.email_notifications,
            "branding": preferences.branding,
            "expected_revision": mutation.expected_revision}, sort_keys=True))
        async with self.dependencies.uow() as uow:
            prior = await uow.repository(PreferencesMutation).get(_GLOBAL, key)
            if prior:
                if prior.input_digest != digest:
                    raise DomainError(ErrorCode.CONFLICT, "Preferences key changed input")
                return prior.result
            repo = uow.repository(PreferencesRecord)
            current = await repo.get(_GLOBAL, actor)
            if mutation.expected_revision is not None and mutation.expected_revision != (current.revision if current else 0):
                raise DomainError(ErrorCode.CONFLICT, "Preferences revision changed")
            saved = await repo.save(_GLOBAL, PreferencesRecord(
                id=actor, locale=preferences.locale, default_model=preferences.default_model,
                default_visibility=preferences.default_visibility,
                credit_reminders=preferences.credit_reminders, sound=preferences.sound,
                email_notifications=preferences.email_notifications, branding=preferences.branding),
                current.revision if current else None)
            result = Preferences(user_id=actor, locale=saved.locale, default_model=saved.default_model,
                                 default_visibility=saved.default_visibility,
                                 credit_reminders=saved.credit_reminders, sound=saved.sound,
                                 email_notifications=saved.email_notifications,
                                 branding=saved.branding, revision=saved.revision)
            await uow.repository(PreferencesMutation).save(_GLOBAL, PreferencesMutation(
                id=key, input_digest=digest, result=result), None)
            await uow.commit()
        return result
