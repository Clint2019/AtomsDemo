"""Attach a previously authorized storage image to the caller's public profile."""
from io import BytesIO

from PIL import Image, UnidentifiedImageError

from atoms_demo.contracts.profiles import Profile
from atoms_demo.core.errors import DomainError, ErrorCode
from atoms_demo.core.types import EntityId, Mutation, RequestContext
from atoms_demo.workflows.registry import ProfileServices

_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_MAX_BYTES = 5_000_000


def _matches_image(data: bytes, media_type: str) -> bool:
    formats = {"image/png": "PNG", "image/jpeg": "JPEG",
               "image/gif": "GIF", "image/webp": "WEBP"}
    try:
        with Image.open(BytesIO(data)) as image:
            if (image.format != formats.get(media_type) or image.width < 1 or
                    image.height < 1 or image.width > 4096 or image.height > 4096 or
                    image.width * image.height > 16_000_000):
                return False
            image.verify()
        with Image.open(BytesIO(data)) as image:
            image.load()
        return True
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        return False


class ProfileWorkflow:
    def __init__(self, services: ProfileServices) -> None:
        self.services = services

    async def set_avatar(self, ctx: RequestContext, object_id: EntityId,
                         mutation: Mutation) -> Profile:
        if ctx.actor_id is None:
            raise DomainError(ErrorCode.UNAUTHENTICATED, "Sign in required")
        prior = await self.services.profiles.avatar_result(ctx, object_id, mutation)
        if prior is not None:
            return prior
        stored = await self.services.storage.get(ctx, object_id)
        if (stored.scope.project_id is not None or stored.purpose != "avatar" or
                stored.blob.media_type not in _TYPES or
                not 1 <= stored.blob.size_bytes <= _MAX_BYTES):
            raise DomainError(ErrorCode.INVALID_INPUT, "Avatar must be a workspace image")
        data = await self.services.storage.download(ctx, object_id)
        if not _matches_image(data, stored.blob.media_type):
            raise DomainError(ErrorCode.INVALID_INPUT, "Avatar file content is invalid")
        return await self.services.profiles.set_avatar(ctx, object_id, stored.blob, mutation)
