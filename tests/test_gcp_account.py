"""Google sign-in, project, region and zone: the gcloud configuration, the
credentials, listing (with Google's clients replaced), the sign-in process
and the dialog's Google Cloud page (with the account functions replaced, and
a fake gcloud).

``test_real_account`` uses this computer's own sign-in, read-only; it runs
only when ``ORANGE3_CLOUD_GCP_PROJECT`` is set (the development env file),
so CI needs no Google credentials."""

import os
import stat
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from AnyQt.QtCore import QSettings, Qt
from AnyQt.QtTest import QTest
from AnyQt.QtWidgets import QApplication

from orangecontrib.cloud.client.providers import gcp_account, gcp_setup
from orangecontrib.cloud.client.providers.base import ProviderError, SignInCancelledError
from orangecontrib.cloud.client.providers.gcp_account import (
    NotSignedInError,
    Project,
    Region,
)
from orangecontrib.cloud.settings import CloudSettings


def spin(app, condition, timeout=30):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        app.processEvents()
        time.sleep(0.01)


def test_gcloud_defaults(tmp_path, monkeypatch):
    for var in ("CLOUDSDK_ACTIVE_CONFIG_NAME", "CLOUDSDK_CORE_PROJECT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("CLOUDSDK_COMPUTE_REGION", raising=False)
    monkeypatch.delenv("CLOUDSDK_COMPUTE_ZONE", raising=False)
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path))
    assert gcp_account.gcloud_defaults() == gcp_account.GcloudDefaults()

    (tmp_path / "configurations").mkdir()
    (tmp_path / "active_config").write_text("work\n")
    (tmp_path / "configurations" / "config_work").write_text(
        "[core]\nproject = my-project\n\n[compute]\nzone = europe-west4-b\n"
    )
    # The region follows the zone when only the zone is set.
    assert gcp_account.gcloud_defaults() == gcp_account.GcloudDefaults(
        "my-project", "europe-west4", "europe-west4-b"
    )
    monkeypatch.setenv("CLOUDSDK_COMPUTE_REGION", "us-east1")
    assert gcp_account.gcloud_defaults().region == "us-east1"


def test_pick_region_and_zone():
    regions = [Region("europe-west4", ("europe-west4-a",)), Region("us-central1", ())]
    assert gcp_account.pick_region(regions) == "us-central1"
    assert gcp_account.pick_region(regions, "", "europe-west4") == "europe-west4"
    assert gcp_account.pick_region(regions, "mars-1") == "us-central1"
    assert gcp_account.pick_region([Region("asia-east1")]) == "asia-east1"
    assert gcp_account.pick_region([]) == ""
    zones = ("us-central1-b", "us-central1-a")
    assert gcp_account.pick_zone(zones, "us-central1") == "us-central1-a"
    assert gcp_account.pick_zone(zones, "us-central1", "us-central1-b") == "us-central1-b"
    assert gcp_account.pick_zone(("us-east1-c",), "us-east1") == "us-east1-c"


def test_load_credentials_drops_the_quota_project(monkeypatch):
    import google.auth
    from google.auth.exceptions import DefaultCredentialsError

    class Credentials:
        quota_project_id = "unrelated-project"

        def with_quota_project(self, project):
            copy = Credentials()
            copy.quota_project_id = project
            return copy

    monkeypatch.setattr(google.auth, "default", lambda **kw: (Credentials(), "adc-project"))
    credentials, project = gcp_account.load_credentials()
    assert credentials.quota_project_id is None and project == "adc-project"

    def missing(**kw):
        raise DefaultCredentialsError("no ADC")

    monkeypatch.setattr(google.auth, "default", missing)
    with pytest.raises(NotSignedInError, match="Sign in with Google"):
        gcp_account.load_credentials()


