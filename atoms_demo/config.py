"""Validated process configuration. Importing this module has no side effects."""
from __future__ import annotations

import os
import ipaddress
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


TURNSTILE_TEST_SITE_KEYS = frozenset({
    "1x00000000000000000000AA", "2x00000000000000000000AB",
    "1x00000000000000000000BB", "2x00000000000000000000BB",
    "3x00000000000000000000FF",
})
TURNSTILE_TEST_SECRET_KEYS = frozenset({
    "1x0000000000000000000000000000000AA",
    "2x0000000000000000000000000000000AA",
    "3x0000000000000000000000000000000AA",
})


@dataclass(frozen=True, kw_only=True)
class Settings:
    database_url: str
    blob_directory: Path
    public_origin: str
    preview_origin: str
    model_provider: str
    model_secret_name: str
    task_timeout_seconds: int
    model_timeout_seconds: int = 600
    build_backend: str = ""
    build_docker_host: str = ""
    model_name: str = ""
    model_base_url: str = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    model_max_output_tokens: int = 32768
    model_input_credits_per_million: int = 1000
    model_output_credits_per_million: int = 4000
    initial_build_credits: int = 0
    max_repairs: int = 2
    automatic_infrastructure_enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8000
    preview_bind_host: str = "127.0.0.1"
    preview_bind_port: int | None = None
    max_request_bytes: int = 1_048_576
    max_blob_bytes: int = 20_971_520
    storage_quota_bytes: int = 1_073_741_824
    queue_lease_seconds: int = 60
    deployment_verify_hosts: tuple[str, ...] = ()
    private_test_mode: bool = False
    public_ip_test_mode: bool = False
    private_mail_outbox: Path | None = None
    captcha_provider: str = ""
    turnstile_site_key: str = ""
    turnstile_secret_key: str = field(default="", repr=False)


def validate_private_test_settings(settings: Settings) -> None:
    """Reject accidental activation outside the local SSH-only staging layout."""
    if not settings.private_test_mode:
        return
    public_host = urlsplit(settings.public_origin).hostname or ""
    preview_host = urlsplit(settings.preview_origin).hostname or ""
    public_ip_allowed = False
    if settings.public_ip_test_mode:
        try:
            public_ip_allowed = ipaddress.ip_address(public_host).version == 4 and ipaddress.ip_address(public_host).is_global
        except ValueError:
            pass
    if (settings.public_ip_test_mode and not public_ip_allowed or
            settings.host != "127.0.0.1" or
            settings.preview_bind_host != "127.0.0.1" or
            not (public_host.endswith(".localhost") or public_ip_allowed) or
            not (preview_host.endswith(".localhost") or
                 (public_ip_allowed and preview_host == public_host and
                  urlsplit(settings.preview_origin).port != urlsplit(settings.public_origin).port)) or
            settings.private_mail_outbox is None or
            not settings.private_mail_outbox.is_absolute() or
            settings.private_mail_outbox.resolve() != database_path(settings).parent / "private-mail" / "outbox.jsonl" or
            settings.private_mail_outbox.resolve().is_relative_to(Path(__file__).resolve().parents[1]) or
            settings.private_mail_outbox.resolve().is_relative_to(settings.blob_directory.resolve())):
        raise ValueError("Private test mode requires loopback, .localhost origins and a separate absolute outbox")


