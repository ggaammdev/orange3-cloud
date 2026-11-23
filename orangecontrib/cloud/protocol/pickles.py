"""Pickles between the add-on and the machine: :func:`dumps` and :func:`loads`,
and :func:`dump` and :func:`load` for files; :func:`digest` names a payload.

Both sides use them for every pickle, never plain ``pickle``. Their
:class:`Pickler` handles Orange objects that plain ``pickle`` can't, and the
classes of Orange's widget modules (which need Qt) that recipes and recorded
steps carry. Like the rest of :mod:`orangecontrib.cloud.protocol`, this
module imports nothing from the rest of the add-on, and no Qt. It imports
Orange's widget modules (which need Qt) only to unpickle their classes, when a
pickle has one: :func:`class_by_name` only those of :data:`BY_NAME_MODULES`.
"""

from __future__ import annotations

import copyreg
import hashlib
import importlib
import io
import pickle
from collections.abc import Callable, Mapping, Sequence
from enum import Enum
from typing import IO, Any

__all__ = [
    "BY_NAME_CLASSES",
    "BY_NAME_MODULES",
    "PICKLE_PROTOCOL",
    "SUBSTITUTES",
    "VARIABLE_CACHES",
    "Pickler",
    "Refs",
    "Unavailable",
    "class_by_name",
    "digest",
    "dump",
    "dumps",
    "load",
    "loads",
]

#: Pickle protocol for payloads (supported by every Python the add-on runs on).
PICKLE_PROTOCOL = 5

#: Classes that pickles carry by name and rebuild with :func:`_rebuild`.
#: Feature Constructor keeps its expressions in these, Create Class its rules,
#: and Preprocess's "Remove numeric features" is one, but their modules are
#: Orange's widget modules, which import Qt, and the machine has no Qt. Where
#: the module can't be imported, the stand-in registered in
#: :data:`SUBSTITUTES` is used instead (the server registers its own).
_FEATURE_CONSTRUCTOR = "Orange.widgets.data.owfeatureconstructor"
_CREATE_CLASS = "Orange.widgets.data.owcreateclass"
BY_NAME_CLASSES = frozenset(
    [
        *(
            f"{_FEATURE_CONSTRUCTOR}.{name}"
            for name in ("FeatureFunc", "MappingTransformCast", "DateTimeCast")
        ),
        *(
            f"{_CREATE_CLASS}.{name}"
            for name in ("ValueFromStringSubstring", "ValueFromDiscreteSubstring")
        ),
        "Orange.widgets.data.owpreprocess._NoneDisc",
    ]
)

#: The modules of :data:`BY_NAME_CLASSES`, the only modules that
#: :func:`class_by_name` imports. Importing this package imports none of them.
BY_NAME_MODULES = frozenset(name.rpartition(".")[0] for name in BY_NAME_CLASSES)

#: Orange's widget modules: their other classes unpickle as
#: :class:`Unavailable` where the module can't be imported.
_WIDGET_MODULES = "Orange.widgets."

#: Stand-ins for :data:`BY_NAME_CLASSES` by qualified name, used only where
#: the original's module can't be imported. A stand-in names the class it
#: stands for in its ``orange_class`` attribute, so it pickles as that class.
SUBSTITUTES: dict[str, Callable[..., Any]] = {}


def _qualified_class_name(obj: Any) -> str:
    cls = type(obj)
    return getattr(cls, "orange_class", None) or f"{cls.__module__}.{cls.__qualname__}"


def class_by_name(name: str) -> Any:
    """The class ``name`` of :data:`BY_NAME_CLASSES`: Orange's own, or its
    stand-in where Orange's widget module can't be imported. Other names are
    refused without importing anything."""
    if name not in BY_NAME_CLASSES:
        raise pickle.UnpicklingError(f"'{name}' can't be rebuilt from a pickle.")
    module, _, qualname = name.rpartition(".")
    try:
        return getattr(importlib.import_module(module), qualname)
    except ImportError:
        if name not in SUBSTITUTES:
            raise
        return SUBSTITUTES[name]


def _rebuild(name: str, args: tuple, state: dict | None, new: bool) -> Any:
    """Unpickle an object of a class in :data:`BY_NAME_CLASSES`."""
    cls = class_by_name(name)
    obj = cls.__new__(cls, *args) if new else cls(*args)
    if state and hasattr(obj, "__setstate__"):
        obj.__setstate__(state)  # e.g. Orange's transformations rebuild a cache
    elif state:
        obj.__dict__.update(state)
    return obj


