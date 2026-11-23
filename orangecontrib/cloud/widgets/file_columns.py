"""Cloud File's column roles and types, edited with Orange's domain editor.

The widget keeps the columns as plain settings rows (name, type, role,
values); these helpers convert them to and from the domain editor's rows and
to the overrides the machine gets (:class:`~orangecontrib.cloud.protocol.tables.ColumnSpec`).
"""

from __future__ import annotations

from typing import Any

from AnyQt.QtCore import QModelIndex, Qt
from Orange.data import ContinuousVariable, DiscreteVariable, Domain, StringVariable, TimeVariable
from Orange.widgets.utils.domaineditor import Column, DomainEditor, VarTableModel

from orangecontrib.cloud.protocol.tables import ColumnRole, ColumnSpec, ColumnType

__all__ = ["ColumnsModel", "column_specs", "editor_row", "rows_from_domain", "setting_row"]

#: Orange variable types and their names in the API, in the domain editor's terms.
TYPE_NAMES = {
    DiscreteVariable: ColumnType.CATEGORICAL.value,
    ContinuousVariable: ColumnType.NUMERIC.value,
    StringVariable: ColumnType.TEXT.value,
    TimeVariable: ColumnType.DATETIME.value,
}
NAME_TYPES = {name: vartype for vartype, name in TYPE_NAMES.items()}
#: The domain editor's places (feature, target, meta, skip), as API roles.
ROLES = [ColumnRole.FEATURE.value, ColumnRole.TARGET.value, ColumnRole.META.value,
         ColumnRole.SKIP.value]  # fmt: skip


class ColumnsModel(VarTableModel):
    """Orange's column table, without renaming: the machine reads the file's
    own column names."""

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        """The cells' flags: names aren't editable."""
        flags = super().flags(index)
        if index.column() == Column.name:
            flags &= ~Qt.ItemFlag.ItemIsEditable
        return flags


def rows_from_domain(domain: Domain) -> list[dict[str, Any]]:
    """The domain editor's rows for a domain, as plain settings."""
    return [
        {
            "name": name,
            "type": TYPE_NAMES[vartype],
            "role": ROLES[place],
            "values": values,
            "numeric_ok": bool(numeric_ok),
        }  # fmt: skip
        for name, vartype, place, values, numeric_ok in DomainEditor.parse_domain(domain)
    ]


def editor_row(row: dict[str, Any]) -> list:
    """A settings row as a row of the domain editor."""
    return [row["name"], NAME_TYPES[row["type"]], ROLES.index(row["role"]), row["values"],
            row["numeric_ok"]]  # fmt: skip


def setting_row(row: list) -> dict[str, Any]:
    """A row of the domain editor as a settings row."""
    name, vartype, place, values, numeric_ok = row
    return {"name": name, "type": TYPE_NAMES[vartype], "role": ROLES[place], "values": values,
            "numeric_ok": bool(numeric_ok)}  # fmt: skip


def column_specs(rows: list[dict], inferred: list[dict]) -> list[ColumnSpec]:
    """Overrides for the machine: the roles and types that differ from what
    the machine inferred."""
    by_name = {r["name"]: r for r in inferred}
    specs = []
    for row in rows:
        orig = by_name.get(row["name"])
        role = row["role"] if orig is None or row["role"] != orig["role"] else None
        vartype = row["type"] if orig is None or row["type"] != orig["type"] else None
        if role or vartype:
            specs.append(ColumnSpec(name=row["name"], role=role, type=vartype))
    return specs
