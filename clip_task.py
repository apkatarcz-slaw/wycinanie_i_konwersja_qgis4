# -*- coding: utf-8 -*-
"""
Zadanie QGIS (QgsTask) uruchamiajace silnik wycinania w tle, z paskiem
postepu i mozliwoscia anulowania.
"""

from qgis.core import QgsTask

from .clip_core import ClipRunner


class ClipRastersTask(QgsTask):

    def __init__(self, settings, on_finished_callback):
        super().__init__(
            "Rastry - wycinanie i konwersja / Rasters - clipping and"
            " conversion",
            QgsTask.Flag.CanCancel,
        )
        self.settings = settings
        self.on_finished_callback = on_finished_callback
        self.result = None
        self.exception = None

    def run(self):
        runner = ClipRunner(
            self.settings,
            progress=self.setProgress,
            is_canceled=self.isCanceled,
        )
        self.result = runner.result
        try:
            return runner.run()
        except Exception as exc:  # noqa: BLE001
            self.exception = exc
            return False

    def finished(self, success):
        self.on_finished_callback(success, self.result, self.exception)


class VectorTask(QgsTask):
    """Konwersja / wycinanie wektorow w tle."""

    def __init__(self, settings, on_finished_callback):
        super().__init__(
            "Wektory - konwersja i wycinanie / Vectors - conversion and"
            " clipping",
            QgsTask.Flag.CanCancel,
        )
        self.settings = settings
        self.on_finished_callback = on_finished_callback
        self.result = None
        self.exception = None

    def run(self):
        from .vector_core import VectorRunner

        runner = VectorRunner(
            self.settings,
            progress=self.setProgress,
            is_canceled=self.isCanceled,
        )
        self.result = runner.result
        try:
            return runner.run()
        except Exception as exc:  # noqa: BLE001
            self.exception = exc
            return False

    def finished(self, success):
        self.on_finished_callback(success, self.result, self.exception)