class Unavailable:
    """Stands for an object of a class in one of Orange's widget modules that
    this side can't import (the machine has no Qt) and that has no stand-in.

    It keeps the object's state and names the class in ``orange_class``, so
    the machine can refuse a step that uses it with a message that names the
    column. It can't compute anything and can't be pickled again.
    """

    orange_class = ""

    def __new__(cls, *args: Any, **kwargs: Any) -> Unavailable:
        return super().__new__(cls)

    def __init__(self, *args: Any, **kwargs: Any):
        self.args = args

    def __setstate__(self, state: Any) -> None:
        """Keep the state as attributes, as plain ``pickle`` would."""
        for part in state if isinstance(state, tuple) else (state,):
            self.__dict__.update(part or {})

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse: the class isn't available here."""
        raise TypeError(f"'{self.orange_class}' isn't available on this machine.")

    def __reduce_ex__(self, protocol: Any) -> Any:
        raise pickle.PicklingError(f"'{self.orange_class}' isn't available on this machine.")


_UNAVAILABLE: dict[str, type[Unavailable]] = {}


def _unavailable(name: str) -> type[Unavailable]:
    """A subclass of :class:`Unavailable` that stands for class ``name``."""
    if name not in _UNAVAILABLE:
        qualname = name.rpartition(".")[2]
        _UNAVAILABLE[name] = type(qualname, (Unavailable,), {"orange_class": name})
    return _UNAVAILABLE[name]


#: Orange's distance matrix, which travels without a copy of its values.
DIST_MATRIX = "Orange.misc.distmatrix.DistMatrix"


def _dist_matrix(values: Any, row_items: Any, col_items: Any, axis: int) -> Any:
    """Unpickle Orange's ``DistMatrix``."""
    from Orange.misc.distmatrix import DistMatrix

    return DistMatrix(values, row_items, col_items, axis)


def _enum_member(module: str, path: str, member: str) -> Any:
    """Unpickle a member of an Enum class kept as a class attribute."""
    owner: Any = importlib.import_module(module)
    for part in path.split("."):
        owner = getattr(owner, part)
    return owner[member]


def _enum_path(enum_class: type[Enum]) -> str | None:
    """Where a class attribute of the Enum's module holds it, e.g.
    ``FilterContinuous.Type``; ``None`` if nowhere."""
    module = importlib.import_module(enum_class.__module__)
    for name, owner in vars(module).items():
        if isinstance(owner, type):
            for attr, value in vars(owner).items():
                if value is enum_class:
                    return f"{name}.{attr}"
    return None


class Pickler(pickle.Pickler):
    """Pickles Orange objects that plain ``pickle`` can't, or that need Qt.

    * Members of Enums made with Orange's functional ``Enum(...)`` inside a
      class (``FilterContinuous.Type``, ``FilterString.Type``, the operators of
      Select Rows' conditions) aren't found by their qualified name, so they
      travel as the class attribute that holds them.
    * Objects of :data:`BY_NAME_CLASSES` travel by name (see there).
    * Orange's ``DistMatrix`` travels as a plain array plus its items, so
      that pickling it doesn't copy its values (``DistMatrix.__reduce__``
      makes a copy, gigabytes for a large matrix).
    """

    def reducer_override(self, obj: Any) -> Any:
        """The reduction for the cases above; ``NotImplemented`` otherwise."""
        if isinstance(obj, Enum):
            return self._reduce_enum(obj)
        name = _qualified_class_name(obj)
        if name == DIST_MATRIX:
            import numpy as np

            values = obj.view(np.ndarray)
            return _dist_matrix, (values, obj.row_items, obj.col_items, obj.axis)
        if name in BY_NAME_CLASSES:
            func, args, *rest = obj.__reduce_ex__(PICKLE_PROTOCOL)
            new = func is copyreg.__newobj__
            state = rest[0] if rest else None
            return _rebuild, (name, tuple(args[1:] if new else args), state, new)
        return NotImplemented

    @staticmethod
    def _reduce_enum(member: Enum) -> Any:
        cls = type(member)
        module = importlib.import_module(cls.__module__)
        if getattr(module, cls.__qualname__, None) is cls:
            return NotImplemented
        path = _enum_path(cls)
        if path is None:
            return NotImplemented
        return _enum_member, (cls.__module__, path, member.name)