def test_list_projects_and_regions(monkeypatch):
    from google.api_core import exceptions
    from google.cloud import compute_v1, resourcemanager_v3

    active = resourcemanager_v3.Project.State.ACTIVE
    deleted = resourcemanager_v3.Project.State.DELETE_REQUESTED

    class Projects:
        def __init__(self, **kw):
            pass

        def search_projects(self, request):
            return [
                SimpleNamespace(project_id="b-id", display_name="Beta", state=active),
                SimpleNamespace(project_id="a-id", display_name="a-id", state=active),
                SimpleNamespace(project_id="gone", display_name="Gone", state=deleted),
            ]

    monkeypatch.setattr(resourcemanager_v3, "ProjectsClient", Projects)
    projects = gcp_account.list_projects(object())
    assert projects == [Project("a-id", "a-id"), Project("b-id", "Beta")]
    assert [p.label for p in projects] == ["a-id", "Beta (b-id)"]

    base = "https://www.googleapis.com/compute/v1/projects/p/regions/"

    class Zones:
        def __init__(self, **kw):
            pass

        def list(self, project):
            assert project == "p"
            return [
                SimpleNamespace(name="us-east1-c", region=base + "us-east1", status="UP"),
                SimpleNamespace(name="us-east1-b", region=base + "us-east1", status="UP"),
                SimpleNamespace(name="us-east1-d", region=base + "us-east1", status="DOWN"),
                SimpleNamespace(name="asia-east1-a", region=base + "asia-east1", status="UP"),
            ]

    monkeypatch.setattr(compute_v1, "ZonesClient", Zones)
    assert gcp_account.list_regions(object(), "p") == [
        Region("asia-east1", ("asia-east1-a",)),
        Region("us-east1", ("us-east1-b", "us-east1-c")),
    ]

    class Disabled:
        def __init__(self, **kw):
            pass

        def list(self, project):
            raise exceptions.Forbidden(
                "Compute Engine API has not been used in project p before or it is disabled."
            )

    monkeypatch.setattr(compute_v1, "ZonesClient", Disabled)
    with pytest.raises(gcp_account.ProviderError, match="API isn't enabled in project p"):
        gcp_account.list_regions(object(), "p")


