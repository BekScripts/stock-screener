"""The canary is proven offline before it is allowed near a bucket.

A live canary that had a bug in its own checks would either pass a broken
backend or fail a working one, and against a real bucket either mistake
costs a request to find. So the whole flow — namespace, dataset, every step,
cleanup and its refusals — runs here first against the stateful fake client,
where a passing run and each way of failing can be produced on demand.

Nothing here reaches a network. The only wiring that is real is the wiring:
`run_live_canary` builds its backend through `open_evidence_layout` exactly
as the command does, with `build_b2_client` replaced by the fake.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING, Any

import pytest
from botocore.exceptions import EndpointConnectionError
from pydantic import SecretStr
from typer.testing import CliRunner

from stock_screener.cli import app
from stock_screener.config import Settings, get_settings
from stock_screener.sec_evidence import (
    B2Configuration,
    B2EvidenceStorage,
    CanaryError,
    CanaryNamespace,
    ControlPlaneUnavailableError,
    StorageUnavailableError,
    UnsafeStorageKeyError,
    delete_canary_prefix,
    open_control_root,
    run_canary,
    run_live_canary,
)
from stock_screener.sec_evidence.canary import (
    CANARY_ROOT,
    HARVEST_PAYLOAD,
    canary_dataset,
    new_run_id,
    validate_cleanup_prefix,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from conftest import FakeS3Client

pytestmark = pytest.mark.unit

ENDPOINT = "https://s3.us-west-004.backblazeb2.com"
BUCKET = "canary-unit-bucket"
KEY_ID = "0041234567890abcdef0000000001"
APPLICATION_KEY = "K004canarySentinelApplicationKeyNeverToAppear"
CONFIG = B2Configuration(
    endpoint_url=ENDPOINT,
    bucket=BUCKET,
    key_id=SecretStr(KEY_ID),
    application_key=SecretStr(APPLICATION_KEY),
)
RUN = "b28-20260915T000000Z-cafef00d"


@pytest.fixture
def control_dir(tmp_path: Path) -> Path:
    root = tmp_path / "control"
    root.mkdir()
    return root


def fake_cleanup(client: FakeS3Client):  # type: ignore[no-untyped-def]  # a test helper
    """A cleanup that deletes from the fake, through the same guarded helper."""

    def cleanup(prefix: str) -> int:
        return delete_canary_prefix(client, BUCKET, prefix)  # type: ignore[arg-type]

    return cleanup


class TestTheNamespaceConfinesEveryKey:
    def test_every_operation_targets_the_prefixed_key(self, fake_s3: type[FakeS3Client]) -> None:
        client = fake_s3()
        namespace = CanaryNamespace(B2EvidenceStorage(CONFIG, client=client), RUN)
        key = "filings/0000000001/000000000124000002/a.txt"

        stored = namespace.write_bytes(key, b"x")

        assert list(client.objects) == [f"{CANARY_ROOT}/{RUN}/{key}"]
        assert stored.key == key, "the receipt names the logical key"
        assert namespace.stat(key).key == key
        assert namespace.exists(key)
        assert namespace.read_bytes(key) == b"x"
        assert namespace.digest(key) == hashlib.sha256(b"x").hexdigest()

    def test_listing_returns_logical_keys_and_only_this_runs(
        self, fake_s3: type[FakeS3Client]
    ) -> None:
        client = fake_s3()
        namespace = CanaryNamespace(B2EvidenceStorage(CONFIG, client=client), RUN)
        namespace.write_bytes("filings/0000000001/x/a.txt", b"a")
        client.objects[f"{CANARY_ROOT}/other-run/filings/0000000001/x/b.txt"] = (b"b", {})
        client.objects["filings/0000000001/x/production.htm"] = (b"p", {})

        assert list(namespace.list_prefix("")) == ["filings/0000000001/x/a.txt"]
        assert list(namespace.list_prefix("filings")) == ["filings/0000000001/x/a.txt"]

    @pytest.mark.parametrize("unsafe", ["../escape", "/abs", "filings//x", "filings\\x", ""])
    def test_an_unsafe_logical_key_never_reaches_the_backend(
        self, fake_s3: type[FakeS3Client], unsafe: str
    ) -> None:
        client = fake_s3()
        namespace = CanaryNamespace(B2EvidenceStorage(CONFIG, client=client), RUN)

        with pytest.raises(UnsafeStorageKeyError):
            namespace.write_bytes(unsafe, b"x")
        with pytest.raises(UnsafeStorageKeyError):
            namespace.exists(unsafe)

        assert client.calls == []

    def test_a_run_id_that_would_escape_is_refused(self, fake_s3: type[FakeS3Client]) -> None:
        with pytest.raises(UnsafeStorageKeyError):
            CanaryNamespace(B2EvidenceStorage(CONFIG, client=fake_s3()), "../filings")

    def test_run_ids_are_unique_and_canary_shaped(self) -> None:
        first, second = new_run_id(), new_run_id()

        assert first != second
        assert first.startswith("b28-")
        assert "/" not in first


class TestTheDatasetIsSmallAndDeterministic:
    def test_shapes_and_bound(self) -> None:
        dataset = canary_dataset()

        sizes = sorted(len(obj.payload) for obj in dataset)
        assert 0 in sizes, "an empty object is part of the set"
        assert max(sizes) == 2 * 1024 * 1024
        assert sum(sizes) < 20 * 1024 * 1024
        assert {obj.key.split("/", 1)[0] for obj in dataset} == {
            "filings",
            "submissions",
            "fulltext",
        }
        assert canary_dataset() == dataset, "the same bytes every time"


class TestARunAgainstAWorkingBackendPasses:
    def test_every_step_holds_and_cleanup_empties_the_prefix(
        self, fake_s3: type[FakeS3Client], control_dir: Path
    ) -> None:
        client = fake_s3()
        storage = B2EvidenceStorage(CONFIG, client=client)
        control = open_control_root(Settings(sec_control_dir=str(control_dir)))
        written: list[str] = []
        original_put = client.put_object

        def recording_put(**kwargs: object) -> dict[str, object]:
            written.append(str(kwargs["Key"]))
            return original_put(**kwargs)  # type: ignore[arg-type]

        client.put_object = recording_put  # type: ignore[method-assign]

        report = run_canary(storage, control, run_id=RUN, cleanup=fake_cleanup(client))

        assert report.passed, report.failure
        assert [s.name for s in report.steps if not s.ok] == []
        assert report.prefix == f"{CANARY_ROOT}/{RUN}"
        assert written, "the canary wrote"
        assert all(key.startswith(f"{CANARY_ROOT}/{RUN}/") for key in written), written
        assert not any(key.startswith(("filings/", "submissions/", "fulltext/")) for key in written)
        assert report.cleanup.startswith("deleted 7 objects")
        assert report.remaining == []
        assert client.objects == {}, "nothing left in the bucket"
        assert report.bytes_written == sum(len(o.payload) for o in canary_dataset()) + len(
            HARVEST_PAYLOAD
        ) + len(b"nobody vouches for this")
        assert (control_dir / "manifests" / "evidence.jsonl").is_file()
        assert not any(op == "delete_object" for op, _ in client.calls)

    def test_the_step_names_cover_the_whole_matrix(
        self, fake_s3: type[FakeS3Client], control_dir: Path
    ) -> None:
        client = fake_s3()
        control = open_control_root(Settings(sec_control_dir=str(control_dir)))

        report = run_canary(B2EvidenceStorage(CONFIG, client=client), control, run_id=RUN)

        names = [s.name for s in report.steps]
        assert names[:3] == [
            "preflight: list empty run prefix",
            "absence: unknown key",
            "unsafe keys refused offline",
        ]
        assert sum(n.startswith("write ") for n in names) == 5
        assert sum(n.startswith("verify ") for n in names) == 5
        assert names[-6:] == [
            "list_prefix",
            "harvest: write then record",
            "replay: cache hit",
            "reconcile: clean",
            "reconcile: orphan object",
            "reconcile: missing object",
        ]
        assert report.passed

    def test_without_a_cleanup_the_objects_are_retained_and_listed(
        self, fake_s3: type[FakeS3Client], control_dir: Path
    ) -> None:
        client = fake_s3()
        control = open_control_root(Settings(sec_control_dir=str(control_dir)))

        report = run_canary(B2EvidenceStorage(CONFIG, client=client), control, run_id=RUN)

        assert report.passed
        assert report.cleanup.startswith("skipped")
        assert len(report.remaining) == 7
        assert len(client.objects) == 7

    def test_a_cleanup_the_bucket_refuses_is_reported_as_retained_not_failed(
        self, fake_s3: type[FakeS3Client], control_dir: Path
    ) -> None:
        client = fake_s3()
        control = open_control_root(Settings(sec_control_dir=str(control_dir)))

        def refused(prefix: str) -> int:
            raise StorageUnavailableError("cleanup of prefix refused: AccessDenied")

        report = run_canary(
            B2EvidenceStorage(CONFIG, client=client), control, run_id=RUN, cleanup=refused
        )

        assert report.passed
        assert report.cleanup.startswith("retained")
        assert len(report.remaining) == 7


class TestARunAgainstABrokenBackendFails:
    def test_a_truncated_upload_stops_the_run_and_still_cleans_up(
        self, fake_s3: type[FakeS3Client], control_dir: Path
    ) -> None:
        client = fake_s3()
        first = canary_dataset()[0]
        client.tamper_after_put[f"{CANARY_ROOT}/{RUN}/{first.key}"] = (
            first.payload[:3],
            {"sha256": first.sha256},
        )
        control = open_control_root(Settings(sec_control_dir=str(control_dir)))

        report = run_canary(
            B2EvidenceStorage(CONFIG, client=client),
            control,
            run_id=RUN,
            cleanup=fake_cleanup(client),
        )

        assert not report.passed
        assert report.failure is not None
        assert report.failure.startswith(f"write {first.key}")
        assert "did not verify" in report.failure
        assert client.objects == {}, "cleanup ran even though the run failed"

    def test_a_bucket_that_lies_about_bytes_is_caught_on_read_back(
        self, fake_s3: type[FakeS3Client], control_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PUT and HEAD agree; the GET body differs. The read-back check catches it."""
        client = fake_s3()
        first = canary_dataset()[0]
        physical = f"{CANARY_ROOT}/{RUN}/{first.key}"
        original_get = client.get_object

        def lying_get(**kwargs: object) -> dict[str, object]:
            response = original_get(**kwargs)  # type: ignore[arg-type]
            if kwargs["Key"] == physical:
                import io

                from botocore.response import StreamingBody

                body = b"X" * len(first.payload)
                response["Body"] = StreamingBody(io.BytesIO(body), len(body))
            return response

        monkeypatch.setattr(client, "get_object", lying_get)
        control = open_control_root(Settings(sec_control_dir=str(control_dir)))

        report = run_canary(B2EvidenceStorage(CONFIG, client=client), control, run_id=RUN)

        assert not report.passed
        assert report.failure is not None
        assert report.failure.startswith(f"verify {first.key}")
        assert "downloaded bytes differ" in report.failure

    def test_a_dead_endpoint_fails_preflight_before_any_write(
        self, fake_s3: type[FakeS3Client], control_dir: Path
    ) -> None:
        client = fake_s3(
            failing={"list_objects_v2": EndpointConnectionError(endpoint_url=ENDPOINT)}
        )
        control = open_control_root(Settings(sec_control_dir=str(control_dir)))

        report = run_canary(B2EvidenceStorage(CONFIG, client=client), control, run_id=RUN)

        assert not report.passed
        assert report.steps[0].name == "preflight: list empty run prefix"
        assert not report.steps[0].ok
        assert not any(op == "put_object" for op, _ in client.calls)

    def test_a_prefix_that_is_not_empty_stops_before_any_write(
        self, fake_s3: type[FakeS3Client], control_dir: Path
    ) -> None:
        client = fake_s3()
        client.objects[f"{CANARY_ROOT}/{RUN}/filings/0000000001/x/leftover.htm"] = (b"?", {})
        control = open_control_root(Settings(sec_control_dir=str(control_dir)))

        report = run_canary(B2EvidenceStorage(CONFIG, client=client), control, run_id=RUN)

        assert not report.passed
        assert "not empty" in (report.failure or "")
        assert not any(op == "put_object" for op, _ in client.calls)


