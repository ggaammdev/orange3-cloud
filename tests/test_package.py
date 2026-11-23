import ast
import os
import subprocess
import sys
from importlib.metadata import entry_points
from pathlib import Path

import orangecontrib.cloud


def test_import():
    import orangecontrib.cloud  # noqa: F401
    from orangecontrib.cloud import widgets

    assert widgets.NAME == "Cloud"


def test_init_files_hold_only_a_docstring():
    """A package's ``__init__.py`` holds only its docstring; the code lives in
    modules. The exception is ``widgets/__init__.py``, which holds the
    category metadata that Orange reads."""
    package = Path(orangecontrib.cloud.__file__).parent
    allowed = {package / "widgets" / "__init__.py"}
    more = []
    for init in sorted(set(package.rglob("__init__.py")) - allowed):
        body = ast.parse(init.read_text()).body
        docstring_only = (
            len(body) == 1
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        )
        if not docstring_only:
            more.append(str(init.relative_to(package)))
    assert not more, f"move the code of these into modules: {more}"


def test_entry_points():
    addon = {ep.name: ep.value for ep in entry_points(group="orange3.addon")}
    widgets = {ep.name: ep.value for ep in entry_points(group="orange.widgets")}
    assert addon.get("cloud") == "orangecontrib.cloud"
    assert widgets.get("Cloud") == "orangecontrib.cloud.widgets"


def test_category_icon_is_packaged():
    from importlib.resources import files

    from orangecontrib.cloud import widgets

    assert files(widgets).joinpath(widgets.ICON).is_file()


def test_every_cloud_widget_has_a_help_page():
    """Orange's help finds a page for each Cloud widget through the
    ``orange.canvas.help`` entry point, and the page and its images exist."""
    import pkgutil
    import re
    from types import SimpleNamespace

    from AnyQt.QtWidgets import QApplication
    from orangecanvas.help.manager import get_help_provider_for_distribution
    from orangecanvas.utils.pkgmeta import get_distribution

    from orangecontrib.cloud import widgets

    QApplication.instance() or QApplication([])
    helps = [ep.value for ep in entry_points(group="orange.canvas.help")]
    assert "orangecontrib.cloud.widgets:WIDGET_HELP_PATH" in helps
    provider = get_help_provider_for_distribution(get_distribution("orange3-cloud"))
    assert provider is not None

    folder = Path(widgets.__file__).parent
    names = []
    for module in pkgutil.iter_modules(widgets.__path__):
        if module.name.startswith("ow"):
            source = (folder / f"{module.name}.py").read_text()
            names += re.findall(r'^    name = "(Cloud [^"]+)"$', source, re.MULTILINE)
    assert len(names) >= 15
    for name in names:
        page = Path(provider.search(SimpleNamespace(name=name)).toLocalFile())
        assert page.is_file(), name
        for image in re.findall(r'src="([^"]+)"', page.read_text()):
            assert (page.parent / image).is_file(), (name, image)


#: What Orange imports of the add-on when it starts (the widgets, through the
#: ``orange.widgets`` entry point, and the settings), plus the provider list of
#: the Cloud settings dialog, in a new Python process.
START_UP = """
import importlib, pkgutil, sys
from orangecontrib.cloud import settings, widgets
from orangecontrib.cloud.client.providers.registry import registered_providers
for module in pkgutil.iter_modules(widgets.__path__):
    if module.name.startswith("ow"):
        importlib.import_module(f"{widgets.__name__}.{module.name}")
registered_providers()
clouds = {"boto3", "botocore", "awscrt", "s3transfer", "azure", "msal", "google", "grpc"}
loaded = sorted({name.split(".")[0] for name in sys.modules} & clouds)
assert not loaded, f"imported when Orange starts: {loaded}"
"""


def test_start_up_imports_no_cloud_library():
    """The clouds' libraries load only when their provider is used, so they
    don't slow down Orange's start."""
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    subprocess.run([sys.executable, "-c", START_UP], check=True, env=env)
