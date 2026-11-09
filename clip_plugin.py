# -*- coding: utf-8 -*-
"""
Glowna klasa wtyczki "Wycinanie i konwersja QGIS 4 (rastry i wektory)".
Wersja dla QGIS 4 (Qt6 / PyQt6), niezalezna od wersji dla QGIS 3.
"""

import os

from qgis.PyQt.QtGui import QAction, QIcon  # Qt6: QAction jest w QtGui

PLUGIN_TITLE = (
    "Wycinanie i konwersja QGIS 4 (rastry i wektory) / Clipping and conversion"
    " QGIS 4 (rasters and vectors)"
)


class ClipRastersPlugin:

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.actions = []
        self.menu = (
            "&Wycinanie i konwersja QGIS 4 / Clipping and conversion QGIS 4"
        )
        self.dialog = None

    def initGui(self):
        icon_path = os.path.join(self.plugin_dir, "icon.png")
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()

        action = QAction(icon, PLUGIN_TITLE, self.iface.mainWindow())
        action.setToolTip(
            "Wycinanie i konwersja formatu rastrow (wg arkuszy) i wektorow (wg"
            " poligonow). Uklad wspolrzednych danych bez zmian.\n\nClipping"
            " and format conversion of rasters (by map sheets) and vectors (by"
            " polygons). Data CRS unchanged."
        )
        action.triggered.connect(self.run)

        self.iface.addToolBarIcon(action)
        self.iface.addPluginToRasterMenu(self.menu, action)
        self.iface.addPluginToVectorMenu(self.menu, action)
        self.actions.append(action)

    def unload(self):
        for action in self.actions:
            self.iface.removePluginRasterMenu(self.menu, action)
            self.iface.removePluginVectorMenu(self.menu, action)
            self.iface.removeToolBarIcon(action)
            action.deleteLater()
        self.actions = []
        if self.dialog is not None:
            self.dialog.close()
            self.dialog.deleteLater()
            self.dialog = None

    def run(self):
        from .clip_dialog import ClipRastersDialog

        if self.dialog is None:
            self.dialog = ClipRastersDialog(self.iface.mainWindow())
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
