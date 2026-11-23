"""Cloud Create Class: its recipe keeps Create Class's rules as Create Class
does on the whole table (text and categorical columns, case sensitivity,
matching at the beginning, regular expressions), the machine's stand-ins
for Create Class's classes compute the same and pickle back as Orange's,
and on a small cloud table the widget gives Create Class's columns, rows and
counts on the full table; the step replays, also under Orange's steps after
it, while Orange's own Create Class on the cloud table stays refused.
"""

import sys

import numpy as np
import pytest
from Orange.data import ContinuousVariable, DiscreteVariable, Domain, StringVariable, Table
from Orange.widgets.data.owcreateclass import OWCreateClass, ValueFromStringSubstring
from Orange.widgets.tests import base  # not WidgetTest itself: pytest would collect it
from test_derive import CloudTableTest

from orangecontrib.cloud.client.api import CloudUnsupportedStepError
from orangecontrib.cloud.data.remote_table import LOCAL_DATA, resolve
from orangecontrib.cloud.protocol.pickles import dumps, loads
from orangecontrib.cloud.protocol.recipes import CreateClass, value_counts
from orangecontrib.cloud.server import expressions
from orangecontrib.cloud.widgets.owcloudcreateclass import OWCloudCreateClass

CREATE_CLASS = "Orange.widgets.data.owcreateclass"

#: (column, rules, case sensitive, match beginning, regular expressions)
CASES = [
    ("c", [("A", "a"), ("B", "b"), ("", "")], False, False, False),
    ("c", [("x", "an"), ("", "")], True, False, False),
    ("c", [("", "c"), ("C5", "d"), ("", ""), ("", "zz")], False, False, False),
    ("s", [("A", "ap"), ("B", "an"), ("", ""), ("D", "fig")], False, False, False),
    ("s", [("A", "a"), ("B", "")], False, True, False),
    ("s", [("A", "^a"), ("A", "e$"), ("", "")], False, False, True),
    ("s", [("A", "^A"), ("B", "")], True, False, True),
]


def local_table():
    """A categorical column with a value no row has, a text column with
    missing values, a numeric feature and a target."""
    rng = np.random.default_rng(5)
    n = 300
    c = DiscreteVariable("c", values=("apple", "Banana", "cherry", "date", "unused"))
    domain = Domain(
        [ContinuousVariable("x"), c],
        DiscreteVariable("y", values=("no", "yes")),
        metas=[StringVariable("s")],
    )
    X = np.column_stack([rng.normal(size=n), rng.choice(4, n)]).astype(float)
    words = rng.choice(np.array(["apple pie", "Banana", "cherry", "date", "fig", "Apricot"]), n)
    words = words.astype(object)
    words[::13] = ""
    return Table.from_numpy(domain, X, rng.integers(0, 2, n), words.reshape(-1, 1))


def orange_create_class(test, data, column, rules, flags=(False, False, False), name="class"):
    """Orange's Create Class on ``data``: its output and its counts."""
    w = test.create_widget(OWCreateClass)
    test.send_signal(w.Inputs.data, data, widget=w)
    set_rules(w, data, column, rules, flags, name)
    return test.get_output(w.Outputs.data, widget=w), [tuple(c) for c in w.match_counts]


def set_rules(w, data, column, rules, flags, name):
    """Set Create Class's (or Cloud Create Class's) column, rules and options, and apply."""
    w.attribute = data.domain[column]
    w.class_name = name
    w.case_sensitive, w.match_beginning, w.regular_expressions = flags
    w.active_rules[:] = [list(rule) for rule in rules]
    w.update_rules()
    w.apply()


def assert_same_domain(got, expected):
    assert got == expected
    for var in got.variables + got.metas:
        if var.is_discrete:
            assert var.values == expected[var.name].values, var.name