#: ``refs`` of :func:`dumps` and :func:`loads`: named sequences of objects
#: that both sides already have, e.g. the columns of a job's input table.
Refs = Mapping[str, Sequence[Any]]


class _RefPickler(Pickler):
    """Writes the objects of ``refs`` as ``("ref", name, position)``."""

    def __init__(self, file: IO[bytes], refs: Refs):
        super().__init__(file, protocol=PICKLE_PROTOCOL)
        self._refs = {
            id(obj): (name, i) for name, objs in refs.items() for i, obj in enumerate(objs)
        }

    def persistent_id(self, obj: Any) -> tuple[str, str, int] | None:
        found = self._refs.get(id(obj))
        return None if found is None else ("ref", *found)


class _Unpickler(pickle.Unpickler):
    """Reads the references that :class:`_RefPickler` writes, and classes of
    Orange's widget modules that can't be imported as :class:`Unavailable`."""

    def __init__(self, file: IO[bytes], refs: Refs | None):
        super().__init__(file)
        self._refs = refs or {}

    def find_class(self, module: str, name: str) -> Any:
        """The class, or a stand-in for one in an unavailable widget module."""
        try:
            return super().find_class(module, name)
        except ImportError:
            if not module.startswith(_WIDGET_MODULES):
                raise
            return _unavailable(f"{module}.{name}")

    def persistent_load(self, pid: Any) -> Any:
        if isinstance(pid, tuple) and len(pid) == 3 and pid[0] == "ref":
            objs, position = self._refs.get(pid[1], ()), pid[2]
            if isinstance(position, int) and 0 <= position < len(objs):
                return objs[position]
        raise pickle.UnpicklingError(f"The pickle refers to an unknown object {pid!r}.")


#: Attributes that Orange's widgets set on variables as they draw them
#: (colors and palettes, cached on first use). They don't change what a
#: recorded step computes, but they would change its pickle.
VARIABLE_CACHES = frozenset({"_colors", "_palette"})


class _DigestPickler(_RefPickler):
    """Pickles variables without :data:`VARIABLE_CACHES`, for :func:`digest`."""

    def reducer_override(self, obj: Any) -> Any:
        reduced = super().reducer_override(obj)
        if reduced is NotImplemented and _is_variable(obj):
            func, args, *rest = obj.__reduce__()
            state = rest[0] if rest else None
            if isinstance(state, dict) and VARIABLE_CACHES & state.keys():
                state = {k: v for k, v in state.items() if k not in VARIABLE_CACHES}
                return func, args, state
        return reduced


def _is_variable(obj: Any) -> bool:
    from Orange.data import Variable

    return isinstance(obj, Variable)


def digest(obj: Any, refs: Refs | None = None) -> str:
    """The SHA-256 of ``obj`` pickled as :func:`dumps` does, but without the
    caches Orange's widgets keep on variables (:data:`VARIABLE_CACHES`): the
    same recorded steps have the same digest before and after a widget drew
    their columns."""
    buffer = io.BytesIO()
    _DigestPickler(buffer, refs or {}).dump(obj)
    return hashlib.sha256(buffer.getvalue()).hexdigest()


def dump(obj: Any, file: IO[bytes], refs: Refs | None = None) -> None:
    """Pickle ``obj`` into ``file`` with :class:`Pickler` and
    :data:`PICKLE_PROTOCOL`. Objects in ``refs`` (by identity) are written as
    references to them."""
    pickler = _RefPickler(file, refs) if refs else Pickler(file, protocol=PICKLE_PROTOCOL)
    pickler.dump(obj)


def dumps(obj: Any, refs: Refs | None = None) -> bytes:
    """Pickle a payload as :func:`dump` does, into bytes."""
    buffer = io.BytesIO()
    dump(obj, buffer, refs)
    return buffer.getvalue()


def load(file: IO[bytes], refs: Refs | None = None) -> Any:
    """Unpickle an object that :func:`dump` wrote to ``file``, with the same
    ``refs``."""
    return _Unpickler(file, refs).load()


def loads(data: bytes, refs: Refs | None = None) -> Any:
    """Unpickle a payload made by :func:`dumps`, with the same ``refs``."""
    return load(io.BytesIO(data), refs)
