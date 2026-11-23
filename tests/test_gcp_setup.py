"""The setup checker, with Google's APIs replaced by a fake project: an
unprepared project lists exactly what's missing, a ready one passes, the
REST errors map to checks, and the dialog's "Enable APIs" asks first.

``test_real_project_is_ready`` checks the development test project with
this computer's own sign-in, read-only; it runs only when
``ORANGE3_CLOUD_GCP_PROJECT`` is set, so CI needs no Google credentials."""

import os
import sys
from types import SimpleNamespace

import pytest
from AnyQt.QtWidgets import QApplication, QMessageBox
from test_gcp_account import fake_google, spin  # noqa: F401 - fixture

from orangecontrib.cloud.client.providers import gcp_setup
from orangecontrib.cloud.client.providers.base import ProviderError
from orangecontrib.cloud.client.providers.gcp_setup import ApiDisabledError, GoogleSetupApi
from orangecontrib.cloud.client.providers.setup_checks import Status, all_passed

CREDENTIALS = object()


class FakeProject(gcp_setup.SetupApi):
    """A project that is ready unless told otherwise."""

    def __init__(
        self, billing=True, apis=("compute", "storage"), region_cpus=None, all_regions=32, denied=()
    ):
        self.billing = billing
        self.apis = {f"{a}.googleapis.com" for a in apis}
        self.region_cpus = region_cpus or {"CPUS": 24, "N2_CPUS": 24, "N2D_CPUS": 24}
        self.all_regions = all_regions
        self.denied = set(denied)
        self.enabled_calls = []

    def _compute(self):
        if "compute.googleapis.com" not in self.apis:
            raise ApiDisabledError("Compute Engine API has not been used in project p")

    def billing_enabled(self, project):
        return self.billing

    def enabled_services(self, project, services):
        return {s for s in services if s in self.apis}

    def enable_services(self, project, services):
        self.enabled_calls.append(list(services))
        self.apis.update(services)

    def region_quotas(self, project, region):
        self._compute()
        return {metric: (limit, 0.0) for metric, limit in self.region_cpus.items()}

    def project_quotas(self, project):
        self._compute()
        return {"CPUS_ALL_REGIONS": (self.all_regions, 4.0)}

    def granted_permissions(self, project, permissions):
        return set(permissions) - self.denied


def run(api, machine_type="n2-standard-16", credentials=CREDENTIALS, project="p"):
    return {
        c.key: c
        for c in gcp_setup.run_checks(credentials, project, "us-central1", machine_type, api=api)
    }


def failed(checks):
    return {key for key, c in checks.items() if c.status is Status.FAILED}


def test_ready_project_passes():
    checks = run(FakeProject())
    assert list(checks) == [
        "credentials",
        "project",
        "billing",
        "apis",
        "quota_family",
        "quota_global",
        "permissions",
    ]
    assert all(c.passed for c in checks.values())
    assert all_passed(checks.values())
    assert checks["quota_family"].title == "N2_CPUS quota in us-central1"
    assert checks["quota_global"].detail.startswith("28 of 32 vCPUs free")


def test_new_project_lists_billing_and_apis():
    """A new project: no billing and the APIs off. The quotas can't be read
    yet (the Compute Engine API is off), so they aren't reported missing."""
    checks = run(FakeProject(billing=False, apis=()))
    assert failed(checks) == {"billing", "apis"}
    assert checks["apis"].missing == ("compute.googleapis.com", "storage.googleapis.com")
    assert "Compute Engine API, Cloud Storage API" in checks["apis"].detail
    assert "billing" in checks["billing"].link and "project=p" in checks["billing"].link
    assert checks["quota_family"].status is Status.UNKNOWN
    assert checks["permissions"].passed
    for check in checks.values():
        if check.status is Status.FAILED:
            assert check.fix and check.link.startswith("https://")


def test_quotas_and_permissions_missing():
    api = FakeProject(
        apis=("compute",),
        region_cpus={"CPUS": 24, "N2_CPUS": 8},
        all_regions=12,
        denied={"storage.buckets.create", "compute.firewalls.create"},
    )
    checks = run(api)  # Medium: 16 vCPUs
    assert failed(checks) == {"apis", "quota_family", "quota_global", "permissions"}
    assert checks["apis"].missing == ("storage.googleapis.com",)
    assert "8 of 8 vCPUs free; n2-standard-16 needs 16" in checks["quota_family"].detail
    assert "N2_CPUS quota in us-central1 of at least 16" in checks["quota_family"].fix
    assert "CPUS_ALL_REGIONS quota of at least 20" in checks["quota_global"].fix
    assert set(checks["permissions"].missing) == api.denied
    assert "firewall rules, buckets" in checks["permissions"].detail

    # The Small size fits; N2D has no quota at all in the region.
    assert failed(run(api, "e2-standard-4")) == {"apis", "permissions"}
    assert "No N2D_CPUS quota in us-central1" in run(api, "n2d-standard-64")["quota_family"].detail
    assert gcp_setup.cpu_quota_metric("c3-standard-88") == "C3_CPUS"


