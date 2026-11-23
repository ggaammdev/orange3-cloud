"""The release's checks (``scripts/release.py``, run by the release workflow)."""

import importlib.util
import io
import re
import shutil
import tarfile
import zipfile
from pathlib import Path

import pytest

from orangecontrib.cloud.client.providers.base import PUBLIC_IMAGE

ROOT = Path(__file__).parent.parent


def load_script():
    spec = importlib.util.spec_from_file_location("release", ROOT / "scripts" / "release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release = load_script()


def test_readme_links_for_pypi(tmp_path):
    """PyPI shows the README without the repository: its relative links and
    images point to the repository at the version's tag."""
    text = "![Shot](docs/a.png) [Guide](docs/setup.md) [Up](#costs) [Orange](https://x.org)"
    assert release.readme_for_pypi(text, "o/r", "v1.2.3") == (
        "![Shot](https://raw.githubusercontent.com/o/r/v1.2.3/docs/a.png) "
        "[Guide](https://github.com/o/r/blob/v1.2.3/docs/setup.md) "
        "[Up](https://github.com/o/r/blob/v1.2.3/README.md#costs) [Orange](https://x.org)"
    )
    shutil.copy(ROOT / "README.md", tmp_path)
    shutil.copy(ROOT / "pyproject.toml", tmp_path)
    release.prepare(tmp_path, "1.0.0rc1", publish=False)
    targets = re.findall(r"\]\(([^)\s]+)\)", (tmp_path / "README.md").read_text())
    assert targets and all(t.startswith("https://") for t in targets)
    assert 'version = "1.0.0rc1"' in (tmp_path / "pyproject.toml").read_text()


def test_publishing_needs_the_committed_version_and_a_changelog(tmp_path):
    shutil.copy(ROOT / "README.md", tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.2.3"\n')
    with pytest.raises(release.ReleaseError, match="X.Y.Z"):
        release.prepare(tmp_path, "1.2.3rc1", publish=True)
    with pytest.raises(release.ReleaseError, match="not 1.2.4"):
        release.prepare(tmp_path, "1.2.4", publish=True)
    with pytest.raises(release.ReleaseError, match="no section"):
        release.prepare(tmp_path, "1.2.3", publish=True)
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [1.2.3] - 2026-12-01\n\n- New.\n\n## 1.2.2\n\n- Old.\n"
    )
    assert release.prepare(tmp_path, "1.2.3", publish=True) == "- New."
    assert 'version = "1.2.3"' in (tmp_path / "pyproject.toml").read_text()


def test_packages_with_internal_files_are_refused(tmp_path):
    def add(tar, name):
        tar.addfile(tarfile.TarInfo(name), io.BytesIO())

    with tarfile.open(tmp_path / "orange3_cloud-1.0.0.tar.gz", "w:gz") as sdist:
        add(sdist, "orange3_cloud-1.0.0/pyproject.toml")
    with zipfile.ZipFile(tmp_path / "orange3_cloud-1.0.0-py3-none-any.whl", "w") as wheel:
        wheel.writestr("orangecontrib/cloud/env.py", "")
    assert len(release.check_dist(tmp_path, "1.0.0")) == 2
    with pytest.raises(release.ReleaseError, match="one sdist orange3_cloud-1.0.1.tar.gz"):
        release.check_dist(tmp_path, "1.0.1")

    with tarfile.open(tmp_path / "orange3_cloud-1.0.0.tar.gz", "w:gz") as sdist:
        add(sdist, "orange3_cloud-1.0.0/pyproject.toml")
        add(sdist, "orange3_cloud-1.0.0/internal/notes.md")
        add(sdist, "orange3_cloud-1.0.0/PLAN.md")
    with pytest.raises(release.ReleaseError, match="internal/notes.md, .*PLAN.md"):
        release.check_dist(tmp_path, "1.0.0")


def test_release_image_is_the_public_image():
    """The workflow pushes the image that released add-ons run."""
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert f"IMAGE: {PUBLIC_IMAGE}\n" in workflow
