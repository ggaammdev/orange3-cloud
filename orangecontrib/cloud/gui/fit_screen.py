"""A scroll area as tall as its content would like, and a window that grows to
show that content whole, as far as its screen allows; beyond that it scrolls.

The Cloud settings dialog uses them, so that it fits on a laptop's screen
(its boxes and a cloud's setup check results are taller than many are) and
its buttons stay visible."""

from __future__ import annotations

from AnyQt.QtCore import QSize
from AnyQt.QtWidgets import QScrollArea, QWidget

#: The window frame's height while the window has none yet (before it shows).
FRAME_GUESS = 40
#: The scroll area's lowest height: a few lines, the rest scrolls.
MIN_HEIGHT = 120


class FitScroll(QScrollArea):
    """A scroll area as tall as its content would like (Qt's default is about
    24 lines), so it scrolls only when the window can't be that tall, and as
    wide as the content needs, so only the height scrolls."""

    def sizeHint(self) -> QSize:
        """The content's own size hint, plus the margins."""
        content = self.widget()
        if content is None:
            return super().sizeHint()
        return self._around(content.sizeHint())

    def minimumSizeHint(self) -> QSize:
        """The content's minimum width (and the scroll bar's), a few lines high."""
        content = self.widget()
        if content is None:
            return super().minimumSizeHint()
        bar = self.verticalScrollBar().sizeHint().width()
        return QSize(self._around(content.minimumSizeHint()).width() + bar, MIN_HEIGHT)

    def needed_height(self) -> int:
        """How tall the viewport must be to show the content whole."""
        content = self.widget()
        if content is None:
            return self.viewport().height()
        width = self.viewport().width()
        needed = content.heightForWidth(width) if content.hasHeightForWidth() else -1
        return max(needed, content.sizeHint().height())

    def _around(self, size: QSize) -> QSize:
        margins = self.contentsMargins()
        return QSize(
            size.width() + margins.left() + margins.right(),
            size.height() + margins.top() + margins.bottom(),
        )


def fit_to_screen(window: QWidget, scroll: FitScroll) -> None:
    """Make ``window`` tall enough to show ``scroll``'s content whole, but no
    taller than its screen's free area, and move it up if its bottom would
    be off the screen. It never shrinks a window that fits."""
    screen = window.screen()
    if screen is None:
        return
    area = screen.availableGeometry()
    frame = window.frameGeometry().height() - window.height()
    frame = frame if frame > 0 else FRAME_GUESS
    room = area.height() - frame
    wanted = window.height() + scroll.needed_height() - scroll.viewport().height()
    height = max(min(max(window.height(), wanted), room), window.minimumHeight())
    if height != window.height():
        window.resize(window.width(), height)
    top = window.frameGeometry().top()
    y = max(min(top, area.bottom() + 1 - height - frame), area.top())
    if y != top:
        window.move(window.frameGeometry().left(), y)