def fake_gcloud(path: Path, body: str) -> str:
    """A shell script standing in for gcloud."""
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def gone(pid: int, timeout: float = 5) -> bool:
    """Whether process ``pid`` ends within ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


@pytest.mark.skipif(sys.platform == "win32", reason="the fake gcloud is a shell script")
def test_sign_in_process(tmp_path):
    """The sign-in runs gcloud's command without a console and returns its
    output; a failure says why; cancelling kills it and what it started."""
    gcloud = fake_gcloud(tmp_path / "gcloud", 'read answer; echo "args: $@ [$answer]"\n')
    assert gcp_account.sign_in(gcloud).strip() == "args: auth application-default login []"

    fake_gcloud(tmp_path / "gcloud", "echo one; echo two >&2; exit 3\n")
    with pytest.raises(ProviderError, match=r"\(gcloud exit code 3\)\.\none\ntwo$"):
        gcp_account.sign_in(gcloud)

    pid_file = tmp_path / "child.pid"
    fake_gcloud(tmp_path / "gcloud", f'sleep 60 & echo $! > "{pid_file}"; wait\n')
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(SignInCancelledError, match="Sign-in cancelled"):
        gcp_account.sign_in(gcloud, cancel)
    assert time.monotonic() - started < 5
    assert gone(int(pid_file.read_text()))  # gcloud's own child process too

    with pytest.raises(ProviderError, match="Couldn't run gcloud to sign in"):
        gcp_account.sign_in(str(tmp_path / "missing"))


def test_gui_runs_no_process():
    """The settings dialog and the widgets run no command (rule 4): signing
    in goes through the session and the provider."""
    package = Path(gcp_account.__file__).parents[2]
    for folder in ("gui", "widgets", "data"):
        for path in (package / folder).rglob("*.py"):
            source = path.read_text()
            assert "QProcess" not in source and "subprocess" not in source, path


@pytest.fixture
def fake_google(tmp_path, monkeypatch):
    """A fake gcloud script and fake account functions: not signed in until
    the script has run, then two projects with a few zones."""
    marker = tmp_path / "signed-in"
    script = tmp_path / "gcloud"
    script.write_text(f'#!/bin/sh\necho "$@" > "{marker}"\necho "Credentials saved."\n')
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    calls = []

    def load_credentials():
        if not marker.exists():
            raise NotSignedInError('Not signed in to Google Cloud. Click "Sign in with Google".')
        return SimpleNamespace(service_account_email="me@example.com"), ""

    def list_regions(credentials, project):
        calls.append(project)
        if project == "proj-a":
            return [
                Region("europe-west4", ("europe-west4-a", "europe-west4-b")),
                Region("us-central1", ("us-central1-a", "us-central1-c")),
            ]
        return [Region("asia-east1", ("asia-east1-b",))]

    monkeypatch.setattr(gcp_account, "find_gcloud", lambda: str(script))
    monkeypatch.setattr(gcp_account, "gcloud_defaults", lambda: gcp_account.GcloudDefaults())
    monkeypatch.setattr(gcp_account, "load_credentials", load_credentials)
    monkeypatch.setattr(
        gcp_account,
        "list_projects",
        lambda c: [Project("proj-a", "Project A"), Project("proj-b", "Project B")],
    )
    monkeypatch.setattr(gcp_account, "list_regions", list_regions)
    # The page's setup checker makes no Google calls (tested in test_gcp_setup).
    monkeypatch.setattr(gcp_setup, "GoogleSetupApi", lambda credentials: gcp_setup.SetupApi())
    return SimpleNamespace(marker=marker, calls=calls)


@pytest.mark.skipif(sys.platform == "win32", reason="the fake gcloud is a shell script")
def test_dialog_signs_in_and_lists_projects_and_regions(fake_google, fake, tmp_path):
    from orangecontrib.cloud.client.session import CloudSession, HandleStore
    from orangecontrib.cloud.gui.settings_dialog import CloudSettingsDialog

    app = QApplication.instance() or QApplication([])
    session = CloudSession(fake, store=HandleStore(tmp_path / "machine.json"))
    dialog = CloudSettingsDialog(session=session)
    dialog.show()
    page = dialog.pages["gcp"]
    assert not page._started  # nothing runs while the Local page shows

    dialog.provider_buttons["gcp"].setChecked(True)
    spin(app, lambda: page._started and not page.busy)
    assert page.account_label.text() == "Not signed in"
    assert "Sign in with Google" in page.message_label.text()
    assert page.project_combo.count() == 0 and not page.project_combo.isEnabled()
    assert page.sign_in_button.isEnabled()
    # Google Cloud machines can't be started yet (5.3).
    assert not dialog.start_button.isEnabled()

    QTest.mouseClick(page.sign_in_button, Qt.MouseButton.LeftButton)
    spin(app, lambda: page.zone_combo.count() > 0 and not page.busy)
    assert fake_google.marker.read_text().split() == ["auth", "application-default", "login"]
    assert page.account_label.text() == "me@example.com"
    assert page.sign_in_button.text() == "Sign in again"
    assert [page.project_combo.itemText(i) for i in range(2)] == [
        "Project A (proj-a)",
        "Project B (proj-b)",
    ]
    assert page.project == "proj-a"
    # No region in the gcloud config: us-central1 / us-central1-a.
    assert (page.region, page.zone) == ("us-central1", "us-central1-a")

    # Choices are saved at once in the settings store, never in a workflow.
    page.region_combo.setCurrentText("europe-west4")
    page.region_combo.activated.emit(page.region_combo.currentIndex())
    page.zone_combo.setCurrentText("europe-west4-b")
    page.zone_combo.activated.emit(page.zone_combo.currentIndex())
    store = QSettings(os.environ["ORANGE3_CLOUD_SETTINGS_FILE"], QSettings.Format.IniFormat)
    assert store.value("orange3-cloud/provider") == "gcp"
    assert store.value("orange3-cloud/providers/gcp/account") == "proj-a"
    assert store.value("orange3-cloud/providers/gcp/region") == "europe-west4"
    assert store.value("orange3-cloud/providers/gcp/zone") == "europe-west4-b"

    page.project_combo.setCurrentIndex(1)
    page.project_combo.activated.emit(1)
    spin(app, lambda: page.region == "asia-east1" and not page.busy)
    assert page.zone == "asia-east1-b"
    assert CloudSettings.load().of("gcp").account == "proj-b"
    page.project_combo.setCurrentIndex(0)
    page.project_combo.activated.emit(0)
    assert fake_google.calls == ["proj-a", "proj-b"]  # regions are cached
    dialog.close()
    page.shutdown()

    # A new dialog restores the saved project, region and zone, also those
    # saved by versions with only Google Cloud (keys gcp_*).
    store.remove("orange3-cloud/providers")
    store.setValue("orange3-cloud/gcp_project", "proj-a")
    store.setValue("orange3-cloud/gcp_region", "europe-west4")
    store.setValue("orange3-cloud/gcp_zone", "europe-west4-b")
    store.sync()
    dialog = CloudSettingsDialog(session=session)
    dialog.show()
    page = dialog.pages["gcp"]
    spin(app, lambda: page.zone_combo.count() > 0 and not page.busy)
    assert (page.project, page.region, page.zone) == ("proj-a", "europe-west4", "europe-west4-b")
    dialog.close()
    page.shutdown()


def test_dialog_without_gcloud(fake_google, monkeypatch, fake, tmp_path):
    from orangecontrib.cloud.client.session import CloudSession, HandleStore
    from orangecontrib.cloud.gui.settings_dialog import CloudSettingsDialog

    monkeypatch.setattr(gcp_account, "find_gcloud", lambda: None)
    app = QApplication.instance() or QApplication([])
    CloudSettings(provider="gcp").save()
    dialog = CloudSettingsDialog(session=CloudSession(fake, store=HandleStore(tmp_path / "m")))
    dialog.show()
    page = dialog.pages["gcp"]
    spin(app, lambda: page._started and not page.busy)
    assert "Install the Google Cloud CLI" in page.gcloud_label.text()
    assert gcp_account.GCLOUD_INSTALL_URL in page.gcloud_label.text()
    assert not page.sign_in_button.isEnabled()
    dialog.close()
    page.shutdown()


@pytest.mark.skipif(sys.platform == "win32", reason="the fake gcloud is a shell script")
def test_dialog_cancels_and_reports_sign_in(fake_google, fake, tmp_path):
    """While the browser sign-in runs, the page stays usable and "Cancel
    sign-in" stops it; a failed sign-in shows gcloud's last lines."""
    from orangecontrib.cloud.client.session import CloudSession, HandleStore
    from orangecontrib.cloud.gui.settings_dialog import CloudSettingsDialog

    app = QApplication.instance() or QApplication([])
    script = Path(gcp_account.find_gcloud())
    fake_gcloud(script, "sleep 60\n")  # waits for a browser that never answers
    CloudSettings(provider="gcp").save()
    session = CloudSession(fake, store=HandleStore(tmp_path / "machine.json"))
    dialog = CloudSettingsDialog(session=session)
    dialog.show()
    page = dialog.pages["gcp"]
    spin(app, lambda: page._started and not page.busy)

    QTest.mouseClick(page.sign_in_button, Qt.MouseButton.LeftButton)
    assert page.busy and session.signing_in
    assert page.sign_in_button.text() == "Cancel sign-in" and page.sign_in_button.isEnabled()
    assert not page.reload_button.isEnabled()
    QTest.mouseClick(page.sign_in_button, Qt.MouseButton.LeftButton)
    spin(app, lambda: not page.busy, timeout=5)
    assert page.message_label.text() == "Sign-in cancelled."
    assert page.sign_in_button.text() == "Sign in with Google"
    assert not session.signing_in

    fake_gcloud(script, "echo 'ERROR: (gcloud.auth) There was a problem.'; exit 1\n")
    QTest.mouseClick(page.sign_in_button, Qt.MouseButton.LeftButton)
    spin(app, lambda: not page.busy)
    assert "gcloud exit code 1" in page.message_label.text()
    assert "There was a problem." in page.message_label.text()
    dialog.close()
    page.shutdown()
    session.shutdown()


@pytest.mark.skipif(
    not os.environ.get("ORANGE3_CLOUD_GCP_PROJECT"),
    reason="needs this computer's Google sign-in (ORANGE3_CLOUD_GCP_PROJECT)",
)
def test_real_account():
    """Read-only: the signed-in user's projects include the test project, and
    its regions include the default one."""
    project = os.environ["ORANGE3_CLOUD_GCP_PROJECT"]
    credentials, _ = gcp_account.load_credentials()
    assert project in {p.project_id for p in gcp_account.list_projects(credentials)}
    regions = {r.name: r.zones for r in gcp_account.list_regions(credentials, project)}
    assert gcp_account.DEFAULT_ZONE in regions[gcp_account.DEFAULT_REGION]
    assert "@" in gcp_account.account_email(credentials)
