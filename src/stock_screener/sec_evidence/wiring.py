"""Choosing the artifact backend once, from configuration, before anything runs.

Every evidence command needs the same three things: somewhere artifacts live,
somewhere the control plane lives, and a decision about which is which. This
module makes that decision in one place so that the CLI, the sweep and any
future caller receive an `EvidenceStorage` and never ask "local or bucket?"
again. Downstream code cannot tell the backends apart, and must not try.

## Selection is explicit, and it fails closed

`SEC_STORAGE_BACKEND` names the backend. Nothing is inferred from what else
happens to be configured: B2 credentials sitting in the environment do not
select `b2`, and their absence does not select `local`. A `b2` run whose
configuration is incomplete raises before any artifact operation and is never
quietly served by a local directory instead — that would put new evidence
somewhere the manifest does not describe, which is the mis-attribution the
whole evidence layer exists to prevent. The backend chosen for a run is the
backend for the whole run.

## Configuration is validated offline

Validation here means: the fields are present, their shapes are sane, and the
combination is coherent. It never means contacting Backblaze — no
authentication, no bucket HEAD, no listing. A future backend qualifies its
bucket when it is constructed, not when settings are read, and this module is
what lets a misconfiguration surface as a one-line startup message rather than
a stack trace on the first upload.

## Secrets do not leave this module

The application key and key ID are `SecretStr` on the way in, stay wrapped in
`B2Configuration`, and never appear in an error message, a log line, a
`repr`, or status output. Errors name the *setting* that is missing or
malformed, never the value it holds.

## Nothing here is a backend

`open_evidence_storage` returns `LocalEvidenceStorage` for `local`. For `b2`
it raises `NotImplementedError` naming the phase that will build the backend:
the seam exists so that B2.7 can fill it without touching a caller, and it is
deliberately not filled with a stand-in that could be mistaken for the real
thing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import structlog

from stock_screener.evidence import resolve_sec_evidence_dir
from stock_screener.providers import ConfigurationError
from stock_screener.sec_evidence.control import open_control_root
from stock_screener.sec_evidence.storage import open_evidence_root

if TYPE_CHECKING:
    from pydantic import SecretStr

    from stock_screener.config import Settings
    from stock_screener.sec_evidence.backend import EvidenceStorage
    from stock_screener.sec_evidence.control import EvidenceControlRoot

log = structlog.get_logger(__name__)

__all__ = [
    "B2Configuration",
    "EvidenceLayout",
    "StorageBackend",
    "StorageConfigurationError",
    "b2_configuration",
    "describe_storage",
    "open_evidence_layout",
    "open_evidence_storage",
    "storage_backend",
    "validate_storage_configuration",
]

#: Backblaze bucket names: 6 to 63 characters of letters, digits and hyphens.
#: Vendor rule, stated once, and enough to refuse a URL, a path or a blank.
_BUCKET_NAME = re.compile(r"^[A-Za-z0-9-]{6,63}$")

#: The endpoint shape Backblaze assigns: `s3.<region>.backblazeb2.com`. Used
#: only to *derive* a signing region when the host matches; any other https
#: host is accepted as configured and left to the backend to qualify.
_BACKBLAZE_HOST = re.compile(r"^s3\.(?P<region>[a-z0-9-]+)\.backblazeb2\.com$")


class StorageBackend(StrEnum):
    """Which backend holds the artifact plane."""

    LOCAL = "local"
    """A directory at `SEC_EVIDENCE_DIR`, through `LocalEvidenceStorage`."""

    B2 = "b2"
    """A Backblaze B2 bucket, through a backend that B2.7 will build."""


class StorageConfigurationError(ConfigurationError):
    """The artifact backend was selected without the configuration it needs.

    A `ConfigurationError`, so the CLI reports it as a one-line message with
    exit code 2 — "you forgot to set B2_BUCKET" is not a crash. Distinct from
    `StorageUnavailableError`, which is a backend that was configured and then
    could not answer; this is raised before any backend exists.
    """


@dataclass(frozen=True)
class B2Configuration:
    """Validated Backblaze B2 settings, with the secrets kept wrapped.

    Attributes:
        endpoint_url: The S3-compatible endpoint, `https` only.
        bucket: The bucket holding the artifact prefixes.
        key_id: The application key ID, as the S3 access key ID.
        application_key: The application key, as the S3 secret access key.
    """

    endpoint_url: str
    bucket: str
    key_id: SecretStr
    application_key: SecretStr

    @property
    def region(self) -> str | None:
        """The signing region, when the endpoint is a Backblaze-assigned host.

        `s3.us-west-004.backblazeb2.com` names `us-west-004`. Any other host
        yields None, and the backend that needs a region says so then; there is
        no separate setting to keep in step with the endpoint.
        """
        host = urlsplit(self.endpoint_url).hostname or ""
        match = _BACKBLAZE_HOST.match(host)
        return match.group("region") if match else None

    def describe(self) -> dict[str, str]:
        """What may be shown about this configuration. No values, no secrets."""
        return {
            "endpoint": "configured",
            "bucket": "configured",
            "credentials": "configured",
        }


@dataclass(frozen=True)
class EvidenceLayout:
    """Both planes, opened and ready, for one run.

    Attributes:
        backend: Which artifact backend was selected.
        storage: The artifact backend. Downstream code takes this and nothing
            more specific.
        control: The control root — manifest, checkpoints, plans, derived.
        backend_defaulted: Whether `SEC_STORAGE_BACKEND` was left unset and
            the legacy default applied. Reported so that it gets set.
    """

    backend: StorageBackend
    storage: EvidenceStorage
    control: EvidenceControlRoot
    backend_defaulted: bool


def storage_backend(settings: Settings) -> StorageBackend:
    """The selected backend, as the typed value the rest of this module uses.

    `Settings` has already refused anything outside `local` and `b2`, so this
    cannot fail; it exists so that no caller compares strings.
    """
    return StorageBackend(settings.sec_storage_backend)


def _backend_defaulted(settings: Settings) -> bool:
    """Whether the backend came from the field default rather than the environment."""
    return "sec_storage_backend" not in settings.model_fields_set


def b2_configuration(settings: Settings) -> B2Configuration:
    """Validate the B2 settings offline and return them, secrets still wrapped.

    Args:
        settings: Application settings.

    Returns:
        The validated configuration.

    Raises:
        StorageConfigurationError: Naming every missing setting at once, or the
            one that is malformed. Never quoting a credential.
    """
    missing = [
        name
        for name, value in (
            ("B2_ENDPOINT_URL", settings.b2_endpoint_url),
            ("B2_BUCKET", settings.b2_bucket),
            ("B2_KEY_ID", _secret_text(settings.b2_key_id)),
            ("B2_APPLICATION_KEY", _secret_text(settings.b2_application_key)),
        )
        if not (value or "").strip()
    ]
    if missing:
        message = (
            f"SEC_STORAGE_BACKEND=b2 requires {', '.join(missing)}; "
            "B2 configuration is incomplete and no local fallback is taken"
        )
        raise StorageConfigurationError(message)

    endpoint = (settings.b2_endpoint_url or "").strip()
    _validate_endpoint(endpoint)
    bucket = (settings.b2_bucket or "").strip()
    if not _BUCKET_NAME.match(bucket):
        message = (
            "B2_BUCKET must be a bucket name — 6 to 63 letters, digits or hyphens — "
            "not a URL or a path"
        )
        raise StorageConfigurationError(message)

    key_id, application_key = settings.b2_key_id, settings.b2_application_key
    if key_id is None or application_key is None:  # already refused above; narrows the type
        raise StorageConfigurationError("B2 credentials are incomplete")
    return B2Configuration(
        endpoint_url=endpoint,
        bucket=bucket,
        key_id=key_id,
        application_key=application_key,
    )


def _secret_text(secret: SecretStr | None) -> str:
    """The wrapped value, for presence checks only. Never returned to a caller."""
    return secret.get_secret_value() if secret is not None else ""


def _validate_endpoint(endpoint: str) -> None:
    """Refuse an endpoint that is not a bare https URL.

    Not a Backblaze-hostname check: accounts get different hosts, and a future
    one must not be refused for being unfamiliar. What is refused is anything
    that could carry a credential — a userinfo component or a query string —
    and anything that is not https at all.
    """
    parts = urlsplit(endpoint)
    if parts.scheme != "https":
        raise StorageConfigurationError("B2_ENDPOINT_URL must be an https:// URL")
    if not parts.hostname:
        raise StorageConfigurationError("B2_ENDPOINT_URL has no hostname")
    if parts.username is not None or parts.password is not None:
        raise StorageConfigurationError(
            "B2_ENDPOINT_URL must not embed credentials; supply them as B2_KEY_ID and "
            "B2_APPLICATION_KEY"
        )
    if parts.query or parts.fragment:
        raise StorageConfigurationError("B2_ENDPOINT_URL must not carry a query string or fragment")


def validate_storage_configuration(settings: Settings) -> StorageBackend:
    """Check that the selected backend has what it needs, without touching it.

    For `local` this applies the existing root rules — configured, present, a
    directory — through `resolve_sec_evidence_dir`. For `b2` it validates the
    `B2_*` settings offline. Either way the control root is not opened here;
    that is `open_evidence_layout`'s job.

    Args:
        settings: Application settings.

    Returns:
        The backend that passed.

    Raises:
        StorageConfigurationError: If `b2` is selected and misconfigured.
        EvidenceUnavailableError: If `local` is selected and the root is
            unusable, exactly as before this setting existed.
    """
    backend = storage_backend(settings)
    if backend is StorageBackend.B2:
        b2_configuration(settings)
    else:
        resolve_sec_evidence_dir(settings, allow_empty=True)
    return backend


def open_evidence_storage(settings: Settings) -> EvidenceStorage:
    """Construct the selected artifact backend.

    Args:
        settings: Application settings.

    Returns:
        `LocalEvidenceStorage` anchored at the verified `SEC_EVIDENCE_DIR`.

    Raises:
        StorageConfigurationError: If `b2` is selected and misconfigured. This
            is checked *before* the `NotImplementedError` below, so a
            misconfiguration is reported as what it is.
        NotImplementedError: If `b2` is selected and configured — the backend
            arrives in B2.7. No local stand-in is returned in its place.
        EvidenceUnavailableError: If `local` is selected and the root is
            unusable.
    """
    backend = validate_storage_configuration(settings)
    if backend is StorageBackend.B2:
        message = (
            "SEC_STORAGE_BACKEND=b2 is configured but the Backblaze backend is not "
            "implemented yet (B2.7). Nothing falls back to local storage."
        )
        raise NotImplementedError(message)
    return open_evidence_root(settings).storage


def open_evidence_layout(settings: Settings) -> EvidenceLayout:
    """Open the artifact backend and the control root for one run.

    The control root is opened for either backend and is required for both:
    a `b2` run with no `SEC_CONTROL_DIR` has nowhere to put its manifest and
    stops here, rather than anywhere else. Only the `local` backend offers its
    own root as the legacy colocated fallback.

    Args:
        settings: Application settings.

    Returns:
        Both planes, ready.

    Raises:
        StorageConfigurationError: If the selected backend is misconfigured.
        ControlPlaneUnavailableError: If the control root is missing, or unset
            with no local root to fall back to.
        NotImplementedError: If `b2` is selected — until B2.7.
        EvidenceUnavailableError: If `local` is selected and the root is
            unusable.
    """
    backend = validate_storage_configuration(settings)
    defaulted = _backend_defaulted(settings)
    if defaulted:
        log.warning(
            "SEC_STORAGE_BACKEND is unset; defaulting to local",
            hint="set it explicitly — the default exists for pre-B2.6 configurations",
        )
    if backend is StorageBackend.B2:
        # Order matters: a b2 run must have its control root before it can
        # have a backend, so a missing SEC_CONTROL_DIR is the error reported.
        control = open_control_root(settings)
        storage = open_evidence_storage(settings)
        return EvidenceLayout(backend, storage, control, defaulted)
    root = open_evidence_root(settings)
    control = open_control_root(settings, colocate_with=root)
    return EvidenceLayout(backend, root.storage, control, defaulted)


def describe_storage(settings: Settings) -> list[tuple[str, str]]:
    """Safe status lines for the artifact backend. Never a value that is secret.

    Does not construct a backend and does not open a root — status is for
    finding out what is wrong, and must work when something is.

    Args:
        settings: Application settings.

    Returns:
        `(label, text)` pairs in display order.
    """
    backend = storage_backend(settings)
    lines: list[tuple[str, str]] = []
    if _backend_defaulted(settings):
        lines.append(("artifact backend", f"{backend} (defaulted — set SEC_STORAGE_BACKEND)"))
    else:
        lines.append(("artifact backend", str(backend)))
    if backend is StorageBackend.LOCAL:
        return lines
    try:
        config = b2_configuration(settings)
    except StorageConfigurationError as exc:
        present = {
            "endpoint": bool((settings.b2_endpoint_url or "").strip()),
            "bucket": bool((settings.b2_bucket or "").strip()),
            "credentials": bool(
                _secret_text(settings.b2_key_id).strip()
                and _secret_text(settings.b2_application_key).strip()
            ),
        }
        lines.extend((name, "configured" if ok else "MISSING") for name, ok in present.items())
        lines.append(("b2 state", f"INVALID — {exc}"))
        return lines
    lines.extend(config.describe().items())
    lines.append(("b2 state", "configured (offline check only; backend arrives in B2.7)"))
    return lines
