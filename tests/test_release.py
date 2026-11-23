"""The release's checks (``scripts/release.py``, run by the release workflow)."""

import importlib.util
import io
import re
import shutil
import tarfile
import zipfile
from pathlib import Path

import pytest
import yaml

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
    with pytest.raises(release.ReleaseError, match="X.Y.Z"):
        release.prepare(tmp_path, "1.2.3\n", publish=True)
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


def test_published_versions_are_refused():
    """A release stops before it publishes if PyPI or GitHub has the version,
    or if either can't be asked."""
    asked = []

    def status(answers):
        def get(url, token):
            asked.append((url, token))
            return answers["pypi" if "pypi.org" in url else "github"]

        return get

    release.check_unpublished("1.2.3", "o/r", "t", status({"pypi": 404, "github": 404}))
    assert asked == [
        ("https://pypi.org/pypi/orange3-cloud/1.2.3/json", None),
        ("https://api.github.com/repos/o/r/releases/tags/v1.2.3", "t"),
    ]
    with pytest.raises(release.ReleaseError, match="PyPI has 1.2.3"):
        release.check_unpublished("1.2.3", "o/r", None, status({"pypi": 200, "github": 404}))
    with pytest.raises(release.ReleaseError, match="GitHub releases has 1.2.3"):
        release.check_unpublished("1.2.3", "o/r", None, status({"pypi": 404, "github": 200}))
    with pytest.raises(release.ReleaseError, match="Couldn't check GitHub releases.*HTTP 403"):
        release.check_unpublished("1.2.3", "o/r", None, status({"pypi": 404, "github": 403}))


def test_one_approval_gates_publishing_and_trials_need_none():
    """The image job, the first that publishes, runs only for a tag and in the
    "release" environment, and PyPI and the release need it; a trial builds the
    same image in a job without an environment."""
    jobs = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text())["jobs"]
    publishing = jobs["image"]
    trial = jobs["image-trial"]
    assert publishing["environment"] == "release"
    assert publishing["if"] == "github.event_name == 'push'"
    assert "environment" not in trial
    assert trial["if"] == "github.event_name == 'workflow_dispatch'"
    assert "image" in jobs["pypi"]["needs"] and "pypi" in jobs["release"]["needs"]

    def build_inputs(job):
        (step,) = [s for s in job["steps"] if s.get("uses", "").startswith("docker/build-push")]
        return dict(step["with"])

    published, tried = build_inputs(publishing), build_inputs(trial)
    assert (published.pop("push"), tried.pop("push")) == (True, False)
    assert published == tried
