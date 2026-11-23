"""Cloud widgets: the "Cloud" section of Orange's toolbox."""

import os

NAME = "Cloud"

DESCRIPTION = "Run heavy computation on a machine in your own cloud account."

BACKGROUND = "#D6E9FB"

ICON = "icons/category.svg"

PRIORITY = 20

#: The widgets' help pages, for Orange's help (the ``orange.canvas.help``
#: entry point, ``html-index``): an index page whose "widgets" list links each
#: widget's name to its page.
WIDGET_HELP_PATH = ((os.path.join(os.path.dirname(__file__), "help", "index.html"), None),)