def _origin(value: str, name: str, *, allow_public_http_ip: bool = False) -> str:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError(f"{name} has an invalid origin") from None
    if port == 0:
        raise ValueError(f"{name} has an invalid origin")
    if (parsed.scheme not in {"http", "https"} or not hostname or
            parsed.username or parsed.password or parsed.path not in {"", "/"} or
            parsed.query or parsed.fragment):
        raise ValueError(f"{name} must be an absolute HTTP origin without a path")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        if (len(hostname) > 253 or not re.fullmatch(r"[A-Za-z0-9.-]+", hostname) or
                any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
                    for label in hostname.split("."))):
            raise ValueError(f"{name} has an invalid hostname") from None
    if parsed.scheme == "http" and hostname not in {"localhost", "127.0.0.1", "::1"} and not hostname.endswith(".localhost"):
        try:
            public_ip = ipaddress.ip_address(hostname)
        except ValueError:
            public_ip = None
        if not (allow_public_http_ip and public_ip is not None and
                public_ip.version == 4 and public_ip.is_global):
            raise ValueError(f"{name} must use HTTPS outside localhost")
    host = f"[{hostname}]" if ":" in hostname else hostname.lower()
    default_port = 80 if parsed.scheme == "http" else 443
    return f"{parsed.scheme}://{host}{':' + str(port) if port and port != default_port else ''}"


def database_path(settings: Settings) -> Path:
    prefix = "sqlite:///"
    if not settings.database_url.startswith(prefix):
        raise ValueError("ATOMS_DATABASE_URL must use sqlite:///path")
    raw = settings.database_url[len(prefix):]
    if not raw or raw == ":memory:" or "?" in raw or "#" in raw:
        raise ValueError("ATOMS_DATABASE_URL must name a persistent SQLite file")
    return Path(raw).expanduser().resolve()