def test_not_signed_in_calls_nothing():
    def no_api(credentials):
        raise AssertionError("no Google calls without credentials")

    checks = gcp_setup.run_checks(None, "", "us-central1", "e2-standard-4", make_api=no_api)
    assert [c.status for c in checks[:2]] == [Status.FAILED, Status.FAILED]
    assert all(c.status is Status.UNKNOWN for c in checks[2:])


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self.ok = status < 400
        self._body = body
        self.content = b"x"
        self.text = str(body)
        self.reason = ""

    def json(self):
        return self._body


def test_google_setup_api_requests_and_errors():
    calls = []
    replies = {
        "billingInfo": FakeResponse(200, {"billingEnabled": False}),
        "services:batchGet": FakeResponse(
            200,
            {
                "services": [
                    {"config": {"name": "compute.googleapis.com"}, "state": "ENABLED"},
                    {"config": {"name": "storage.googleapis.com"}, "state": "DISABLED"},
                ]
            },
        ),
        "regions/us-central1": FakeResponse(
            403,
            {
                "error": {
                    "code": 403,
                    "message": "Compute Engine API has not been used",
                    "details": [{"reason": "SERVICE_DISABLED"}],
                }
            },
        ),
        "projects/p:testIamPermissions": FakeResponse(
            403, {"error": {"code": 403, "message": "denied", "status": "PERMISSION_DENIED"}}
        ),
        "services:batchEnable": FakeResponse(200, {"name": "operations/op1", "done": False}),
        "operations/op1": FakeResponse(200, {"name": "operations/op1", "done": True}),
    }

    def request(method, url, timeout, **kwargs):
        calls.append((method, url, kwargs))
        return next(reply for key, reply in replies.items() if url.endswith(key))

    api = GoogleSetupApi.__new__(GoogleSetupApi)
    api.session, api.timeout = SimpleNamespace(request=request), 5
    assert api.billing_enabled("p") is False
    assert api.enabled_services("p", gcp_setup.REQUIRED_APIS) == {"compute.googleapis.com"}
    with pytest.raises(ApiDisabledError):
        api.region_quotas("p", "us-central1")
    with pytest.raises(ProviderError, match="no permission in project p"):
        api.granted_permissions("p", ["compute.instances.create"])

    gcp_setup.enable_apis(api, "p", ["storage.googleapis.com"])
    enable = [c for c in calls if c[1].endswith("batchEnable")]
    assert enable[0][0] == "POST"
    assert enable[0][2]["json"] == {"serviceIds": ["storage.googleapis.com"]}
    assert calls[-1][1] == f"{GoogleSetupApi.SERVICE_USAGE}/operations/op1"


@pytest.mark.skipif(sys.platform == "win32", reason="the fake gcloud is a shell script")
def test_dialog_shows_checks_and_enables_apis(fake_google, fake, tmp_path, monkeypatch):  # noqa: F811
    from orangecontrib.cloud.client.session import CloudSession, HandleStore
    from orangecontrib.cloud.gui.settings_dialog import CloudSettingsDialog
    from orangecontrib.cloud.settings import CloudSettings

    project = FakeProject(billing=True, apis=())
    monkeypatch.setattr(gcp_setup, "GoogleSetupApi", lambda credentials: project)
    fake_google.marker.write_text("signed in")
    asked = []

    def question(parent, title, text, *args):
        asked.append(text)
        return QMessageBox.StandardButton.Yes if len(asked) > 1 else QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", question)

    app = QApplication.instance() or QApplication([])
    CloudSettings(provider="gcp").save()
    dialog = CloudSettingsDialog(session=CloudSession(fake, store=HandleStore(tmp_path / "m")))
    dialog.show()
    page = dialog.pages["gcp"]
    spin(app, lambda: page.setup_checks is not None and not page.busy)
    text = page.setup_label.text()
    assert "✗" in text and "Compute Engine API, Cloud Storage API" in text
    assert "flows/enableapi" in text
    assert not page.setup_ok and not page.enable_button.isHidden()

    # The user says no: nothing is enabled.
    page.enable_button.click()
    assert project.enabled_calls == [] and "Cloud Storage API" in asked[0]
    # The user says yes: the APIs are enabled and checked again.
    page.enable_button.click()
    spin(app, lambda: page.setup_ok and not page.busy)
    assert project.enabled_calls == [["compute.googleapis.com", "storage.googleapis.com"]]
    assert page.enable_button.isHidden()
    assert "The project is ready" in page.setup_label.text()
    assert "Permissions for machines" in page.setup_label.toolTip()
    dialog.close()
    page.shutdown()


@pytest.mark.skipif(
    not os.environ.get("ORANGE3_CLOUD_GCP_PROJECT"),
    reason="needs this computer's Google sign-in (ORANGE3_CLOUD_GCP_PROJECT)",
)
def test_real_project_is_ready():
    """Read-only: the development test project passes every check."""
    from orangecontrib.cloud.client.providers.gcp_account import load_credentials

    credentials, _ = load_credentials()
    project = os.environ["ORANGE3_CLOUD_GCP_PROJECT"]
    region = os.environ.get("ORANGE3_CLOUD_GCP_REGION", "us-central1")
    for machine_type in ("e2-standard-4", "n2-standard-16"):
        checks = gcp_setup.run_checks(credentials, project, region, machine_type)
        assert all_passed(checks), [c for c in checks if not c.passed]
