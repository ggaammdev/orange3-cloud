"""Orange's Preprocess widget's own preprocessor class on the machine, without Qt.

Preprocess's "Discretize → Remove numeric features" uses ``_NoneDisc``, a
class in Orange's widget module ``Orange.widgets.data.owpreprocess``, which
imports Qt; the machine has no Qt. :mod:`orangecontrib.cloud.protocol`
pickles it by name, and on the machine :class:`RemoveNumeric` takes its
place: it does the same (every numeric column is dropped) and pickles as
Orange's class again. Everything else in Preprocess's recipes comes from
``Orange.preprocess``, which needs no Qt.
"""

from __future__ import annotations

from typing import Any

from Orange.preprocess.discretize import Discretization

from orangecontrib.cloud.protocol.pickles import SUBSTITUTES

__all__ = ["RemoveNumeric", "register"]


class RemoveNumeric(Discretization):
    """Discretizes every numeric column into nothing, i.e. removes it."""

    orange_class = "Orange.widgets.data.owpreprocess._NoneDisc"

    def __call__(self, data: Any, variable: Any) -> None:
        return None


def register() -> None:
    """Make :mod:`orangecontrib.cloud.protocol` use the stand-in where
    Orange's widget module can't be imported."""
    SUBSTITUTES[RemoveNumeric.orange_class] = RemoveNumeric


register()
