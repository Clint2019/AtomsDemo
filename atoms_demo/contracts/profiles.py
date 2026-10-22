from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from atoms_demo.core.types import (
    BlobRef, EntityId, Environment, Mutation, OperationReceipt,
    Page, PageQuery, RequestContext, Scope, SecretRef,
)

@dataclass(frozen=True, kw_only=True)
class Profile:
    user_id: EntityId
    display_name: str
    avatar: BlobRef | None
    public_slug: str
    revision: int

@dataclass(frozen=True, kw_only=True)
class Preferences:
    user_id: EntityId
    locale: str
    default_model: str
    default_visibility: str
    credit_reminders: bool
    sound: bool
    email_notifications: bool
    branding: bool
    revision: int

@dataclass(frozen=True, kw_only=True)
class AvatarImage:
    content: bytes
    media_type: str

class ProfileAPI(Protocol):
    async def get(self, ctx: RequestContext, user_id: EntityId) -> Profile: ...
    async def avatar(self, ctx: RequestContext, user_id: EntityId) -> AvatarImage: ...
    async def avatar_result(self, ctx: RequestContext, object_id: EntityId, mutation: Mutation) -> Profile | None: ...
    async def set_avatar(self, ctx: RequestContext, object_id: EntityId, blob: BlobRef, mutation: Mutation) -> Profile: ...
    async def update(self, ctx: RequestContext, profile: Profile, mutation: Mutation) -> Profile: ...
    async def preferences(self, ctx: RequestContext) -> Preferences: ...
    async def save_preferences(self, ctx: RequestContext, preferences: Preferences, mutation: Mutation) -> Preferences: ...
