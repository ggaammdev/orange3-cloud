"""Another cloud plugs in through its own module: a provider defined and
registered only here appears in the Cloud settings dialog with its sizes and
prices, Cloud File suggests one of its sizes and loads from its storage; and
the settings, saved per provider, keep the ones saved before."""

import shutil
import tempfile
from pathlib import Path

from AnyQt.QtCore import QSettings
from conftest import SPEC, https_server
from Orange.widgets.tests import base  # not WidgetTest itself: pytest would collect it
from test_cloud_file import write_csv
from test_widget_base import TOKEN, SharedMachineProvider

from orangecontrib.cloud.client.providers.base import MachineType
from orangecontrib.cloud.client.providers.registry import (
    register_provider,
    registered_providers,
    unregister_provider,
)
from orangecontrib.cloud.client.session import CloudSession, HandleStore
from orangecontrib.cloud.gui.machine_size import size_hints
from orangecontrib.cloud.gui.provider_page import SizesPage
from orangecontrib.cloud.gui.settings_dialog import CloudSettingsDialog
from orangecontrib.cloud.server.app import ServerConfig, create_machine_app
from orangecontrib.cloud.settings import CloudSettings, ProviderSettings
from orangecontrib.cloud.widgets.owcloudfile import OWCloudFile


class ThirdCloud(SharedMachineProvider):
    """A cloud the add-on doesn't know: the shared test machine, with sizes,
    prices and storage of its own. Its storage is a folder, and the machine
    reads it through "signed links" (here: the file's path on the machine)."""

    id = "thirdcloud"
    label = "Third Cloud"
    machine_sizes = (
        MachineType("t-small", "Tiny", 2, 4),
        MachineType("t-medium", "Middling", 8, 32),
        MachineType("t-huge", "Enormous", 96, 4096),
    )
    default_size = "t-medium"
    storage_schemes = ("third://",)
    PRICES = {"t-small": 0.1, "t-medium": 0.4, "t-huge": 9.5}

    def __init__(self, url, cert, storage):
        super().__init__(url, cert)
        self.storage = storage
        self.signed = []

    def size_price(self, machine_type, region, disk_gb):
        return self.PRICES.get(machine_type)

    def prices_date(self):
        return "2026-11-22"

    def machine_location(self, location, handle=None):
        if not location.startswith("third://"):
            return location
        self.signed.append(location)
        return str(self.storage / location.removeprefix("third://"))

    def read_stored_head(self, uri, length):
        path = self.storage / uri.removeprefix("third://")
        with open(path, "rb") as f:
            return path.stat().st_size, f.read(length)


