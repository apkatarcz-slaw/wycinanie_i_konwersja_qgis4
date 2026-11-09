# -*- coding: utf-8 -*-
"""
Wycinanie i konwersja (rastry i wektory) - Clipping and conversion (rasters and
vectors)
Wtyczka QGIS: wycinanie rastrow wg arkuszy, wycinanie wektorow wg poligonow,
konwersja formatow - bez zmiany ukladu wspolrzednych danych.
"""


def classFactory(iface):
    from .clip_plugin import ClipRastersPlugin

    return ClipRastersPlugin(iface)
