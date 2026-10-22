from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol
from atoms_demo.core.types import BlobRef, EntityId
from atoms_demo.contracts.generation import AnswerResult, FileChangeSet, GenerationInput, RepairInput
from atoms_demo.contracts.builds import BuildInput, BuildResult, ScanResult
from atoms_demo.contracts.billing import Checkout, Money
from atoms_demo.core.metering import StorageMetering
from atoms_demo.contracts.publication import PublicationConfig, ManualDeploymentResult


class ModelProvider(Protocol):
    async def generate(self, command: GenerationInput) -> FileChangeSet: ...
    async def repair(self, command: RepairInput) -> FileChangeSet: ...
    async def answer(self, command: GenerationInput) -> AnswerResult: ...


class BuildExecutor(Protocol):
    """Runs fixed build recipes in isolation, never arbitrary model shell commands."""
    async def build(self, command: BuildInput) -> BuildResult: ...
    async def scan(self, version_id: EntityId, artifact: BlobRef) -> ScanResult: ...


class MailProvider(Protocol):
    async def send_verification(self, address: str, verification_url: str, idempotency_key: str) -> str: ...
    async def send_invitation(self, address: str, invitation_url: str, idempotency_key: str) -> str: ...


class CaptchaProvider(Protocol):
    async def verify(self, token: str, request_id: str) -> bool: ...


@dataclass(frozen=True, kw_only=True)
class OAuthIdentity:
    subject: str
    email: str | None
    access_token: str
    refresh_token: str | None


class OAuthProvider(Protocol):
    async def authorize_url(self, provider: str, state: str, challenge: str) -> str: ...
    async def exchange(self, provider: str, code: str, verifier: str) -> OAuthIdentity: ...
    async def revoke(self, provider: str, token: str) -> None: ...


class PaymentProvider(Protocol):
    # Local configuration check only; called while a database unit of work is open.
    async def ensure_configured(self) -> None: ...
    async def checkout(self, reference: str, amount: Money, idempotency_key: str) -> Checkout: ...
    async def verify_webhook(self, body: bytes, signature: str) -> VerifiedPaymentEvent: ...


class StoragePricing(Protocol):
    async def quote(self, event: StorageMetering) -> Money | None: ...


class UsagePricing(Protocol):
    async def reserve(self, model: str, prompt: str) -> int: ...
    async def charge(self, model: str, input_tokens: int, output_tokens: int) -> int: ...


@dataclass(frozen=True, kw_only=True)
class VerifiedPaymentEvent:
    provider_event_id: str
    checkout_reference: str
    status: str
    amount: Money
    provider_subscription_reference: str | None


class InfrastructureExecutor(Protocol):
    """Deferred ONLY: cloud provisioning / remote deployment / DNS / certificates."""
    async def execute(self, config: PublicationConfig) -> str: ...


@dataclass(frozen=True, kw_only=True)
class DeploymentVerification:
    reachable: bool
    version_matches: bool
    diagnostics: tuple[str, ...]


class DeploymentVerifier(Protocol):
    """Read-only, allowlisted endpoint checks; no cloud provisioning or arbitrary URL fetch."""
    async def verify(self, result: ManualDeploymentResult, expected_version_id: EntityId) -> DeploymentVerification: ...
    async def verify_state(self, result: ManualDeploymentResult, expected_version_id: EntityId,
                           state: str) -> DeploymentVerification: ...
