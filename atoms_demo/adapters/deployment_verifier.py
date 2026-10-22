"""Read-only HTTPS verifier pinned to allowlisted public DNS answers."""
from __future__ import annotations

import asyncio
import http.client
import ipaddress
import json
import socket
import ssl
from urllib.parse import urlsplit

from atoms_demo.config import Settings
from atoms_demo.contracts.publication import ManualDeploymentResult
from atoms_demo.core.errors import DomainError, ErrorCode
from atoms_demo.core.types import EntityId
from atoms_demo.ports.providers import DeploymentVerification, DeploymentVerifier

_MARKER_PATH = "/.well-known/atoms-demo.json"
_MAX_MARKER = 16_384


class UnconfiguredDeploymentVerifier:
    async def verify(self, result: ManualDeploymentResult,
                     expected_version_id: EntityId) -> DeploymentVerification:
        raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE,
                          "Deployment verification hosts are not configured")

    async def verify_state(self, result: ManualDeploymentResult,
                           expected_version_id: EntityId, state: str) -> DeploymentVerification:
        raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE,
                          "Deployment verification hosts are not configured")


class HTTPSDeploymentVerifier:
    def __init__(self, allowed_hosts: tuple[str, ...]) -> None:
        self.allowed_hosts = frozenset(allowed_hosts)

    def _verify_sync(self, result: ManualDeploymentResult,
                     expected_version_id: EntityId, expected_state: str) -> DeploymentVerification:
        try:
            parsed = urlsplit(result.public_url)
            hostname = parsed.hostname
            if (parsed.scheme != "https" or hostname not in self.allowed_hosts or
                    parsed.port not in {None, 443} or parsed.username or parsed.password or
                    parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
                raise DomainError(ErrorCode.INVALID_INPUT, "Deployment URL is not allowlisted")
            answers = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
            if not answers or any(not ipaddress.ip_address(item[4][0]).is_global for item in answers):
                raise DomainError(ErrorCode.FORBIDDEN, "Deployment address is not public")
            def fetch(address, family, socktype, protocol, path, limit, enforce_limit=True):
                with socket.socket(family, socktype, protocol) as raw:
                    raw.settimeout(5)
                    raw.connect(address)
                    with ssl.create_default_context().wrap_socket(raw,
                            server_hostname=hostname) as secure:
                        secure.settimeout(5)
                        secure.sendall((f"GET {path} HTTP/1.1\r\n"
                            f"Host: {hostname}\r\nConnection: close\r\n\r\n").encode("ascii"))
                        response = http.client.HTTPResponse(secure)
                        response.begin()
                        body = response.read(limit + 1)
                        if enforce_limit and len(body) > limit:
                            raise DomainError(ErrorCode.INVALID_INPUT,
                                "Deployment response exceeds limit")
                        return response.status, response.getheader("Content-Type", ""), body

            for family, socktype, protocol, _, address in answers:
                try:
                    root_status, root_type, root_body = fetch(address, family, socktype,
                        protocol, "/", 1, False)
                    if expected_state == "live":
                        root_valid = root_status == 200 and bool(root_body) and "text/html" in root_type.lower()
                    else:
                        root_valid = root_status in {403, 404, 410, 503}
                    if not root_valid:
                        return DeploymentVerification(reachable=False, version_matches=False,
                            diagnostics=("Application root state does not match requested action",))
                    marker_status, _, payload = fetch(address, family, socktype,
                        protocol, _MARKER_PATH, _MAX_MARKER)
                    if marker_status != 200:
                        return DeploymentVerification(reachable=False,
                            version_matches=False,
                            diagnostics=(f"Version marker returned HTTP {marker_status}",))
                    marker = json.loads(payload)
                    matches = (isinstance(marker, dict) and
                        marker.get("version_id") == str(expected_version_id) and
                        marker.get("state") == expected_state)
                    return DeploymentVerification(reachable=True,
                        version_matches=matches,
                        diagnostics=() if matches else ("Deployed version marker differs",))
                except (OSError, ssl.SSLError, http.client.HTTPException):
                    continue
            return DeploymentVerification(reachable=False, version_matches=False,
                diagnostics=("Deployment endpoint is unreachable",))
        except (ValueError, TypeError, json.JSONDecodeError):
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid deployment URL or marker") from None
        except socket.gaierror:
            return DeploymentVerification(reachable=False, version_matches=False,
                diagnostics=("Deployment hostname did not resolve",))

    async def verify(self, result: ManualDeploymentResult,
                     expected_version_id: EntityId) -> DeploymentVerification:
        return await asyncio.to_thread(self._verify_sync, result, expected_version_id, "live")

    async def verify_state(self, result: ManualDeploymentResult,
                           expected_version_id: EntityId, state: str) -> DeploymentVerification:
        if state not in {"paused", "unpublished"}:
            raise DomainError(ErrorCode.INVALID_INPUT, "Unsupported remote state")
        return await asyncio.to_thread(self._verify_sync, result, expected_version_id, state)


def create_adapter(settings: Settings) -> DeploymentVerifier:
    hosts = settings.deployment_verify_hosts
    return HTTPSDeploymentVerifier(hosts) if hosts else UnconfiguredDeploymentVerifier()
