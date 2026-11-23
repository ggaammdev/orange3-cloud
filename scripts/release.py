#!/usr/bin/env python
"""Prepare and check a release of orange3-cloud. The release workflow
(``.github/workflows/release.yml``) runs it on its own checkout::

    python scripts/release.py prepare VERSION [--publish] [--notes FILE]
    python scripts/release.py check-dist dist VERSION

``prepare`` checks the version and gets the checkout ready to build. It
changes files in place, so run it on a throwaway checkout:

- with ``--publish``, the version must be a final release ``X.Y.Z``, equal to
  the one in ``pyproject.toml`` (so the tag matches the code), and
  ``CHANGELOG.md`` must have a section for it (``## X.Y.Z``, ``## [X.Y.Z]``,
  optionally followed by a date);
- without it (a trial), any PEP 440 version such as ``1.0.0rc1`` is allowed,
  and it is written into ``pyproject.toml``.

Either way, the README's relative links and images become absolute links to
the repository at the tag ``vVERSION``, because PyPI shows the README
without the repository's files, and the version's changelog section is
written to ``--notes``.

``check-dist`` checks that the folder holds one sdist and one wheel of the
version, and that neither contains an internal file (the files git ignores
because they are never published).
"""

from __future__ import annotations

import argparse
import re
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "ggaammdev/orange3-cloud"

#: A final release: what ``--publish`` accepts.
FINAL_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
#: A normalized PEP 440 public version, e.g. ``1.0.0rc1`` or ``1.1.0.dev3``.
ANY_VERSION = re.compile(r"^\d+(\.\d+)*((a|b|rc)\d+)?(\.post\d+)?(\.dev\d+)?$")
_PROJECT_VERSION = re.compile(r'^version = "([^"]*)"$', re.MULTILINE)

#: Names that never go into a package, anywhere in it.
INTERNAL_NAMES = frozenset(
    {
        "CLAUDE.md",
        "CLAUDE.local.md",
        "PLAN.md",
        "internal",
        ".claude",
        ".mcp.json",
        ".orange3-cloud-dev.env",
    }
)

#: ``[label](target)`` and ``![alt](target)``.
_LINK = re.compile(r"(!?)\[([^\]]*)\]\(([^)\s]+)\)")
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)


class ReleaseError(Exception):
    """The checkout or the built packages can't be released; the message says why."""


def readme_for_pypi(text: str, repository: str, tag: str) -> str:
    """``text`` with relative links pointing to ``repository`` on GitHub at
    ``tag``: images to their raw files, other links (and ``#anchors``, which
    are the README's own) to GitHub's pages."""

    def absolute(match: re.Match) -> str:
        bang, label, target = match.groups()
        if _SCHEME.match(target):
            return match.group(0)
        if target.startswith("#"):
            target = "README.md" + target
        if bang:
            base = f"https://raw.githubusercontent.com/{repository}/{tag}/"
        else:
            base = f"https://github.com/{repository}/blob/{tag}/"
        return f"{bang}[{label}]({base}{target.removeprefix('./')})"

    return _LINK.sub(absolute, text)


def changelog_section(text: str, version: str) -> str | None:
    """The body of ``version``'s section in a changelog, or ``None``."""
    heading = re.compile(rf"^##\s+\[?v?{re.escape(version)}\]?(\s|$)")
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if heading.match(line)), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start + 1 : end]).strip()


def set_project_version(pyproject: str, version: str) -> str:
    """``pyproject`` (the text of ``pyproject.toml``) with ``version``."""
    project_version(pyproject)  # exactly one version line
    return _PROJECT_VERSION.sub(f'version = "{version}"', pyproject)


def project_version(pyproject: str) -> str:
    """The version in ``pyproject`` (the text of ``pyproject.toml``)."""
    found = _PROJECT_VERSION.findall(pyproject)
    if len(found) != 1:
        raise ReleaseError('pyproject.toml must have exactly one line `version = "..."`.')
    return found[0]


def prepare(root: Path, version: str, publish: bool, repository: str = REPOSITORY) -> str:
    """Check ``version`` and get the checkout at ``root`` ready to build it
    (see the module's docstring); return the release notes."""
    pyproject_path = root / "pyproject.toml"
    pyproject = pyproject_path.read_text()
    changelog = root / "CHANGELOG.md"
    notes = changelog_section(changelog.read_text(), version) if changelog.exists() else None
    if publish:
        if not FINAL_VERSION.match(version):
            raise ReleaseError(f"A release version is X.Y.Z, not {version!r}.")
        if (committed := project_version(pyproject)) != version:
            raise ReleaseError(
                f"pyproject.toml has version {committed}, not {version}: commit the version first."
            )
        if not notes:
            raise ReleaseError(f"CHANGELOG.md has no section for {version}.")
    else:
        if not ANY_VERSION.match(version):
            raise ReleaseError(f"Not a normalized PEP 440 version: {version!r}.")
        pyproject_path.write_text(set_project_version(pyproject, version))
    readme = root / "README.md"
    readme.write_text(readme_for_pypi(readme.read_text(), repository, f"v{version}"))
    return notes or f"(CHANGELOG.md has no section for {version}.)"


def internal_members(names: list[str]) -> list[str]:
    """The paths in ``names`` (members of a package) that are internal files."""
    return [name for name in names if INTERNAL_NAMES.intersection(name.split("/"))]


def check_dist(folder: Path, version: str) -> list[Path]:
    """Check the sdist and wheel of ``version`` in ``folder``; return them."""
    sdists = sorted(folder.glob("*.tar.gz"))
    wheels = sorted(folder.glob("*.whl"))
    expected_sdist = f"orange3_cloud-{version}.tar.gz"
    if [p.name for p in sdists] != [expected_sdist]:
        raise ReleaseError(
            f"Expected one sdist {expected_sdist}, found {[p.name for p in sdists]}."
        )
    if len(wheels) != 1 or not wheels[0].name.startswith(f"orange3_cloud-{version}-"):
        raise ReleaseError(f"Expected one wheel of {version}, found {[p.name for p in wheels]}.")
    with tarfile.open(sdists[0]) as sdist:
        sdist_names = sdist.getnames()
    with zipfile.ZipFile(wheels[0]) as wheel:
        wheel_names = wheel.namelist()
    for path, names in ((sdists[0], sdist_names), (wheels[0], wheel_names)):
        if found := internal_members(names):
            raise ReleaseError(f"{path.name} contains internal files: {', '.join(found)}")
        size_mb = path.stat().st_size / 1e6
        print(f"{path.name}: {len(names)} files, {size_mb:.1f} MB, no internal files")
    return [sdists[0], wheels[0]]


def main(argv: list[str] | None = None) -> int:
    """Run the command line."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare", help="check the version and prepare the checkout")
    prep.add_argument("version")
    prep.add_argument("--publish", action="store_true", help="a release to publish")
    prep.add_argument("--notes", type=Path, help="write the release notes here")
    prep.add_argument("--repository", default=REPOSITORY, help="OWNER/NAME on GitHub")
    check = commands.add_parser("check-dist", help="check the built sdist and wheel")
    check.add_argument("folder", type=Path)
    check.add_argument("version")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            notes = prepare(ROOT, args.version, args.publish, args.repository)
            if args.notes:
                args.notes.write_text(notes + "\n")
            print(f"Prepared {args.version} ({'release' if args.publish else 'trial'}).")
        else:
            check_dist(args.folder, args.version)
    except ReleaseError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