def load_settings(environ: dict[str, str] | None = None) -> Settings:
    env = os.environ if environ is None else environ
    required = ("ATOMS_DATABASE_URL", "ATOMS_BLOB_DIRECTORY", "ATOMS_PUBLIC_ORIGIN", "ATOMS_PREVIEW_ORIGIN")
    missing = [name for name in required if not env.get(name, "").strip()]
    if missing:
        raise ValueError("Missing configuration: " + ", ".join(missing))

    def integer(name: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(env.get(name, str(default)))
        except ValueError:
            raise ValueError(f"{name} must be an integer") from None
        if not minimum <= value <= maximum:
            raise ValueError(f"{name} must be between {minimum} and {maximum}")
        return value

    raw_private_test = env.get("ATOMS_PRIVATE_TEST_MODE", "false").strip().lower()
    if raw_private_test not in {"false", "0", "true", "1"}:
        raise ValueError("ATOMS_PRIVATE_TEST_MODE must be true or false")
    private_test_mode = raw_private_test in {"true", "1"}
    raw_public_ip_test = env.get("ATOMS_PUBLIC_IP_TEST_MODE", "false").strip().lower()
    if raw_public_ip_test not in {"false", "0", "true", "1"}:
        raise ValueError("ATOMS_PUBLIC_IP_TEST_MODE must be true or false")
    public_ip_test_mode = raw_public_ip_test in {"true", "1"}
    if public_ip_test_mode and not private_test_mode:
        raise ValueError("ATOMS_PUBLIC_IP_TEST_MODE requires private test mode")
    public = _origin(env["ATOMS_PUBLIC_ORIGIN"].strip(), "ATOMS_PUBLIC_ORIGIN",
                     allow_public_http_ip=public_ip_test_mode)
    preview = _origin(env["ATOMS_PREVIEW_ORIGIN"].strip(), "ATOMS_PREVIEW_ORIGIN",
                      allow_public_http_ip=public_ip_test_mode)
    if (urlsplit(public).hostname == urlsplit(preview).hostname and
            not (public_ip_test_mode and urlsplit(public).port != urlsplit(preview).port)):
        raise ValueError("Public and preview hosts must differ to isolate cookies")
    if urlsplit(public).scheme != urlsplit(preview).scheme:
        raise ValueError("Public and preview origins must use the same scheme for embedded sessions")
    if (urlsplit(preview).hostname == "preview.localhost" and not public_ip_test_mode and
            urlsplit(public).hostname != "app.preview.localhost"):
        raise ValueError("Use app.preview.localhost as the local public host for embedded previews")
    preview_bind_host = env.get("ATOMS_PREVIEW_BIND_HOST", "127.0.0.1").strip()
    if preview_bind_host != "127.0.0.1":
        raise ValueError("ATOMS_PREVIEW_BIND_HOST must be loopback")
    preview_bind_port = (integer("ATOMS_PREVIEW_BIND_PORT", 8132, 1, 65535)
        if env.get("ATOMS_PREVIEW_BIND_PORT") else None)
    if urlsplit(preview).scheme == "https" and preview_bind_port is None:
        raise ValueError("ATOMS_PREVIEW_BIND_PORT is required behind HTTPS proxy")
    raw_auto = env.get("ATOMS_AUTOMATIC_INFRASTRUCTURE_ENABLED", "false").lower()
    if raw_auto not in {"false", "0"}:
        raise ValueError("Automatic infrastructure execution is deferred")
    verify_hosts = tuple(item.strip().lower() for item in
                         env.get("ATOMS_DEPLOYMENT_VERIFY_HOSTS", "").split(",") if item.strip())
    if any(not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", host) or
           len(host) > 253 or any(part.startswith("-") or part.endswith("-")
           for part in host.split(".")) for host in verify_hosts):
        raise ValueError("ATOMS_DEPLOYMENT_VERIFY_HOSTS must contain DNS hostnames")
    provider = env.get("ATOMS_MODEL_PROVIDER", "").strip()
    build_backend = env.get("ATOMS_BUILD_BACKEND", "").strip()
    if build_backend not in {"", "docker"}:
        raise ValueError("ATOMS_BUILD_BACKEND must be docker or empty")
    build_docker_host = env.get("ATOMS_BUILD_DOCKER_HOST", "").strip()
    if build_docker_host and not re.fullmatch(r"ssh://[A-Za-z0-9_.@:-]+", build_docker_host):
        raise ValueError("ATOMS_BUILD_DOCKER_HOST must use an SSH host")
    private_mail_outbox = None
    if private_test_mode:
        public_host = urlsplit(public).hostname or ""
        preview_host = urlsplit(preview).hostname or ""
        if (env.get("ATOMS_HOST", "127.0.0.1") != "127.0.0.1" or
                not (public_host.endswith(".localhost") or public_ip_test_mode) or
                not (preview_host.endswith(".localhost") or
                     (public_ip_test_mode and preview_host == public_host and
                      urlsplit(public).port != urlsplit(preview).port))):
            raise ValueError("Private test mode requires loopback bind and .localhost origins")
        raw_outbox = env.get("ATOMS_PRIVATE_MAIL_OUTBOX", "").strip()
        if not raw_outbox or not Path(raw_outbox).is_absolute():
            raise ValueError("ATOMS_PRIVATE_MAIL_OUTBOX must be an absolute path in private test mode")
        private_mail_outbox = Path(raw_outbox).resolve()
        blob_root = Path(env["ATOMS_BLOB_DIRECTORY"]).expanduser().resolve()
        if private_mail_outbox.is_relative_to(blob_root):
            raise ValueError("Private mail outbox must be outside the blob directory")
    elif env.get("ATOMS_PRIVATE_MAIL_OUTBOX", "").strip():
        raise ValueError("ATOMS_PRIVATE_MAIL_OUTBOX requires private test mode")
    captcha_provider = env.get("ATOMS_CAPTCHA_PROVIDER", "").strip().lower()
    turnstile_site_key = env.get("ATOMS_TURNSTILE_SITE_KEY", "").strip()
    turnstile_secret_key = env.get("ATOMS_TURNSTILE_SECRET_KEY", "").strip()
    if captcha_provider not in {"", "turnstile"}:
        raise ValueError("ATOMS_CAPTCHA_PROVIDER must be turnstile or empty")
    if captcha_provider == "turnstile":
        if not turnstile_site_key or not turnstile_secret_key:
            raise ValueError("Turnstile requires a site key and secret key")
    elif turnstile_site_key or turnstile_secret_key:
        raise ValueError("Turnstile keys require ATOMS_CAPTCHA_PROVIDER=turnstile")
    test_keys = (turnstile_site_key in TURNSTILE_TEST_SITE_KEYS and
                 turnstile_secret_key in TURNSTILE_TEST_SECRET_KEYS)
    if ((turnstile_site_key in TURNSTILE_TEST_SITE_KEYS) !=
            (turnstile_secret_key in TURNSTILE_TEST_SECRET_KEYS)):
        raise ValueError("Turnstile test and production keys cannot be mixed")
    if private_test_mode and captcha_provider == "turnstile" and not test_keys:
        raise ValueError("Private test mode requires official Turnstile test keys")
    if not private_test_mode and test_keys:
        raise ValueError("Turnstile test keys require private test mode")
    settings = Settings(
        database_url=env["ATOMS_DATABASE_URL"].strip(),
        blob_directory=Path(env["ATOMS_BLOB_DIRECTORY"]).expanduser().resolve(),
        public_origin=public,
        preview_origin=preview,
        model_provider=provider,
        model_secret_name=env.get("ATOMS_MODEL_SECRET_NAME",
            "OPS_TOKEN_PLAN_API_KEY" if provider == "aliyun_token_plan" else "").strip(),
        task_timeout_seconds=integer("ATOMS_TASK_TIMEOUT_SECONDS", 180, 10, 3600),
        model_timeout_seconds=integer("ATOMS_MODEL_TIMEOUT_SECONDS", 600, 10, 3600),
        build_backend=build_backend,
        build_docker_host=build_docker_host,
        model_name=env.get("ATOMS_MODEL_NAME",
            "qwen3.7-plus" if provider == "aliyun_token_plan" else "").strip(),
        model_base_url=env.get("ATOMS_MODEL_BASE_URL", "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1").strip().rstrip("/"),
        model_max_output_tokens=integer("ATOMS_MODEL_MAX_OUTPUT_TOKENS", 32768, 256, 65536),
        model_input_credits_per_million=integer("ATOMS_MODEL_INPUT_CREDITS_PER_MILLION", 1000, 1, 1_000_000),
        model_output_credits_per_million=integer("ATOMS_MODEL_OUTPUT_CREDITS_PER_MILLION", 4000, 1, 1_000_000),
        initial_build_credits=integer("ATOMS_INITIAL_BUILD_CREDITS",
            100000 if provider == "aliyun_token_plan" else 0,
            0, 1_000_000_000),
        max_repairs=integer("ATOMS_MAX_REPAIRS", 2, 0, 2),
        host=env.get("ATOMS_HOST", "127.0.0.1"),
        port=integer("ATOMS_PORT", 8000, 1, 65535),
        preview_bind_host=preview_bind_host,
        preview_bind_port=preview_bind_port,
        max_request_bytes=integer("ATOMS_MAX_REQUEST_BYTES", 1_048_576, 1024, 10_485_760),
        max_blob_bytes=integer("ATOMS_MAX_BLOB_BYTES", 20_971_520, 1024, 1_073_741_824),
        storage_quota_bytes=integer("ATOMS_STORAGE_QUOTA_BYTES", 1_073_741_824, 1024, 1_099_511_627_776),
        queue_lease_seconds=integer("ATOMS_QUEUE_LEASE_SECONDS", 60, 5, 3600),
        deployment_verify_hosts=verify_hosts,
        private_test_mode=private_test_mode,
        public_ip_test_mode=public_ip_test_mode,
        private_mail_outbox=private_mail_outbox,
        captcha_provider=captcha_provider,
        turnstile_site_key=turnstile_site_key,
        turnstile_secret_key=turnstile_secret_key,
    )
    validate_private_test_settings(settings)
    database_path(settings)
    return settings
