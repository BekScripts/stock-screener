"""Choosing the artifact backend from configuration, and keeping the secrets in.

Phase B2.6 adds the setting that says *which* backend holds the artifact
plane and the Backblaze settings a future backend will need. Nothing here
talks to Backblaze — there is no backend to talk with yet, and validation is
deliberately offline: fields present, shapes sane, combination coherent.

Three things are proved:

- **Selection is explicit and fails closed.** Credentials do not choose `b2`;
  their absence does not choose `local`; a `b2` run with incomplete
  configuration stops and is never served by a local directory instead.
- **Secrets never leave the settings.** The application key does not appear
  in a `repr`, an error message, a log line or status output, whatever went
  wrong.
- **The control plane is still required.** A `b2` run with no
  `SEC_CONTROL_DIR` has nowhere to put its manifest and says so.

The B2.5 legacy layout — `local`, evidence root set, control root unset — is
tested again here under the new selector so that it cannot regress silently.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from stock_screener.cli import app
from stock_screener.config import Settings, get_settings
from stock_screener.evidence import EvidenceUnavailableError
from stock_screener.providers import ConfigurationError
from stock_screener.sec_evidence import (
    B2Configuration,
    ControlPlaneUnavailableError,
    LocalEvidenceStorage,
    StorageBackend,
    StorageConfigurationError,
    b2_configuration,
    open_evidence_layout,
    open_evidence_storage,
    storage_backend,
    validate_storage_configuration,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration

APPLICATION_KEY = "K004superSecretApplicationKeyValueThatMustNeverAppear"
KEY_ID = "0041234567890abcdef0000000001"
ENDPOINT = "https://s3.us-west-004.backblazeb2.com"
BUCKET = "compounder-radar-evidence"


def b2_settings(control_dir: Path | None = None, **overrides: object) -> Settings:
    """A complete, valid B2 configuration; override one field to break it."""
    fields: dict[str, object] = {
        "sec_storage_backend": "b2",
        "b2_endpoint_url": ENDPOINT,
        "b2_bucket": BUCKET,
        "b2_key_id": KEY_ID,
        "b2_application_key": APPLICATION_KEY,
        "sec_control_dir": str(control_dir) if control_dir is not None else None,
    }
    fields.update(overrides)
    return Settings(**fields)  # type: ignore[arg-type]


@pytest.fixture
def artifact_root(tmp_path: Path) -> Path:
    root = tmp_path / "volume" / "sec"
    root.mkdir(parents=True)
    return root


@pytest.fixture
def control_dir(tmp_path: Path) -> Path:
    root = tmp_path / "boot" / "control"
    root.mkdir(parents=True)
    return root


class TestBackendSelectionIsExplicit:
    def test_the_selector_defaults_to_local_for_configurations_that_predate_it(self) -> None:
        settings = Settings()

        assert storage_backend(settings) is StorageBackend.LOCAL
        assert "sec_storage_backend" not in settings.model_fields_set

    def test_an_explicit_selector_is_recorded_as_explicit(self) -> None:
        settings = Settings(sec_storage_backend="local")

        assert storage_backend(settings) is StorageBackend.LOCAL
        assert "sec_storage_backend" in settings.model_fields_set

    def test_an_unknown_backend_is_refused_by_settings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SEC_STORAGE_BACKEND", "foo")

        with pytest.raises(ValueError, match="sec_storage_backend"):
            Settings()

    def test_credentials_alone_do_not_select_b2(self, artifact_root: Path) -> None:
        """Every B2 value present, selector unset: still local, still the root."""
        settings = Settings(
            sec_evidence_dir=str(artifact_root),
            b2_endpoint_url=ENDPOINT,
            b2_bucket=BUCKET,
            b2_key_id=SecretStr(KEY_ID),
            b2_application_key=SecretStr(APPLICATION_KEY),
        )

        assert validate_storage_configuration(settings) is StorageBackend.LOCAL
        assert isinstance(open_evidence_storage(settings), LocalEvidenceStorage)

    def test_a_local_root_present_does_not_pull_a_b2_run_back_to_local(
        self, artifact_root: Path, control_dir: Path
    ) -> None:
        """b2 selected, a perfectly good local root configured: the root is not used."""
        settings = b2_settings(control_dir, sec_evidence_dir=str(artifact_root))

        assert validate_storage_configuration(settings) is StorageBackend.B2
        with pytest.raises(NotImplementedError, match="Nothing falls back to local"):
            open_evidence_storage(settings)
        assert list(artifact_root.iterdir()) == [], "the local root was not even touched"


class TestLocalConfiguration:
    def test_valid_local_with_separate_control_root(
        self, artifact_root: Path, control_dir: Path
    ) -> None:
        settings = Settings(
            sec_storage_backend="local",
            sec_evidence_dir=str(artifact_root),
            sec_control_dir=str(control_dir),
        )

        layout = open_evidence_layout(settings)

        assert layout.backend is StorageBackend.LOCAL
        assert isinstance(layout.storage, LocalEvidenceStorage)
        assert layout.storage.root == artifact_root
        assert layout.control.path == control_dir
        assert not layout.control.colocated
        assert not layout.backend_defaulted

    def test_legacy_local_with_control_unset_is_colocated_and_flagged(
        self, artifact_root: Path
    ) -> None:
        """The B2.5 compatibility policy, unchanged by the selector."""
        settings = Settings(sec_evidence_dir=str(artifact_root))

        layout = open_evidence_layout(settings)

        assert layout.backend is StorageBackend.LOCAL
        assert layout.control.colocated
        assert layout.control.path == artifact_root
        assert layout.backend_defaulted

    def test_a_missing_local_root_fails_exactly_as_before(self, tmp_path: Path) -> None:
        absent = tmp_path / "Volumes" / "GONE" / "sec"
        settings = Settings(sec_storage_backend="local", sec_evidence_dir=str(absent))

        with pytest.raises(EvidenceUnavailableError):
            validate_storage_configuration(settings)
        with pytest.raises(EvidenceUnavailableError):
            open_evidence_layout(settings)

        assert not (tmp_path / "Volumes").exists()

    def test_an_unconfigured_local_root_fails(self) -> None:
        with pytest.raises(EvidenceUnavailableError, match="SEC_EVIDENCE_DIR"):
            validate_storage_configuration(Settings(sec_storage_backend="local"))


class TestB2ConfigurationValidatesOffline:
    def test_a_complete_configuration_passes_without_any_network(self, control_dir: Path) -> None:
        settings = b2_settings(control_dir)

        backend = validate_storage_configuration(settings)
        config = b2_configuration(settings)

        assert backend is StorageBackend.B2
        assert config.endpoint_url == ENDPOINT
        assert config.bucket == BUCKET
        assert config.region == "us-west-004"
        assert config.key_id.get_secret_value() == KEY_ID
        assert config.application_key.get_secret_value() == APPLICATION_KEY

    @pytest.mark.parametrize(
        ("field", "name"),
        [
            ("b2_endpoint_url", "B2_ENDPOINT_URL"),
            ("b2_bucket", "B2_BUCKET"),
            ("b2_key_id", "B2_KEY_ID"),
            ("b2_application_key", "B2_APPLICATION_KEY"),
        ],
    )
    def test_each_missing_field_is_named(self, control_dir: Path, field: str, name: str) -> None:
        settings = b2_settings(control_dir, **{field: None})

        with pytest.raises(StorageConfigurationError, match=name):
            validate_storage_configuration(settings)

    @pytest.mark.parametrize("field", ["b2_endpoint_url", "b2_bucket", "b2_key_id"])
    def test_a_blank_field_counts_as_missing(self, control_dir: Path, field: str) -> None:
        settings = b2_settings(control_dir, **{field: "   "})

        with pytest.raises(StorageConfigurationError, match="incomplete"):
            b2_configuration(settings)

    def test_every_missing_field_is_reported_at_once(self, control_dir: Path) -> None:
        settings = b2_settings(control_dir, b2_bucket=None, b2_application_key=None)

        with pytest.raises(StorageConfigurationError) as caught:
            b2_configuration(settings)

        assert "B2_BUCKET" in str(caught.value)
        assert "B2_APPLICATION_KEY" in str(caught.value)
        assert "B2_ENDPOINT_URL" not in str(caught.value)

    @pytest.mark.parametrize(
        ("endpoint", "reason"),
        [
            ("http://s3.us-west-004.backblazeb2.com", "https"),
            ("s3.us-west-004.backblazeb2.com", "https"),
            ("https://", "hostname"),
            ("https://user:pass@s3.us-west-004.backblazeb2.com", "embed credentials"),
            ("https://s3.us-west-004.backblazeb2.com/?X-Amz-Credential=abc", "query string"),
            ("https://s3.us-west-004.backblazeb2.com/#token", "query string"),
        ],
    )
    def test_an_endpoint_that_is_not_a_bare_https_url_is_refused(
        self, control_dir: Path, endpoint: str, reason: str
    ) -> None:
        settings = b2_settings(control_dir, b2_endpoint_url=endpoint)

        with pytest.raises(StorageConfigurationError, match=reason):
            b2_configuration(settings)

    def test_an_unfamiliar_https_host_is_accepted_and_has_no_derived_region(
        self, control_dir: Path
    ) -> None:
        """No Backblaze hostname is hardcoded; a future account's endpoint is fine."""
        settings = b2_settings(control_dir, b2_endpoint_url="https://s3.example.net")

        config = b2_configuration(settings)

        assert config.endpoint_url == "https://s3.example.net"
        assert config.region is None

    @pytest.mark.parametrize(
        "bucket",
        [
            "https://s3.us-west-004.backblazeb2.com/compounder-radar-evidence",
            "compounder/radar",
            "short",
            "has space",
            "dotted.name",
            "x" * 64,
        ],
    )
    def test_a_bucket_that_is_not_a_bucket_name_is_refused(
        self, control_dir: Path, bucket: str
    ) -> None:
        settings = b2_settings(control_dir, b2_bucket=bucket)

        with pytest.raises(StorageConfigurationError, match="bucket name"):
            b2_configuration(settings)


