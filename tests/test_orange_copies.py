"""Code that follows Orange's own code, because Orange's widgets can't be
subclassed or need Qt (the machine has none), names here the Orange functions
and classes it follows, with a fingerprint of each one's source.

When a newer Orange changes one of them, this test fails (in the weekly check
against the newest Orange) and names the copy to update. Update the copy, then
record the new fingerprints: ``python tests/test_orange_copies.py`` prints
them. The copies follow Orange 3.41, so older Orange (CI's) isn't compared.
"""

import hashlib
import importlib
import inspect
import math
import re

import pytest
from Orange.version import short_version

#: The Orange version the copies follow; older versions aren't compared.
FOLLOWS = (3, 41)

_DISCRETIZE = "Orange.widgets.data.owdiscretize"
_EDIT_DOMAIN = "Orange.widgets.data.oweditdomain"
_CREATE_CLASS = "Orange.widgets.data.owcreateclass"
_FEATURE_CONSTRUCTOR = "Orange.widgets.data.owfeatureconstructor"
_RANK = "Orange.widgets.data.owrank"

#: Each copy in the add-on, and the fingerprint of each Orange function or
#: class (``module:qualified name``) it follows.
COPIES: dict[str, dict[str, str]] = {
    "protocol/recipes.py: DiscretizeColumns and its functions": {
        f"{_DISCRETIZE}:Methods": "fb13a5ff84594bd5",
        f"{_DISCRETIZE}:variable_key": "56528c22056bbc22",
        f"{_DISCRETIZE}:_fixed_width_discretization": "240fba263651e010",
        f"{_DISCRETIZE}:_fixed_time_width_discretization": "4920eee78815b6b5",
        f"{_DISCRETIZE}:_mdl_discretization": "01467d4400782a6f",
        f"{_DISCRETIZE}:_custom_discretization": "503733646253ae5b",
        f"{_DISCRETIZE}:OWDiscretize._discretize_var": "15b14a8eb15b44a7",
    },
    "protocol/recipes.py: ColumnCategories and its functions": {
        f"{_EDIT_DOMAIN}:GroupItemsDialog": "48eb7fa888c8a262",
        f"{_EDIT_DOMAIN}:categorical_from_vector": "844ade8774d8465b",
        f"{_EDIT_DOMAIN}:apply_reinterpret_c": "e0e5d70e461d4eba",
        f"{_EDIT_DOMAIN}:apply_reinterpret_s": "9e29d2f38246da1e",
        f"{_EDIT_DOMAIN}:apply_transform_discete": "83f5888bbec468ae",
    },
    "protocol/recipes.py: CreateClass and its functions": {
        f"{_CREATE_CLASS}:unique_in_order_mapping": "88b43fe63ec0e475",
        f"{_CREATE_CLASS}:OWCreateClass.class_labels": "72a6c5266ec78364",
        f"{_CREATE_CLASS}:OWCreateClass.update_counts": "51ccd0e8855a34f5",
        f"{_CREATE_CLASS}:OWCreateClass._create_variable": "182e2843897a5498",
    },
    "widgets/owclouddiscretize.py: OWCloudDiscretize": {
        f"{_DISCRETIZE}:OWDiscretize": "593f564d5874458a",
    },
    "widgets/owcloudcategories.py: OWCloudCategories": {
        f"{_EDIT_DOMAIN}:GroupItemsDialog": "48eb7fa888c8a262",
    },
    "widgets/owcloudcreateclass.py: OWCloudCreateClass": {
        f"{_CREATE_CLASS}:OWCreateClass": "616918f77c6840ae",
    },
    "widgets/owcloudrank.py: OWCloudRank": {
        f"{_RANK}:OWRank": "b65bd2a45c5dcd6e",
    },
    "widgets/owcloudpurgedomain.py: OWCloudPurgeDomain": {
        "Orange.widgets.data.owpurgedomain:OWPurgeDomain": "904d546404e30355",
    },
    "server/ops/rank.py: method_scores and scorer_scores": {
        f"{_RANK}:get_method_scores": "b3cec0b202a23523",
        f"{_RANK}:get_scorer_scores": "2b1870262143e94f",
    },
    "server/expressions.py: the stand-ins for Feature Constructor's classes": {
        f"{_FEATURE_CONSTRUCTOR}:__ALLOWED": "77d58fef28dbe103",
        f"{_FEATURE_CONSTRUCTOR}:__GLOBALS": "0f5383de118cd322",
        f"{_FEATURE_CONSTRUCTOR}:make_lambda": "d111114377543298",
        f"{_FEATURE_CONSTRUCTOR}:FeatureFunc": "141380dc2b0d74d0",
        f"{_FEATURE_CONSTRUCTOR}:MappingTransformCast": "1a197c002408ed34",
        f"{_FEATURE_CONSTRUCTOR}:cast_datetime": "eb8562d77de74859",
        f"{_FEATURE_CONSTRUCTOR}:DateTimeCast": "e49c6ced5ce850be",
    },
    "server/expressions.py: the stand-ins for Create Class's classes": {
        f"{_CREATE_CLASS}:map_by_substring": "f5566cb2bd034e21",
        f"{_CREATE_CLASS}:_EqHashMixin": "f443f59cbbdd8018",
        f"{_CREATE_CLASS}:ValueFromStringSubstring": "aa0f273b7c439665",
        f"{_CREATE_CLASS}:ValueFromDiscreteSubstring": "a9ba82e499692224",
    },
    "server/preprocessors.py: RemoveNumeric": {
        "Orange.widgets.data.owpreprocess:_NoneDisc": "0fb6116f6037dce2",
    },
}


def orange_version() -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", short_version)[:2])


def fingerprint(name: str) -> str:
    """A short hash of the source of Orange's function or class ``name``, or
    of the value of a module's variable (a dict by its keys, without the
    names of ``math``, which differ between Python versions)."""
    module, _, qualname = name.partition(":")
    obj = importlib.import_module(module)
    for part in qualname.split("."):
        obj = getattr(obj, part)
    if inspect.isclass(obj) or inspect.isroutine(obj):
        text = inspect.getsource(obj)
    elif isinstance(obj, dict):
        text = repr(sorted(key for key in obj if not hasattr(math, key)))
    else:
        text = repr(obj)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


@pytest.mark.skipif(
    orange_version() < FOLLOWS, reason=f"the copies follow Orange {FOLLOWS[0]}.{FOLLOWS[1]}"
)
@pytest.mark.parametrize("copy", COPIES)
def test_followed_orange_code_is_unchanged(copy):
    changed = [name for name, recorded in COPIES[copy].items() if fingerprint(name) != recorded]
    assert not changed, (
        f"{copy} follows Orange code that Orange {short_version} changed: {', '.join(changed)}. "
        "Update the copy, then record the new fingerprints (python tests/test_orange_copies.py)."
    )


if __name__ == "__main__":
    for copy, followed in COPIES.items():
        print(f"    {copy!r}: {{")
        for name in followed:
            print(f"        {name!r}: {fingerprint(name)!r},")
        print("    },")