class TestCleanupRefusesEverythingButOneRun:
    @pytest.mark.parametrize(
        "prefix",
        [
            "",
            "/",
            "filings/",
            "submissions/",
            "fulltext/",
            "_canary/",
            "_canary",
            "_canary/a/b/",
            "other/x/",
        ],
    )
    def test_a_prefix_that_is_not_one_canary_run_is_refused(self, prefix: str) -> None:
        with pytest.raises(CanaryError):
            validate_cleanup_prefix(prefix)

    def test_exactly_one_run_is_accepted_with_its_separator(self) -> None:
        assert validate_cleanup_prefix(f"_canary/{RUN}") == f"_canary/{RUN}/"
        assert validate_cleanup_prefix(f"_canary/{RUN}/") == f"_canary/{RUN}/"

    def test_deletion_touches_only_the_run(self, fake_s3: type[FakeS3Client]) -> None:
        client = fake_s3()
        client.objects[f"_canary/{RUN}/filings/0000000001/x/a.txt"] = (b"a", {})
        client.objects[f"_canary/{RUN}/submissions/CIK0000000001.json"] = (b"", {})
        client.objects["_canary/another-run/filings/0000000001/x/a.txt"] = (b"a", {})
        client.objects["filings/0000000001/x/production.htm"] = (b"p", {})

        deleted = delete_canary_prefix(client, BUCKET, f"_canary/{RUN}/")  # type: ignore[arg-type]

        assert deleted == 2
        assert sorted(client.objects) == [
            "_canary/another-run/filings/0000000001/x/a.txt",
            "filings/0000000001/x/production.htm",
        ]

    def test_deletion_refuses_a_production_prefix_without_listing(
        self, fake_s3: type[FakeS3Client]
    ) -> None:
        client = fake_s3()

        with pytest.raises(CanaryError, match="production"):
            delete_canary_prefix(client, BUCKET, "filings/")  # type: ignore[arg-type]

        assert client.calls == []

    def test_a_bucket_that_refuses_deletion_is_reported_as_unavailable(
        self, fake_s3: type[FakeS3Client]
    ) -> None:
        from botocore.exceptions import ClientError

        response: dict[str, Any] = {
            "Error": {"Code": "AccessDenied", "Message": "x"},
            "ResponseMetadata": {"HTTPStatusCode": 403},
        }
        refused = ClientError(response, "DeleteObjects")  # type: ignore[arg-type]
        client = fake_s3(failing={"delete_objects": refused})
        client.objects[f"_canary/{RUN}/filings/0000000001/x/a.txt"] = (b"a", {})

        with pytest.raises(StorageUnavailableError, match="AccessDenied"):
            delete_canary_prefix(client, BUCKET, f"_canary/{RUN}/")  # type: ignore[arg-type]

        assert len(client.objects) == 1