class TestB2NeverFallsBackToLocal:
    def test_incomplete_b2_with_a_local_root_available_still_fails(
        self, artifact_root: Path, control_dir: Path
    ) -> None:
        """The exact mistake to refuse: b2 half-configured, a local root waiting."""
        settings = b2_settings(
            control_dir, b2_application_key=None, sec_evidence_dir=str(artifact_root)
        )

        with pytest.raises(StorageConfigurationError, match="no local fallback"):
            open_evidence_layout(settings)
        with pytest.raises(StorageConfigurationError):
            open_evidence_storage(settings)

        assert list(artifact_root.iterdir()) == []
        assert not (control_dir / "manifests").exists(), "nothing was opened before the refusal"

    def test_a_configured_b2_run_reaches_the_seam_and_stops_there(self, control_dir: Path) -> None:
        """Valid configuration, control root present: the only thing missing is B2.7."""
        settings = b2_settings(control_dir)

        with pytest.raises(NotImplementedError, match=r"B2\.7"):
            open_evidence_layout(settings)

        assert (control_dir / "manifests").is_dir(), "the control root was opened first"

    def test_a_storage_configuration_error_is_a_configuration_error(self) -> None:
        """So the CLI reports it in one line with exit 2, like every other misconfiguration."""
        assert issubclass(StorageConfigurationError, ConfigurationError)


