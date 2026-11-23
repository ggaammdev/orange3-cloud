"""Value mappings on cloud data: Edit Domain's category changes replay, steps
that took their values from the placeholder row are refused with a message
that says so, and classes of Orange's widget modules that the machine can't
import are refused by name. (The widgets themselves on a cloud table:
``test_supported_widgets.py``.)
"""

import pickle
import sys

import numpy as np
import pytest
from Orange.data import DiscreteVariable, Domain, Table
from Orange.preprocess import Remove
from Orange.widgets.data import oweditdomain as edit_domain
from Orange.widgets.data.owcreateclass import ValueFromDiscreteSubstring
from test_derive import CloudTableTest, small_table

from orangecontrib.cloud.protocol.errors import ErrorCode
from orangecontrib.cloud.protocol.pickles import Unavailable, dumps, loads
from orangecontrib.cloud.server.ops.derive import (
    AS_CATEGORICAL_MESSAGE,
    CREATE_CLASS_MESSAGE,
    NO_VALUES_MESSAGE,
    UNSUPPORTED_MESSAGE,
    replay,
)
from orangecontrib.cloud.server.worker import OperationError

EDIT_DOMAIN = "Orange.widgets.data.oweditdomain"
CREATE_CLASS = "Orange.widgets.data.owcreateclass"


def placeholder(table):
    """The add-on's placeholder for ``table``: one row, every value missing."""
    domain = table.domain
    return Table.from_numpy(
        domain, np.full((1, len(domain.attributes)), np.nan), np.full(1, np.nan)
    )


def edited(table, column, *transforms):
    """``table``'s domain with ``column`` changed by Edit Domain's ``transforms``
    (a text column moves to the metas), as Edit Domain makes it."""
    domain = table.domain
    var = edit_domain.apply_transform(domain[column], table, list(transforms))
    attributes = [v for v in domain.attributes if v.name != column]
    if var.is_primitive():
        return Domain([*attributes, var], domain.class_vars, domain.metas)
    return Domain(attributes, domain.class_vars, (*domain.metas, var))


def refusal(table, domain):
    with pytest.raises(OperationError) as e:
        replay(table, [("transform", domain)])
    assert e.value.code == ErrorCode.UNSUPPORTED_STEP
    return e.value.message


def test_lookup_checks():
    """Mappings of categories replay, also a merge of all of them by hand,
    unless they keep none of them."""
    table = small_table()  # color: red, green, blue
    merge = edit_domain.CategoriesMapping
    some = edited(table, "color", merge([("red", "warm"), ("green", "warm"), ("blue", "blue")]))
    np.testing.assert_array_equal(replay(table, [("transform", some)]).X[:, -1], [0, 0, 1, 0])
    for transform in (edit_domain.AsString(), edit_domain.AsContinuous()):
        replay(table, [("transform", edited(table, "color", transform))])

    everything = merge([("red", "all"), ("green", "all"), ("blue", "all")])
    merged = replay(table, [("transform", edited(table, "color", everything))])
    np.testing.assert_array_equal(merged.X[:, -1], [0, 0, 0, 0])
    # Purge Domain's "remove unused values" on the placeholder keeps none.
    purged = Remove(attr_flags=Remove.RemoveUnusedValues)(placeholder(table)).domain
    assert refusal(table, purged) == NO_VALUES_MESSAGE.format(name="color", source="color")
    # Edit Domain's "as categorical" took its values from the placeholder.
    as_categorical = edited(placeholder(table), "x1", edit_domain.AsCategorical())
    assert refusal(table, as_categorical) == AS_CATEGORICAL_MESSAGE.format(name="x1")


def test_widget_classes_without_qt(monkeypatch):
    """Where Orange's widget modules can't be imported (the machine has no
    Qt), their classes unpickle as Unavailable, and the machine refuses the
    step that uses one, naming the column."""
    table = small_table()
    as_text = edited(table, "x1", edit_domain.AsString())
    color = table.domain["color"]
    rules = ValueFromDiscreteSubstring(color, ["red", ""], map_values=[0, 1])
    warm = DiscreteVariable("warm", ("yes", "no"), compute_value=rules)
    create_class = Domain([*table.domain.attributes, warm], table.domain.class_vars)
    data = dumps((as_text, create_class))
    for module in (EDIT_DOMAIN, CREATE_CLASS):
        monkeypatch.setitem(sys.modules, module, None)  # import fails
    as_text, create_class = loads(data)

    unavailable = as_text["x1"].compute_value
    assert isinstance(unavailable, Unavailable)
    assert unavailable.orange_class == f"{EDIT_DOMAIN}.ToStringTransform"
    with pytest.raises(pickle.PicklingError):
        dumps(unavailable)
    message = UNSUPPORTED_MESSAGE.format(name="x1", kind="ToStringTransform")
    assert refusal(table, as_text) == message
    assert refusal(table, create_class) == CREATE_CLASS_MESSAGE.format(name="warm")


class TestMergeInfrequent(CloudTableTest):
    """Edit Domain's "Merge infrequent" on a cloud table merges nothing, in
    each mode: it counts the values of the placeholder row, which has none."""

    __test__ = True

    def merged(self, data, radio, spin, value):
        color = data.domain["color"]
        dialog = edit_domain.GroupItemsDialog(
            edit_domain.abstract(color), edit_domain.table_column_data(data, color), [], {}
        )
        getattr(dialog, spin).setValue(value)
        getattr(dialog, radio).setChecked(True)
        return dialog.get_merge_attributes()

    def test_merges_nothing(self):
        modes = [
            ("frequent_abs_radio", "frequent_abs_spin", 1001),  # fewer than 1001 rows
            ("frequent_rel_radio", "frequent_rel_spin", 33.4),  # less than 33.4 %
            ("n_values_radio", "n_values_spin", 1),  # all but the most frequent
        ]
        for mode in modes:
            assert self.merged(self.full, *mode), mode
            assert self.merged(self.data, *mode) == [], mode
