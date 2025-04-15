from aqt import mw
from aqt.qt import QAction
from .ui import launch_timewarp

action = QAction("Anki Time Warp", mw)
action.triggered.connect(launch_timewarp)
mw.form.menuTools.addAction(action)