class TestB2StillRequiresTheControlRoot:
    def test_b2_with_no_control_root_fails_before_any_backend(self) -> None:
        settings = b2_settings(None)

        with pytest.raises(ControlPlaneUnavailableError, match="SEC_CONTROL_DIR is unset"):
            open_evidence_layout(settings)

    def test_b2_with_a_missing_control_root_fails_and_creates_nothing(self, tmp_path: Path) -> None:
        absent = tmp_path / "boot" / "never-made"
        settings = b2_settings(absent)

        with pytest.raises(ControlPlaneUnavailableError, match="never created"):
            open_evidence_layout(settings)

        assert not absent.exists()

    def test_b2_never_colocates_with_a_local_root(self, artifact_root: Path) -> None:
        """The legacy fallback belongs to the local backend alone."""
        settings = b2_settings(None, sec_evidence_dir=str(artifact_root))

        with pytest.raises(ControlPlaneUnavailableError):
            open_evidence_layout(settings)

        assert not (artifact_root / "manifests").exists()


class TestSecretsStayIn:
    def test_the_settings_repr_hides_both_credentials(self) -> None:
        settings = b2_settings(None)

        rendered = repr(settings) + str(settings)

        assert APPLICATION_KEY not in rendered
        assert KEY_ID not in rendered

    def test_the_configuration_repr_and_description_hide_both_credentials(
        self, control_dir: Path
    ) -> None:
        config = b2_configuration(b2_settings(control_dir))

        rendered = repr(config) + str(config) + str(config.describe())

        assert APPLICATION_KEY not in rendered
        assert KEY_ID not in rendered
        assert config.describe() == {
            "endpoint": "configured",
            "bucket": "configured",
            "credentials": "configured",
        }

    @pytest.mark.parametrize(
        "broken",
        [
            {"b2_bucket": None},
            {"b2_endpoint_url": "http://insecure.example.net"},
            {"b2_bucket": "not a bucket"},
            {"b2_endpoint_url": f"https://{KEY_ID}:{APPLICATION_KEY}@s3.example.net"},
        ],
    )
    def test_no_error_message_quotes_a_credential(
        self, control_dir: Path, broken: dict[str, object]
    ) -> None:
        settings = b2_settings(control_dir, **broken)

        with pytest.raises(StorageConfigurationError) as caught:
            b2_configuration(settings)

        assert APPLICATION_KEY not in str(caught.value)
        assert KEY_ID not in str(caught.value)

    def test_no_log_line_carries_a_credential(
        self, control_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        settings = b2_settings(control_dir, b2_bucket=None)
        with caplog.at_level(logging.DEBUG), pytest.raises(StorageConfigurationError):
            open_evidence_layout(settings)

        assert APPLICATION_KEY not in caplog.text
        assert KEY_ID not in caplog.text

    def test_a_secret_str_is_the_only_way_a_credential_is_held(self) -> None:
        config = B2Configuration(
            endpoint_url=ENDPOINT,
            bucket=BUCKET,
            key_id=SecretStr(KEY_ID),
            application_key=SecretStr(APPLICATION_KEY),
        )

        assert isinstance(config.application_key, SecretStr)
        assert isinstance(config.key_id, SecretStr)
        assert "**********" in repr(config)


class TestStatusReportsTheBackendWithoutSecrets:
    @pytest.fixture
    def environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ENVIRONMENT", "test")
        get_settings.cache_clear()

    def test_local_separated(
        self,
        environment: None,
        artifact_root: Path,
        control_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("SEC_STORAGE_BACKEND", "local")
        monkeypatch.setenv("SEC_EVIDENCE_DIR", str(artifact_root))
        monkeypatch.setenv("SEC_CONTROL_DIR", str(control_dir))
        get_settings.cache_clear()

        result = CliRunner().invoke(app, ["evidence", "status"])

        assert result.exit_code == 0
        assert "artifact backend : local" in result.stdout
        assert "defaulted" not in result.stdout
        assert f"artifact root    : {artifact_root}" in result.stdout
        assert f"control root     : {control_dir}" in result.stdout
        get_settings.cache_clear()

    def test_local_legacy_colocated_reports_the_default_and_the_shared_domain(
        self, environment: None, artifact_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SEC_EVIDENCE_DIR", str(artifact_root))
        get_settings.cache_clear()

        result = CliRunner().invoke(app, ["evidence", "status"])

        assert result.exit_code == 0
        assert "artifact backend : local (defaulted — set SEC_STORAGE_BACKEND)" in result.stdout
        assert "LEGACY" in result.stdout
        assert "shared failure domain" in result.stdout
        get_settings.cache_clear()

    def test_b2_configured_reports_only_safe_state(
        self, environment: None, control_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SEC_STORAGE_BACKEND", "b2")
        monkeypatch.setenv("SEC_CONTROL_DIR", str(control_dir))
        monkeypatch.setenv("B2_ENDPOINT_URL", ENDPOINT)
        monkeypatch.setenv("B2_BUCKET", BUCKET)
        monkeypatch.setenv("B2_KEY_ID", KEY_ID)
        monkeypatch.setenv("B2_APPLICATION_KEY", APPLICATION_KEY)
        get_settings.cache_clear()

        result = CliRunner().invoke(app, ["evidence", "status"])

        assert result.exit_code == 0
        assert "artifact backend : b2" in result.stdout
        assert "endpoint         : configured" in result.stdout
        assert "bucket           : configured" in result.stdout
        assert "credentials      : configured" in result.stdout
        assert f"control root     : {control_dir}" in result.stdout
        assert "artifact root" not in result.stdout, "the local root is not the backend"
        assert "LEGACY" not in result.stdout
        assert APPLICATION_KEY not in result.stdout
        assert KEY_ID not in result.stdout
        assert BUCKET not in result.stdout
        assert ENDPOINT not in result.stdout
        get_settings.cache_clear()

    def test_b2_missing_config_reports_what_is_missing_and_exits_non_zero(
        self, environment: None, control_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SEC_STORAGE_BACKEND", "b2")
        monkeypatch.setenv("SEC_CONTROL_DIR", str(control_dir))
        monkeypatch.setenv("B2_ENDPOINT_URL", ENDPOINT)
        monkeypatch.setenv("B2_KEY_ID", KEY_ID)
        monkeypatch.setenv("B2_APPLICATION_KEY", APPLICATION_KEY)
        get_settings.cache_clear()

        result = CliRunner().invoke(app, ["evidence", "status"])

        assert result.exit_code == 1
        assert "bucket           : MISSING" in result.stdout
        assert "credentials      : configured" in result.stdout
        assert "B2_BUCKET" in result.stdout
        assert APPLICATION_KEY not in result.stdout
        assert KEY_ID not in result.stdout
        get_settings.cache_clear()

    def test_b2_without_a_control_root_exits_non_zero(
        self, environment: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SEC_STORAGE_BACKEND", "b2")
        monkeypatch.setenv("B2_ENDPOINT_URL", ENDPOINT)
        monkeypatch.setenv("B2_BUCKET", BUCKET)
        monkeypatch.setenv("B2_KEY_ID", KEY_ID)
        monkeypatch.setenv("B2_APPLICATION_KEY", APPLICATION_KEY)
        get_settings.cache_clear()

        result = CliRunner().invoke(app, ["evidence", "status"])

        assert result.exit_code == 1
        assert "requires SEC_CONTROL_DIR" in result.stdout
        get_settings.cache_clear()

    def test_a_b2_harvest_stops_with_one_line_when_misconfigured(
        self, environment: None, control_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A runtime command hits the resolver before anything else."""
        monkeypatch.setenv("SEC_STORAGE_BACKEND", "b2")
        monkeypatch.setenv("SEC_CONTROL_DIR", str(control_dir))
        monkeypatch.setenv("B2_APPLICATION_KEY", APPLICATION_KEY)
        get_settings.cache_clear()

        result = CliRunner().invoke(app, ["evidence", "harvest", "--dry-run", "--limit", "1"])

        assert result.exit_code != 0
        assert isinstance(result.exception, StorageConfigurationError)
        assert APPLICATION_KEY not in result.stdout
        assert APPLICATION_KEY not in str(result.exception)
        get_settings.cache_clear()