class TestRecipe(base.WidgetTest):
    def test_same_as_create_class(self):
        """The recipe on a local table gives Create Class's class column and counts."""
        table = local_table()
        for column, rules, *flags in CASES:
            expected, expected_counts = orange_create_class(self, table, column, rules, flags)
            recipe = CreateClass(
                column,
                "class",
                rules,
                case_sensitive=flags[0],
                match_beginning=flags[1],
                regular_expressions=flags[2],
            )
            counts = value_counts(table, table.domain[column])
            assert recipe.match_counts(counts.values, counts.counts) == expected_counts
            out = recipe(table)
            assert_same_domain(out.domain, expected.domain)
            np.testing.assert_array_equal(out.Y, expected.Y)

    def test_stand_ins_without_qt(self):
        """Where Create Class's module can't be imported (the machine has no
        Qt), the recipe uses the machine's stand-ins: they compute the same
        values and pickle back as Orange's own classes."""
        table = local_table()
        recipes = [CreateClass("s", "k", CASES[3][1]), CreateClass("c", "k", CASES[0][1])]
        expected = [recipe(table) for recipe in recipes]
        original = sys.modules[CREATE_CLASS]
        sys.modules[CREATE_CLASS] = None  # import fails
        try:
            got = [recipe(table) for recipe in recipes]
            data = dumps([out.domain for out in got])
        finally:
            sys.modules[CREATE_CLASS] = original
        for out, domain, orange in zip(got, loads(data), expected, strict=True):
            assert type(out.domain.class_var.compute_value).__module__ == expressions.__name__
            np.testing.assert_array_equal(out.Y, orange.Y)
            assert_same_domain(domain, orange.domain)
            assert type(domain.class_var.compute_value).__module__ == CREATE_CLASS
        assert isinstance(loads(data)[0].class_var.compute_value, ValueFromStringSubstring)


class TestCloudCreateClass(CloudTableTest):
    __test__ = True

    def cloud_create_class(self, data, column, rules, flags=(False, False, False), name="class"):
        w = self.create_widget(OWCloudCreateClass)
        self.send_signal(w.Inputs.data, data, widget=w)
        self.wait_until_finished(w, timeout=120000)
        set_rules(w, data, column, rules, flags, name)
        self.wait_until_finished(w, timeout=120000)
        return w, self.get_output(w.Outputs.data, widget=w)

    def rows(self, data):
        return self.client.table_rows(resolve(data, self.client, self.machine_id), 0, self.N_ROWS)

    def assert_same(self, got, expected):
        assert_same_domain(got.domain, expected.domain)
        np.testing.assert_array_equal(got.X, expected.X)
        np.testing.assert_array_equal(got.Y, expected.Y)
        np.testing.assert_array_equal(got.metas, expected.metas)

    def test_same_as_create_class(self):
        cases = [
            ("color", [("warm", "r"), ("cold", "b"), ("", "")], (False, False, False)),
            ("id", [("one", "row1"), ("two", "2"), ("", "5$"), ("", "")], (False, False, True)),
            ("id", [("early", "row1"), ("rest", ""), ("never", "x")], (False, True, False)),
        ]
        for column, rules, flags in cases:
            w, cloud = self.cloud_create_class(self.data, column, rules, flags)
            assert not w.Error.active, [str(m) for m in w.Error.active]
            local, counts = orange_create_class(self, self.full, column, rules, flags)
            assert [kind for kind, _ in cloud.steps][-1] == "preprocess"
            assert w.match_counts == counts and len(counts) > 1
            assert w.counts[0][0].text() == str(counts[0][0])
            self.assert_same(self.rows(cloud), local)
        # Orange's own steps after it replay on the new class column.
        self.assert_same(
            self.rows(self.select_columns(cloud, drop="x1")),
            self.select_columns(local, drop="x1"),
        )

    def test_orange_create_class_refused(self):
        """Orange's Create Class on the cloud table keeps only its first rule,
        and the machine refuses its step, naming Cloud Create Class."""
        rules = [("warm", "r"), ("cold", "b"), ("", "")]
        cloud, counts = orange_create_class(self, self.data, "color", rules)
        assert len(counts) == 1
        with pytest.raises(CloudUnsupportedStepError, match="Cloud Create Class"):
            resolve(cloud, self.client, self.machine_id)

    def test_checks_and_local_data(self):
        """Create Class's checks of the settings, and local data refused."""
        w, out = self.cloud_create_class(self.data, "color", [("a", "r")], name="x1")
        assert w.Error.class_name_duplicated.is_shown() and out is None
        w, out = self.cloud_create_class(self.data, "id", [("a", "(")], (False, False, True))
        assert w.Error.invalid_regular_expression.is_shown() and out is None
        w.regular_expressions = False
        self.send_signal(w.Inputs.data, self.full, widget=w)
        assert LOCAL_DATA in str(w.Error.cloud_error)
        assert self.get_output(w.Outputs.data, widget=w) is None

    def test_no_output_from_previous_data(self):
        """New data whose rules weren't applied (the column changed before
        the job ended) gives no output, not the one from the previous data."""
        w, first = self.cloud_create_class(self.data, "color", [("warm", "r"), ("", "")])
        assert first is not None
        other = self.select_columns(self.data, drop="x1")
        self.send_signal(w.Inputs.data, other, widget=w)
        w.attribute = other.domain["id"]
        w._attribute_changed()  # as the combo box does; cancels the apply
        self.wait_until_finished(w, timeout=120000)
        assert self.get_output(w.Outputs.data, widget=w) is None