class TestLiveWiringOffline:
    """`run_live_canary` through the real resolver, with the client faked."""

    @pytest.fixture
    def wired(self, fake_s3: type[FakeS3Client], monkeypatch: pytest.MonkeyPatch) -> FakeS3Client:
        client = fake_s3()
        monkeypatch.setattr(
            "stock_screener.sec_evidence.wiring.B2EvidenceStorage",
            lambda config: B2EvidenceStorage(config, client=client),
        )
        monkeypatch.setattr(
            "stock_screener.sec_evidence.canary.build_b2_client", lambda config: client
        )
        return client

    def settings(self, control_dir: Path | None = None) -> Settings:
        return Settings(
            sec_storage_backend="b2",
            sec_control_dir=str(control_dir) if control_dir else None,
            b2_endpoint_url=ENDPOINT,
            b2_bucket=BUCKET,
            b2_key_id=SecretStr(KEY_ID),
            b2_application_key=SecretStr(APPLICATION_KEY),
        )

    def test_the_canary_runs_through_open_evidence_layout(
        self, wired: FakeS3Client, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        control_dir = tmp_path / "canary-control"
        control_dir.mkdir()
        with caplog.at_level(logging.DEBUG):
            report = run_live_canary(self.settings(), control_dir=control_dir)

        assert report.passed, report.failure
        assert report.control_root == str(control_dir)
        assert (control_dir / "manifests" / "evidence.jsonl").is_file()
        assert wired.objects == {}
        rendered = "\n".join(report.summary()) + caplog.text
        assert APPLICATION_KEY not in rendered
        assert KEY_ID not in rendered

    def test_a_local_backend_is_refused(self, tmp_path: Path) -> None:
        root = tmp_path / "sec"
        root.mkdir()
        settings = Settings(sec_storage_backend="local", sec_evidence_dir=str(root))

        with pytest.raises(Exception, match="SEC_STORAGE_BACKEND=b2"):
            run_live_canary(settings, control_dir=tmp_path)

    def test_the_production_control_root_is_never_the_canarys(
        self, wired: FakeS3Client, tmp_path: Path
    ) -> None:
        production = tmp_path / "production-control"
        production.mkdir()
        canary = tmp_path / "canary-control"
        canary.mkdir()

        report = run_live_canary(self.settings(production), control_dir=canary)

        assert report.passed
        assert not (production / "manifests").exists(), "the production control root was not opened"
        assert (canary / "manifests" / "evidence.jsonl").is_file()

    def test_a_missing_canary_control_dir_fails_loud(
        self, wired: FakeS3Client, tmp_path: Path
    ) -> None:
        with pytest.raises(ControlPlaneUnavailableError):
            run_live_canary(self.settings(), control_dir=tmp_path / "never-made")

        assert wired.calls == [], "nothing was sent before the control root check"


class TestTheCommand:
    @pytest.fixture
    def environment(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
        control = tmp_path / "control"
        control.mkdir()
        monkeypatch.setenv("ENVIRONMENT", "test")
        monkeypatch.setenv("SEC_STORAGE_BACKEND", "b2")
        monkeypatch.setenv("SEC_CONTROL_DIR", str(control))
        monkeypatch.setenv("B2_ENDPOINT_URL", ENDPOINT)
        monkeypatch.setenv("B2_BUCKET", BUCKET)
        monkeypatch.setenv("B2_KEY_ID", KEY_ID)
        monkeypatch.setenv("B2_APPLICATION_KEY", APPLICATION_KEY)
        get_settings.cache_clear()
        yield control
        get_settings.cache_clear()

    def test_without_confirm_nothing_is_sent(
        self, environment: Path, fake_s3: type[FakeS3Client], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = fake_s3()
        monkeypatch.setattr(
            "stock_screener.sec_evidence.wiring.B2EvidenceStorage",
            lambda config: B2EvidenceStorage(config, client=client),
        )

        result = CliRunner().invoke(app, ["evidence", "b2-canary"])

        assert result.exit_code == 1
        assert "refusing without --confirm" in result.stdout
        assert client.calls == []

    def test_with_confirm_the_run_is_reported_without_secrets(
        self, environment: Path, fake_s3: type[FakeS3Client], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = fake_s3()
        monkeypatch.setattr(
            "stock_screener.sec_evidence.wiring.B2EvidenceStorage",
            lambda config: B2EvidenceStorage(config, client=client),
        )
        monkeypatch.setattr(
            "stock_screener.sec_evidence.canary.build_b2_client", lambda config: client
        )

        result = CliRunner().invoke(app, ["evidence", "b2-canary", "--confirm"])

        assert result.exit_code == 0, result.stdout
        assert "CANARY PASSED" in result.stdout
        assert "_canary/b28-" in result.stdout
        assert "deleted 7 objects" in result.stdout
        assert APPLICATION_KEY not in result.stdout
        assert KEY_ID not in result.stdout
        assert str(environment / "manifests") not in result.stdout, (
            "the production control root is not used"
        )

    def test_a_local_backend_stops_the_command(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        root = tmp_path / "sec"
        root.mkdir()
        monkeypatch.setenv("ENVIRONMENT", "test")
        monkeypatch.setenv("SEC_EVIDENCE_DIR", str(root))
        get_settings.cache_clear()

        result = CliRunner().invoke(app, ["evidence", "b2-canary", "--confirm"])

        assert result.exit_code == 1
        assert "requires SEC_STORAGE_BACKEND=b2" in result.stdout
        get_settings.cache_clear()