class TestThirdCloud(base.WidgetTest):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.folder = folder = Path(tempfile.mkdtemp())
        cls.tear_down_stack.callback(shutil.rmtree, folder, ignore_errors=True)
        config = ServerConfig(token=TOKEN, data_dir=folder / "machine", worker_threads=2)
        cls.url, cls.cert, _ = cls.tear_down_stack.enter_context(
            https_server(create_machine_app(config), folder)
        )
        (folder / "bucket").mkdir()
        write_csv(folder / "bucket" / "data.csv", 500)

    def setUp(self):
        super().setUp()
        self.state = Path(tempfile.mkdtemp())
        self.provider = ThirdCloud(self.url, self.cert, self.folder / "bucket")
        register_provider("thirdcloud", lambda: self.provider, "Third Cloud")
        CloudSettings(provider="thirdcloud").save()
        # No provider of its own: the session takes the one chosen in the settings.
        self.session = CloudSession(store=HandleStore(self.state / "m.json"))
        self._previous, CloudSession._instance = CloudSession._instance, self.session
        self.session.start_machine(spec=SPEC, wait=True)
        self.process_events()

    def tearDown(self):
        super().tearDown()
        self.session.disconnect(forget=True, wait=True)
        self.session.shutdown()
        CloudSession._instance = self._previous
        unregister_provider("thirdcloud")
        shutil.rmtree(self.state, ignore_errors=True)

    def big_file(self):
        """A sparse file: 20 GB on paper, 2000 real rows."""
        big = self.state / "big.csv"
        write_csv(big, 2000)
        with open(big, "r+b") as f:
            f.truncate(20 * 1024**3)
        return big

    def test_sizes_in_dialog_and_cloud_file(self):
        assert self.session.handle.provider == "thirdcloud"
        assert [e.id for e in registered_providers()] == [
            "local",
            "gcp",
            "aws",
            "azure",
            "thirdcloud",
        ]

        # Cloud File: the file is too big for the machine; it suggests the
        # chosen provider's size that fits.
        w = self.create_widget(OWCloudFile)
        w.set_local_path(str(self.big_file()))
        suggested = w.estimate.suggested_machine_type(ThirdCloud.machine_sizes)
        assert suggested is not None and suggested.name == "t-huge"
        text = str(w.Warning.too_big)
        assert 'On Third Cloud, use "Enormous" (t-huge, 4096 GB) or larger' in text

        # The dialog offers the provider, with its page: its sizes and prices,
        # the one that fits the file marked.
        dialog = CloudSettingsDialog(None, self.session)
        try:
            assert dialog.provider_buttons["thirdcloud"].text() == "Third Cloud"
            assert dialog.provider_buttons["thirdcloud"].isChecked()
            page = dialog.pages["thirdcloud"]
            assert isinstance(page, SizesPage) and dialog.page is page and page.ready
            picker = page.size_picker
            items = [picker.combo.itemText(i) for i in range(picker.combo.count())]
            assert items[0] == "Tiny (t-small): 2 CPUs, 4 GB, $0.10/h"
            assert items[1] == "Middling (t-medium): 8 CPUs, 32 GB, $0.40/h"
            assert items[2].startswith("Enormous (t-huge)") and items[2].endswith("fits your data")
            assert picker.machine_type == "t-medium"  # the provider's default
            assert "on 2026-11-22" in picker.note_label.text()
            picker.use_suggestion()
            assert CloudSettings.load().of("thirdcloud").machine_type == "t-huge"
        finally:
            dialog.close()
        w.onDeleteWidget()
        assert size_hints().largest() is None

    def test_storage_locations(self):
        w = self.create_widget(OWCloudFile)
        w.set_url("s3://bucket/data.csv")
        assert not w.load()
        assert "must start with https://, http:// or third://" in str(w.Error.source_error)
        assert "third://bucket/data.csv" in w.url_edit.placeholderText()

        # A location in the provider's storage: estimated with the user's
        # sign-in, then loaded through the location the provider gives the machine.
        w.set_url("third://data.csv")
        assert w.load() or w.task is not None
        self.wait_until_finished(w, timeout=60000)
        assert not w.Error.active, [str(m) for m in w.Error.active]
        assert w.estimate is not None and w.estimate.columns == 5
        assert self.provider.signed == ["third://data.csv"]
        assert self.get_output(w.Outputs.data, widget=w).ref.n_rows == 500


def test_settings_per_provider(tmp_path):
    """Each provider's settings are kept apart; those saved by versions with
    only Google Cloud (gcp_* keys) are read, and replaced when saving."""
    store = QSettings(str(tmp_path / "old.ini"), QSettings.Format.IniFormat)
    for key, value in {
        "provider": "gcp",
        "gcp_project": "my-project",
        "gcp_region": "europe-west1",
        "gcp_zone": "europe-west1-b",
        "gcp_machine_type": "n2-highmem-16",
        "gcp_disk_gb": "300",
    }.items():
        store.setValue(f"orange3-cloud/{key}", value)
    settings = CloudSettings.load(store)
    assert settings.provider == "gcp"
    assert settings.of("gcp") == ProviderSettings(
        "my-project", "europe-west1", "europe-west1-b", "n2-highmem-16", 300
    )

    settings.of("thirdcloud").region = "north-1"
    settings.save(store)
    assert store.value("orange3-cloud/providers/gcp/account") == "my-project"
    assert store.value("orange3-cloud/gcp_project") is None
    again = CloudSettings.load(store)
    assert again.of("gcp") == settings.of("gcp")
    assert again.of("thirdcloud").region == "north-1" and again.of("local").region == ""
