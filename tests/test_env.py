import re
from importlib.metadata import distributions, version

from packaging.utils import canonicalize_name

from orangecontrib.cloud.env import EnvironmentSpec, collect_environment

# Qt bindings, not pure-Python packages that merely use Qt (pyqtgraph, AnyQt).
QT_BINDING = re.compile(r"^(pyqt\d|pyside|shiboken)")


def test_collect_environment():
    spec = collect_environment()
    required = {p.name: p.version for p in spec.required}
    for name in ("orange3", "numpy", "scipy", "scikit-learn"):
        assert required[name] == version(name)
    assert "orange3-cloud" in required

    copied = [p.name for p in spec.required + spec.best_effort]
    assert not [n for n in copied if QT_BINDING.match(n)]
    installed_qt = {
        canonicalize_name(d.metadata["Name"])
        for d in distributions()
        if QT_BINDING.match(canonicalize_name(d.metadata["Name"]))
    }
    assert installed_qt <= {p.name for p in spec.excluded}

    assert EnvironmentSpec.from_json(spec.to_json()) == spec
