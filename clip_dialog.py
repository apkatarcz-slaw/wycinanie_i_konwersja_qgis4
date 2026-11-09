# -*- coding: utf-8 -*-
"""
Okno dialogowe wtyczki "Wycinanie i konwersja QGIS 4 (rastry i wektory)" (Qt6).
Zakladka "Rastry": wycinanie wg arkuszy / konwersja formatu.
Zakladka "Wektory": konwersja formatu i wycinanie poligonami - bez przeliczen.

(Dawniej: "Wycinanie rastrow wg arkuszy" - wsadowe wycinanie
rastrow z georeferencja po poligonach siatki arkuszy (jeden plik .shp/.gpkg
= jeden arkusz, o tej samej nazwie co raster). Uklad wspolrzednych rastra
pozostaje bez zmian.

Interfejs dwujezyczny: etykiety i komunikaty "polski / English",
tooltipy - tekst polski, pod nim angielski.
"""

import os
import html

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QPixmap
from qgis.PyQt.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QGroupBox,
    QLabel,
    QComboBox,
    QLineEdit,
    QPushButton,
    QFileDialog,
    QProgressBar,
    QTextEdit,
    QCheckBox,
    QMessageBox,
    QWidget,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
)
from qgis.core import QgsApplication, Qgis

try:
    from qgis.utils import iface
except Exception:  # pragma: no cover
    iface = None

from .clip_core import (
    ClipSettings,
    RESAMPLING_METHODS,
    OUTSIDE_NODATA,
    OUTSIDE_ALPHA,
    pair_files,
    bi,
    available_output_formats,
    FORMAT_ORIGINAL,
)
from .clip_task import ClipRastersTask, VectorTask
from .vector_core import (
    VectorSettings,
    available_vector_formats,
    MODE_CLIP,
    MODE_SELECT,
)


def tt(pl, en):
    """Tooltip: tekst polski, pod nim angielski."""
    return "{}\n\n{}".format(pl, en)


def lb(pl, en):
    """Etykieta / checkbox: polski w pierwszej linii, angielski w drugiej."""
    return "{}\n{}".format(pl, en)


BROWSE = bi("Przegladaj...", "Browse...")
ERR = bi("Blad", "Error")


class ClipRastersDialog(QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(
            bi(
                "Wycinanie i konwersja QGIS 4 (rastry i wektory)",
                "Clipping and conversion QGIS 4 (rasters and vectors)",
            )
        )
        minimize_flag = Qt.WindowType.WindowMinimizeButtonHint
        maximize_flag = Qt.WindowType.WindowMaximizeButtonHint
        self.setWindowFlags(self.windowFlags() | minimize_flag | maximize_flag)
        self.setMinimumWidth(480)
        self.resize(680, 760)
        self._task = None
        self._build_ui()

    # ------------------------------------------------------------------
    # Budowa okna
    # ------------------------------------------------------------------

    def _build_ui(self):
        main_layout = QVBoxLayout()

        top_row = QHBoxLayout()
        flag_label = QLabel()
        flag_path = os.path.join(os.path.dirname(__file__), "flag_pl.png")
        if os.path.exists(flag_path):
            flag_label.setPixmap(
                QPixmap(flag_path).scaledToHeight(
                    24, Qt.TransformationMode.SmoothTransformation
                )
            )
        banner_label = QLabel(
            "Wycinanie i konwersja rastrow i wektorow - uklad wspolrzednych"
            " danych bez zmian.\nClipping and conversion of rasters and"
            " vectors - data CRS unchanged."
        )
        banner_label.setStyleSheet("color: red; font-weight: bold;")
        banner_label.setWordWrap(True)
        top_row.addWidget(flag_label)
        top_row.addWidget(banner_label, 1)
        main_layout.addLayout(top_row)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        content_widget = QWidget()
        inner = QVBoxLayout(content_widget)

        # ==============================================================
        # FOLDERY (na gorze) / FOLDERS (at the top)
        # ==============================================================
        folders_box = QGroupBox(bi("Foldery", "Folders"))
        folders_layout = QVBoxLayout()

        self.input_dir_edit = self._folder_row(
            folders_layout,
            lb(
                "1. Folder z rastrami (pliki graficzne)",
                "1. Raster folder (image files)",
            ),
            bi("Wybierz folder z rastrami...", "Select the raster folder..."),
            tt(
                "Folder z rastrami do wyciecia. Wtyczka wykryje w nim"
                " wszystkie\nobslugiwane pliki rastrowe (GeoTIFF, IMG, JP2,"
                " JPG, PNG, PDF ...).\n\nW tym folderze tworzony jest"
                " tymczasowy podfolder roboczy\n'_wycinanie_tmp_...', usuwany"
                " automatycznie po zakonczeniu.",
                "Folder with the rasters to clip. The plugin detects"
                " all\nsupported raster files in it (GeoTIFF, IMG, JP2, JPG,"
                " PNG, PDF ...).\n\nA temporary work subfolder"
                " '_wycinanie_tmp_...' is created in\nthis folder and removed"
                " automatically when finished.",
            ),
        )
        self.sheets_dir_edit = self._folder_row(
            folders_layout,
            lb(
                "2. Folder z arkuszami (pliki wektorowe .shp / .gpkg)",
                "2. Sheet folder (vector files .shp / .gpkg)",
            ),
            bi("Wybierz folder z arkuszami...", "Select the sheet folder..."),
            tt(
                "Kazdy arkusz to osobny plik wektorowy o tej samej nazwie co\n"
                "raster, np. N3450Cd3.shp (lub .gpkg) dla N3450Cd3.tif.\n"
                "Wielkosc liter w nazwach nie ma znaczenia.",
                "Each sheet is a separate vector file named like the raster,\n"
                "e.g. N3450Cd3.shp (or .gpkg) for N3450Cd3.tif.\n"
                "Letter case in names does not matter.",
            ),
        )
        self.output_dir_edit = self._folder_row(
            folders_layout,
            lb("3. Folder wyjsciowy", "3. Output folder"),
            bi("Wybierz folder docelowy...", "Select the output folder..."),
            tt(
                "Folder na wyciete rastry i raport CSV. Musi byc inny niz"
                " folder\nz rastrami, aby nie nadpisac plikow zrodlowych.",
                "Folder for the clipped rasters and the CSV report. It must"
                " differ\nfrom the raster folder so source files are not"
                " overwritten.",
            ),
        )

        self.convert_only_checkbox = QCheckBox(
            lb(
                "Tylko konwersja formatu (bez wycinania po arkuszach)",
                "Format conversion only (no clipping by sheets)",
            )
        )
        self.convert_only_checkbox.setToolTip(
            tt(
                "Zaznaczone: wszystkie rastry z folderu 1 sa tylko"
                " konwertowane do\nwybranego formatu wyjsciowego (z"
                " pozostalymi opcjami: 8-bit, DPI,\nkompresja...), bez"
                " wycinania. Folder z arkuszami nie jest wtedy\npotrzebny."
                " Raster nieobrocony jest kopiowany 1:1 (te same piksele\ni"
                " georeferencja).",
                "Checked: all rasters from folder 1 are only converted to"
                " the\nselected output format (with the other options: 8-bit,"
                " DPI,\ncompression...), without clipping. The sheet folder is"
                " not needed\nthen. A non-rotated raster is copied 1:1 (same"
                " pixels and\ngeoreferencing).",
            )
        )
        self.convert_only_checkbox.toggled.connect(
            self._on_convert_only_toggled
        )
        folders_layout.addWidget(self.convert_only_checkbox)

        self.pure_checkbox = QCheckBox(
            lb(
                "Bez przeliczen (nie wykonuje transformacji)",
                "No recalculation (no transformation)",
            )
        )
        self.pure_checkbox.setToolTip(
            tt(
                "Czysta konwersja (gdal_translate): piksele, georeferencja"
                " (takze\nobrot) i uklad wspolrzednych pozostaja DOKLADNIE"
                " takie jak w pliku\nzrodlowym - zmienia sie tylko format."
                " Wylaczone: resampling, zmiana\nDPI, wycinanie.\n'Wymus"
                " 8-bit' dziala tylko dla plikow 16-bit / float - pliki"
                " 8-bit\nsa kopiowane bez zmian.\nUwaga: JPEG, WebP i JPEG"
                " 2000 sa stratne - piksele zmienia sie\nnieznacznie mimo"
                " braku przeliczen.",
                "Pure conversion (gdal_translate): pixels, georeferencing"
                " (including\nrotation) and the CRS stay EXACTLY as in the"
                " source file - only the\nformat changes. Disabled:"
                " resampling, DPI change, clipping.\n'Force 8-bit' applies"
                " only to 16-bit / float files - 8-bit files\nare copied"
                " unchanged.\nNote: JPEG, WebP and JPEG 2000 are lossy -"
                " pixels change slightly\neven without any recalculation.",
            )
        )
        self.pure_checkbox.setEnabled(False)
        self.pure_checkbox.toggled.connect(self._on_pure_toggled)
        pure_row = QHBoxLayout()
        pure_row.addSpacing(24)
        pure_row.addWidget(self.pure_checkbox)
        pure_row.addStretch()
        folders_layout.addLayout(pure_row)

        self.check_pairs_button = QPushButton(
            bi("Sprawdz parowanie plikow", "Check file pairing")
        )
        self.check_pairs_button.setToolTip(
            tt(
                "Szybki podglad (bez przetwarzania): ile rastrow ma"
                " pasujacy\narkusz, ktore rastry nie maja arkusza i ktore"
                " arkusze nie maja\nrastra.",
                "Quick preview (no processing): how many rasters have a"
                " matching\nsheet, which rasters have no sheet and which"
                " sheets have no raster.",
            )
        )
        self.check_pairs_button.clicked.connect(self._check_pairs)
        folders_layout.addWidget(self.check_pairs_button)

        folders_box.setLayout(folders_layout)
        inner.addWidget(folders_box)

        # ==============================================================
        # FORMAT WYJSCIOWY / OUTPUT FORMAT
        # ==============================================================
        format_box = QGroupBox(bi("Format wyjsciowy", "Output format"))
        format_layout = QVBoxLayout()
        self.format_combo = QComboBox()
        for key, label in available_output_formats():
            self.format_combo.addItem(label, key)
        self.format_combo.setCurrentIndex(
            max(0, self.format_combo.findData(FORMAT_ORIGINAL))
        )
        self.format_combo.setToolTip(
            tt(
                "Format zapisu wynikow. 'Jak plik zrodlowy' - kazdy wynik w"
                " formacie\nswojego rastra (PDF -> GeoTIFF; formaty tylko do"
                " odczytu jak ECW,\nMrSID -> GeoTIFF). Lista zawiera formaty"
                " dostepne w tej instalacji\nQGIS/GDAL.\n\nPlik"
                " georeferencyjny dopisywany jest automatycznie, odpowiedni"
                " do\nformatu: .tif -> .tfw, .jpg -> .jgw, .png -> .pgw, .jp2"
                " -> .j2w,\n.bmp -> .bpw, .webp -> .wld (.img ma georeferencje"
                " w pliku).\nUklad wspolrzednych zapisywany jest w pliku lub w"
                " .aux.xml.\n\nPrzezroczystosc poza arkuszem: GeoTIFF, COG,"
                " JPEG - maska;\nPNG, WebP, JPEG 2000 - kanal alfa; BMP, IMG -"
                " tylko tlo.\nJPEG/WebP/JPEG 2000 sa stratne (jakosc 90). PNG,"
                " JPEG i BMP\nprzyjmuja tylko dane 8-bit (PNG takze 16-bit) -"
                " dla NMT wybierz\nGeoTIFF albo zaznacz 'Wymus 8-bit'.",
                "Output file format. 'Same as source' - each output in the"
                " format\nof its raster (PDF -> GeoTIFF; read-only formats"
                " such as ECW,\nMrSID -> GeoTIFF). The list shows formats"
                " available in this\nQGIS/GDAL installation.\n\nThe matching"
                " world file is written automatically: .tif -> .tfw,\n.jpg ->"
                " .jgw, .png -> .pgw, .jp2 -> .j2w, .bmp -> .bpw,\n.webp ->"
                " .wld (.img stores georeferencing internally). The CRS\nis"
                " stored in the file or in .aux.xml.\n\nTransparency outside"
                " the sheet: GeoTIFF, COG, JPEG - mask;\nPNG, WebP, JPEG 2000"
                " - alpha band; BMP, IMG - background only.\nJPEG/WebP/JPEG"
                " 2000 are lossy (quality 90). PNG, JPEG and BMP\naccept 8-bit"
                " data only (PNG also 16-bit) - for DEMs choose\nGeoTIFF or"
                " tick 'Force 8-bit'.",
            )
        )
        format_layout.addWidget(self.format_combo)
        wf_info = QLabel(
            "Plik georeferencyjny (.tfw, .jgw, .pgw, .j2w, .bpw, .wld)"
            " dopisywany automatycznie.\nWorld file (.tfw, .jgw, .pgw, .j2w,"
            " .bpw, .wld) written automatically."
        )
        wf_info.setWordWrap(True)
        wf_info.setStyleSheet("color: gray; font-size: 11px;")
        format_layout.addWidget(wf_info)

        # ==============================================================
        # WYCINANIE / CLIPPING
        # ==============================================================
        sheet_box = QGroupBox(
            bi("Wycinanie wg siatki arkuszy", "Clipping by map sheets")
        )
        self.sheet_box = sheet_box
        sheet_layout = QVBoxLayout()

        sheet_layout.addWidget(
            QLabel(lb("Obszar poza arkuszem:", "Area outside the sheet:"))
        )
        self.outside_combo = QComboBox()
        self.outside_combo.addItem(
            bi(
                "Tlo biale/czarne + maska (domyslnie)",
                "White/black background + mask (default)",
            ),
            OUTSIDE_NODATA,
        )
        self.outside_combo.addItem(
            bi("Kanal alfa (przezroczystosc)", "Alpha band (transparency)"),
            OUTSIDE_ALPHA,
        )
        self.outside_combo.setToolTip(
            tt(
                "Tlo + maska (domyslnie): poza arkuszem piksele sa biale"
                " (albo\nczarne - opcja ponizej), a obszar poza arkuszem"
                " oznaczony jest\nMASKA wewnetrzna GeoTIFF. Tresc mapy nie"
                " traci zadnych pikseli -\nbiale tlo papieru czy lasy (zielony"
                " kanal = 255) pozostaja\nwidoczne w QGIS/ArcGIS;"
                " przezroczysty jest tylko obszar poza\narkuszem. Program"
                " graficzny pokaze biale tlo.\nJesli plik zrodlowy ma wlasna"
                " wartosc NoData (np. NMT) albo\nustawiono ja recznie -"
                " uzywana jest ta wartosc.\n\nKanal alfa: do wyniku dodawany"
                " jest kanal przezroczystosci -\nwewnatrz arkusza zadna"
                " wartosc nie jest tracona. Nie kazdy\nprogram graficzny go"
                " obsluguje. Dla formatow bez kanalu alfa\n(np. JPEG)"
                " przezroczystosc zapisywana jest jako maska.",
                "Background + mask (default): pixels outside the sheet are"
                " white\n(or black - option below) and the area outside the"
                " sheet is\nmarked with an internal GeoTIFF MASK. No map pixel"
                " is lost -\nwhite paper or forests (green band = 255) stay"
                " visible in\nQGIS/ArcGIS; only the area outside the sheet is"
                " transparent.\nGraphics programs show a white background.\nIf"
                " the source file has its own NoData value (e.g. a DEM) or"
                " one\nis set manually - that value is used.\n\nAlpha band: a"
                " transparency band is added to the output - no\nvalue inside"
                " the sheet is lost. Not every graphics program\nsupports it."
                " For formats without alpha (e.g. JPEG) the\ntransparency is"
                " stored as a mask.",
            )
        )
        sheet_layout.addWidget(self.outside_combo)

        self.reproject_checkbox = QCheckBox(
            lb("Przeliczaj z innego ukladu", "Reproject from another CRS")
        )
        self.reproject_checkbox.setToolTip(
            tt(
                "Odznaczone (domyslnie): zakladamy, ze arkusze i rastry sa w"
                " tym\nsamym ukladzie wspolrzednych. Jesli uklady sie roznia,"
                " raster\njest POMIJANY (status 'niezgodny uklad' w raporcie"
                " CSV).\n\nZaznaczone: granica arkusza jest przeliczana do"
                " ukladu rastra\n(z zageszczeniem wierzcholkow, zeby krawedzie"
                " arkusza zostaly\npoprawnie odwzorowane). Uklad rastra NIGDY"
                " nie jest zmieniany.",
                "Unchecked (default): sheets and rasters are assumed to share"
                " the\nsame coordinate system. If they differ, the raster is"
                " SKIPPED\n(status 'CRS mismatch' in the CSV"
                " report).\n\nChecked: the sheet boundary is transformed to"
                " the raster CRS\n(with densified vertices, so sheet edges are"
                " reproduced\ncorrectly). The raster CRS is NEVER changed.",
            )
        )
        sheet_layout.addWidget(self.reproject_checkbox)

        rules_label = QLabel(
            "Wynik przyciety do zasiegu poligonu (po ramce, bez przesuniecia"
            " pikseli). Kilka poligonow w pliku: wybierany ten z nazwa arkusza"
            " w polu tekstowym. GPKG z kilkoma warstwami: warstwa o nazwie"
            " arkusza, a gdy jej brak - pierwsza.\nOutput cropped to the"
            " polygon extent (along the frame, no pixel shift). Several"
            " polygons in a file: the one with the sheet name in a text field"
            " is used. GPKG with several layers: the layer named like the"
            " sheet, otherwise the first one."
        )
        rules_label.setWordWrap(True)
        rules_label.setStyleSheet("color: gray; font-size: 11px;")
        sheet_layout.addWidget(rules_label)

        sheet_box.setLayout(sheet_layout)
        inner.addWidget(sheet_box)

        # -- sufiks --
        self.suffix_checkbox = QCheckBox(
            lb(
                "Dodaj sufiks do nazwy pliku wyjsciowego",
                "Add a suffix to the output file name",
            )
        )
        self.suffix_checkbox.setToolTip(
            tt(
                "Odznaczone (domyslnie): plik wyjsciowy ma taka sama nazwe"
                " jak\nraster wejsciowy.\n\nZaznaczone: tekst dopisywany na"
                " koncu nazwy (przed\nrozszerzeniem), np. '_wyciety' ->"
                " N3450Cd3_wyciety.tif.",
                "Unchecked (default): the output file has the same name as"
                " the\ninput raster.\n\nChecked: the text is appended to the"
                " name (before the\nextension), e.g. '_clip' ->"
                " N3450Cd3_clip.tif.",
            )
        )
        self.suffix_checkbox.toggled.connect(
            lambda c: self.suffix_edit.setEnabled(c)
        )
        inner.addWidget(self.suffix_checkbox)
        self.suffix_edit = QLineEdit()
        self.suffix_edit.setPlaceholderText(bi("np. _wyciety", "e.g. _clip"))
        self.suffix_edit.setToolTip(tt("Tekst sufiksu.", "Suffix text."))
        self.suffix_edit.setEnabled(False)
        inner.addWidget(self.suffix_edit)

        # ==============================================================
        # OPCJE RASTROWE / RASTER OPTIONS
        # ==============================================================
        raster_box = QGroupBox(bi("Opcje rastrowe", "Raster options"))
        raster_layout = QVBoxLayout()

        crs_label = QLabel(
            lb("Uklad dla plikow bez ukladu:", "CRS for files without a CRS:")
        )
        raster_layout.addWidget(crs_label)
        self.default_crs_combo = QComboBox()
        self.default_crs_combo.setEditable(True)
        for code, label in (
            (
                "",
                bi(
                    "(brak - wytnij bez ukladu)",
                    "(none - process without CRS)",
                ),
            ),
            ("EPSG:2180", "EPSG:2180 - PL-1992"),
            ("EPSG:2176", "EPSG:2176 - PL-2000 strefa / zone 5"),
            ("EPSG:2177", "EPSG:2177 - PL-2000 strefa / zone 6"),
            ("EPSG:2178", "EPSG:2178 - PL-2000 strefa / zone 7"),
            ("EPSG:2179", "EPSG:2179 - PL-2000 strefa / zone 8"),
            ("EPSG:3120", "EPSG:3120 - PL-1965 strefa / zone 1"),
            ("EPSG:2172", "EPSG:2172 - PL-1965 strefa / zone 2"),
            ("EPSG:2173", "EPSG:2173 - PL-1965 strefa / zone 3"),
            ("EPSG:2174", "EPSG:2174 - PL-1965 strefa / zone 4"),
            ("EPSG:2175", "EPSG:2175 - PL-1965 strefa / zone 5"),
            ("EPSG:32633", "EPSG:32633 - UTM 33N"),
            ("EPSG:32634", "EPSG:32634 - UTM 34N"),
            ("EPSG:4326", "EPSG:4326 - WGS 84"),
        ):
            self.default_crs_combo.addItem(label, code)
        self.default_crs_combo.setCurrentIndex(
            self.default_crs_combo.findData("EPSG:2180")
        )
        self.default_crs_combo.setToolTip(
            tt(
                "Dotyczy rastrow BEZ zapisanego ukladu (np. JPG z samym"
                " plikiem\n.jgw, bez .aux.xml / .prj) oraz arkuszy bez pliku"
                " .prj:\n1) raster bez ukladu dostaje uklad arkusza (i"
                " odwrotnie),\n2) gdy oba sa bez ukladu - uklad wybrany"
                " tutaj,\n3) przy '(brak)' - wycinanie w samych wspolrzednych,"
                " wynik bez\n   ukladu (tylko plik georeferencyjny).\nPrzyjety"
                " uklad jest zapisywany w wyniku i opisany w raporcie"
                " CSV.\nMozna tez wpisac wlasny kod, np. EPSG:2180.\n\nUWAGA:"
                " jesli raster i arkusz bez ukladu sa w roznych"
                " ukladach,\nwtyczka tego nie wykryje.",
                "Applies to rasters WITHOUT a stored CRS (e.g. JPG with only a"
                " .jgw\nfile, no .aux.xml / .prj) and to sheets without a .prj"
                " file:\n1) a raster without CRS takes the sheet CRS (and vice"
                " versa),\n2) when both have no CRS - the CRS selected"
                " here,\n3) with '(none)' - processed in plain coordinates,"
                " output without\n   CRS (world file only).\nThe assumed CRS"
                " is written to the output and noted in the CSV\nreport. A"
                " custom code can be typed, e.g. EPSG:2180.\n\nNOTE: if a"
                " raster and a sheet without CRS are in different CRSs,\nthe"
                " plugin cannot detect it.",
            )
        )
        crs_label.setToolTip(self.default_crs_combo.toolTip())
        raster_layout.addWidget(self.default_crs_combo)

        raster_layout.addWidget(
            QLabel(lb("Metoda resamplingu:", "Resampling method:"))
        )
        self.resampling_combo = QComboBox()
        self.resampling_combo.addItem(
            bi(
                "Automatycznie (osobno dla kazdego pliku)",
                "Automatic (per file)",
            ),
            "auto",
        )
        for code, label in RESAMPLING_METHODS:
            self.resampling_combo.addItem(label, code)
        near_index = self.resampling_combo.findData("near")
        if near_index >= 0:
            self.resampling_combo.setCurrentIndex(near_index)
        self.resampling_combo.setToolTip(
            tt(
                "Domyslnie: najblizszy sasiad - przy niezmienionej"
                " rozdzielczosci\npiksele sa kopiowane 1:1 (zasieg wycinka"
                " jest dopasowany do\nsiatki pikseli rastra"
                " zrodlowego).\n\nInne metody maja znaczenie glownie przy"
                " zmianie rozdzielczosci\n(DPI) albo przy rastrach obroconych."
                " 'Automatycznie' dobiera\nmetode wg typu danych (kategorie /"
                " NMT / RGB).",
                "Default: nearest neighbour - with unchanged resolution"
                " pixels\nare copied 1:1 (the clip extent is aligned to the"
                " source pixel\ngrid).\n\nOther methods matter mainly when"
                " changing resolution (DPI) or\nfor rotated rasters."
                " 'Automatic' picks a method by data type\n(categories / DEM /"
                " RGB).",
            )
        )
        raster_layout.addWidget(self.resampling_combo)

        self.compression_combo = QComboBox()
        self.compression_combo.addItem(bi("Brak", "None"), None)
        for c in ["LZW", "DEFLATE", "PACKBITS", "JPEG", "ZSTD", "LZMA"]:
            self.compression_combo.addItem(c, c)
        self.compression_combo.setToolTip(
            tt(
                "Bezstratne: LZW, DEFLATE, PACKBITS, ZSTD, LZMA.\n"
                "JPEG jest STRATNY - tylko dla ortofotomap/zdjec RGB.\n\n"
                "Stosowana, gdy wynik jest zapisywany jako GeoTIFF albo COG.",
                "Lossless: LZW, DEFLATE, PACKBITS, ZSTD, LZMA.\n"
                "JPEG is LOSSY - only for orthophotos/RGB images.\n\n"
                "Applied when the output is written as GeoTIFF or COG.",
            )
        )
        format_layout.addWidget(
            QLabel(
                lb("Kompresja GeoTIFF / COG:", "GeoTIFF / COG compression:")
            )
        )
        format_layout.addWidget(self.compression_combo)
        format_box.setLayout(format_layout)
        inner.insertWidget(inner.indexOf(self.sheet_box), format_box)

        self.default_nodata_black_checkbox = QCheckBox(
            lb(
                "Tlo poza arkuszem: czarne (zamiast bialego)",
                "Background outside the sheet: black (instead of white)",
            )
        )
        self.default_nodata_black_checkbox.setToolTip(
            tt(
                "Dla plikow bez wlasnej wartosci NoData: obszar poza"
                " arkuszem\nwypelniany jest biela (255, domyslnie) albo"
                " czernia (0) i\noznaczany maska. Recznie ustawiona wartosc"
                " NoData ma\npierwszenstwo.",
                "For files without their own NoData value: the area outside"
                " the\nsheet is filled with white (255, default) or black (0)"
                " and\nmarked with a mask. A manually set NoData value"
                " takes\nprecedence.",
            )
        )
        raster_layout.addWidget(self.default_nodata_black_checkbox)

        self.manual_nodata_checkbox = QCheckBox(
            lb("Recznie ustaw wartosc NoData", "Set NoData value manually")
        )
        self.manual_nodata_checkbox.setToolTip(
            tt(
                "Odznaczone (domyslnie): uzywana jest wartosc NoData z pliku\n"
                "zrodlowego, a gdy jej brak - tlo biale/czarne + maska.\n\n"
                "Zaznaczone: podana wartosc dla wszystkich plikow.",
                "Unchecked (default): the NoData value of the source file is\n"
                "used; if missing - white/black background + mask.\n\n"
                "Checked: the given value is used for all files.",
            )
        )
        self.manual_nodata_checkbox.toggled.connect(
            lambda c: self.nodata_edit.setEnabled(c)
        )
        raster_layout.addWidget(self.manual_nodata_checkbox)
        raster_layout.addWidget(QLabel(lb("Wartosc NoData:", "NoData value:")))
        self.nodata_edit = QLineEdit()
        self.nodata_edit.setPlaceholderText(bi("np. -9999", "e.g. -9999"))
        self.nodata_edit.setToolTip(
            tt("Liczba, np. -9999 lub 255.", "A number, e.g. -9999 or 255.")
        )
        self.nodata_edit.setEnabled(False)
        raster_layout.addWidget(self.nodata_edit)

        self.dpi_resize_checkbox = QCheckBox(
            lb(
                "Zmien rozdzielczosc wg DPI wejscia/wyjscia",
                "Change resolution by input/output DPI",
            )
        )
        self.dpi_resize_checkbox.setToolTip(
            tt(
                "Zaznaczone: rozmiar piksela przeskalowany proporcjonalnie"
                " do\nstosunku DPI wejsciowe / DPI wyjsciowe (np. 400 -> 200 ="
                " piksel\n2x wiekszy).",
                "Checked: the pixel size is scaled by the ratio input DPI /\n"
                "output DPI (e.g. 400 -> 200 = pixel twice as large).",
            )
        )
        self.dpi_resize_checkbox.toggled.connect(self._on_dpi_resize_toggled)
        raster_layout.addWidget(self.dpi_resize_checkbox)
        raster_layout.addWidget(QLabel(lb("DPI wejsciowe:", "Input DPI:")))
        self.input_dpi_edit = QLineEdit()
        self.input_dpi_edit.setPlaceholderText(bi("np. 400", "e.g. 400"))
        self.input_dpi_edit.setToolTip(
            tt(
                "DPI skanowania rastra zrodlowego.",
                "Scan DPI of the source raster.",
            )
        )
        self.input_dpi_edit.setEnabled(False)
        raster_layout.addWidget(self.input_dpi_edit)
        raster_layout.addWidget(QLabel(lb("DPI wyjsciowe:", "Output DPI:")))
        self.output_dpi_edit = QLineEdit()
        self.output_dpi_edit.setPlaceholderText(bi("np. 300", "e.g. 300"))
        self.output_dpi_edit.setToolTip(
            tt("Docelowe DPI wyniku.", "Target DPI of the output.")
        )
        self.output_dpi_edit.setEnabled(False)
        raster_layout.addWidget(self.output_dpi_edit)

        self.force_8bit_checkbox = QCheckBox(
            lb("Wymus 8-bit (wartosci 0-255)", "Force 8-bit (values 0-255)")
        )
        self.force_8bit_checkbox.setToolTip(
            tt(
                "Liczony jest jeden wspolny zakres wartosci OSOBNO DLA"
                " KAZDEGO\nKANALU - tylko z pikseli WEWNATRZ arkuszy (ramki i"
                " opisy\npozaramkowe nie zaburzaja rozciagania). Nastepnie"
                " wycinki sa\nskalowane do 0-255 i zapisywane jako"
                " GeoTIFF.\n\nPliki, ktore juz sa 8-bit (np. skany map RGB),"
                " NIE sa\nrozciagane - kolory zostaja bez zmian. Rozciagane sa"
                " tylko\npliki 16-bit / float (np. NMT).\n\nDomyslnie"
                " wszystkie rastry sa najpierw wycinane do folderu\nroboczego"
                " (szybciej, ale folder zajmuje tyle miejsca, ile\nwszystkie"
                " wycinki razem) - patrz opcja ponizej.",
                "One common value range is computed SEPARATELY FOR EACH BAND"
                " -\nonly from pixels INSIDE the sheets (frames and marginal"
                " text do\nnot distort the stretch). The clips are then scaled"
                " to 0-255\nand saved as GeoTIFF.\n\nFiles that are already"
                " 8-bit (e.g. RGB map scans) are NOT\nstretched - colours stay"
                " unchanged. Only 16-bit / float files\n(e.g. DEMs) are"
                " stretched.\n\nBy default all rasters are first clipped into"
                " the work folder\n(faster, but the folder takes as much space"
                " as all clips\ntogether) - see the option below.",
            )
        )
        self.force_8bit_checkbox.toggled.connect(self._on_force_8bit_toggled)
        raster_layout.addWidget(self.force_8bit_checkbox)

        self.low_disk_checkbox = QCheckBox(
            lb(
                "Uzyj, gdy jest malo miejsca na dysku (wolniej)",
                "Use when disk space is low (slower)",
            )
        )
        self.low_disk_checkbox.setToolTip(
            tt(
                "Dotyczy wymuszonego 8-bit.\n\nOdznaczone (domyslnie):"
                " najpierw wszystkie arkusze sa wycinane\ndo folderu"
                " roboczego, potem liczony jest zakres i skalowanie"
                " -\nszybciej, ale w folderze roboczym leza naraz wszystkie"
                " wycinki.\n\nZaznaczone: zakres liczony jest z wycinkow"
                " wirtualnych (bez\nzapisu na dysk), a potem kazdy arkusz po"
                " kolei: wyciecie ->\n8-bit -> zapis -> natychmiastowe"
                " usuniecie roboczego pliku.\nNa dysku jest najwyzej jeden"
                " roboczy plik naraz, ale kazdy\narkusz jest wycinany dwa"
                " razy, wiec trwa to dluzej.",
                "Applies to forced 8-bit.\n\nUnchecked (default): all sheets"
                " are first clipped into the work\nfolder, then the range is"
                " computed and scaled - faster, but all\nclips sit in the work"
                " folder at the same time.\n\nChecked: the range is computed"
                " from virtual clips (nothing\nwritten to disk), then each"
                " sheet in turn: clip -> 8-bit ->\nsave -> work file deleted"
                " immediately. At most one work file\nis on disk at a time,"
                " but each sheet is clipped twice, so it\ntakes longer.",
            )
        )
        self.low_disk_checkbox.setEnabled(False)
        low_disk_row = QHBoxLayout()
        low_disk_row.addSpacing(24)
        low_disk_row.addWidget(self.low_disk_checkbox)
        low_disk_row.addStretch()
        raster_layout.addLayout(low_disk_row)

        raster_layout.addWidget(
            QLabel(
                lb(
                    "DPI rasteryzacji plikow PDF/GeoPDF:",
                    "PDF/GeoPDF rasterization DPI:",
                )
            )
        )
        self.pdf_dpi_edit = QLineEdit("300")
        self.pdf_dpi_edit.setToolTip(
            tt(
                "Pliki .pdf traktowane sa wylacznie jako obraz - tresc"
                " wektorowa\njest splaszczana przy tym DPI, a nastepnie"
                " wycinana jak kazdy\ninny raster. Wymaga sterownika PDF w"
                " GDAL i GeoPDF z georeferencja.",
                ".pdf files are treated as images only - vector content"
                " is\nflattened at this DPI and then clipped like any other"
                " raster.\nRequires the GDAL PDF driver and a georeferenced"
                " GeoPDF.",
            )
        )
        raster_layout.addWidget(self.pdf_dpi_edit)
        pdf_warn = QLabel(
            lb(
                "Uzywac tylko dla plikow PDF typu rastrowego!!! Tresc"
                " wektorowa zostanie splaszczona",
                "Use only for raster-type PDF files!!! Vector content will be"
                " flattened",
            )
        )
        pdf_warn.setWordWrap(True)
        pdf_warn.setStyleSheet("color: red; font-weight: bold;")
        raster_layout.addWidget(pdf_warn)

        raster_box.setLayout(raster_layout)
        inner.addWidget(raster_box)

        self.info_label = QLabel(
            "Wyniki i raport raport_wycinania_RRRR-MM-DD_GGMM.csv (separator"
            " ';') trafiaja do folderu wyjsciowego. Rastry bez arkusza i"
            " arkusze bez rastra sa pomijane i wpisywane do raportu.\nOutputs"
            " and the report raport_wycinania_YYYY-MM-DD_HHMM.csv (separator"
            " ';') go to the output folder. Rasters without a sheet and sheets"
            " without a raster are skipped and listed in the report."
        )
        self.info_label.setWordWrap(True)
        self.info_label.setStyleSheet("color: gray; font-size: 11px;")
        inner.addWidget(self.info_label)

        for combo in (
            self.outside_combo,
            self.resampling_combo,
            self.compression_combo,
            self.format_combo,
            self.default_crs_combo,
        ):
            self._narrow_combo(combo)

        scroll_area.setWidget(content_widget)

        self.tabs = QTabWidget()
        self.tabs.addTab(scroll_area, bi("Rastry", "Rasters"))
        self.tabs.addTab(self._build_vector_tab(), bi("Wektory", "Vectors"))
        self.tabs.currentChanged.connect(lambda _i: self._update_run_text())
        main_layout.addWidget(self.tabs, 1)
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setToolTip(
            tt("Postep przetwarzania.", "Processing progress.")
        )
        main_layout.addWidget(self.progress_bar)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(110)
        self.log_view.setMaximumHeight(260)
        self.log_view.setToolTip(
            tt(
                "Log: przetworzone pliki i warstwy, ostrzezenia i pominiecia.",
                "Log: processed files and layers, warnings and skipped items.",
            )
        )
        main_layout.addWidget(self.log_view)

        btn_row = QHBoxLayout()
        self.run_button = QPushButton(bi("Wytnij rastry", "Clip rasters"))
        self.run_button.setDefault(True)
        self.run_button.setMinimumWidth(250)
        self.run_button.setToolTip(
            tt(
                "Uruchamia przetwarzanie dla aktywnej zakladki (Rastry /"
                " Wektory).",
                "Starts processing for the active tab (Rasters / Vectors).",
            )
        )
        self.run_button.clicked.connect(self._run)
        self.cancel_button = QPushButton(bi("Anuluj", "Cancel"))
        self.cancel_button.setEnabled(False)
        self.cancel_button.setToolTip(
            tt(
                "Przerywa przetwarzanie. Pliki juz zapisane zostaja, folder\n"
                "roboczy jest usuwany, raport CSV jest zapisywany.",
                "Stops processing. Files already written are kept, the work\n"
                "folder is removed and the CSV report is written.",
            )
        )
        self.cancel_button.clicked.connect(self._cancel)
        close_button = QPushButton(bi("Zamknij", "Close"))
        close_button.setToolTip(
            tt("Zamyka okno wtyczki.", "Closes the plugin window.")
        )
        close_button.clicked.connect(self.close)
        btn_row.addWidget(close_button)
        btn_row.addStretch()
        btn_row.addWidget(self.run_button)
        btn_row.addWidget(self.cancel_button)
        main_layout.addLayout(btn_row)

        self.setLayout(main_layout)

    # ------------------------------------------------------------------
    # Pomocnicze UI
    # ------------------------------------------------------------------

    def _folder_row(self, layout, label_text, placeholder, tooltip):
        label = QLabel(label_text)
        label.setToolTip(tooltip)
        layout.addWidget(label)
        row = QHBoxLayout()
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        edit.setToolTip(tooltip)
        edit.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        btn = QPushButton(BROWSE)
        btn.setToolTip(
            tt(
                "Otworz okno wyboru folderu.",
                "Open the folder selection dialog.",
            )
        )
        btn.clicked.connect(
            lambda: self._browse_dir(edit, label_text.replace("\n", " / "))
        )
        row.addWidget(edit, 1)
        row.addWidget(btn)
        layout.addLayout(row)
        edit.row_widgets = (label, btn)
        return edit

    @staticmethod
    def _narrow_combo(combo):
        """Combo nie rozpycha okna dlugimi dwujezycznymi pozycjami."""
        policy = (
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        combo.setSizeAdjustPolicy(policy)
        combo.setMinimumContentsLength(24)

    def _browse_dir(self, edit, title):
        d = QFileDialog.getExistingDirectory(self, title, edit.text().strip())
        if d:
            edit.setText(d)

    def _on_dpi_resize_toggled(self, checked):
        self.input_dpi_edit.setEnabled(checked)
        self.output_dpi_edit.setEnabled(checked)

    def _on_convert_only_toggled(self, checked):
        for w in (self.sheets_dir_edit,) + self.sheets_dir_edit.row_widgets:
            w.setEnabled(not checked)
        self.check_pairs_button.setEnabled(not checked)
        self.sheet_box.setEnabled(not checked)
        self.pure_checkbox.setEnabled(checked)
        if not checked:
            self.pure_checkbox.setChecked(False)
        self._update_run_text()

    def _on_pure_toggled(self, checked):
        # czysta konwersja: bez resamplingu, zmiany DPI i trybu "malo miejsca"
        self.resampling_combo.setEnabled(not checked)
        self.dpi_resize_checkbox.setEnabled(not checked)
        if checked:
            self.dpi_resize_checkbox.setChecked(False)
        self.low_disk_checkbox.setEnabled(
            not checked and self.force_8bit_checkbox.isChecked()
        )

    def _update_run_text(self):
        if not hasattr(self, "run_button"):
            return
        if self.tabs.currentIndex() == 1:
            text = (
                bi("Wytnij wektory", "Clip vectors")
                if self.v_clip_checkbox.isChecked()
                else bi("Konwertuj wektory", "Convert vectors")
            )
        else:
            text = (
                bi("Konwertuj rastry", "Convert rasters")
                if self.convert_only_checkbox.isChecked()
                else bi("Wytnij rastry", "Clip rasters")
            )
        self.run_button.setText(text)

    def _selected_default_crs(self):
        """Kod ukladu z listy (albo wpisany recznie).

        None = brak ukladu, False = bledny wpis.
        """
        text = self.default_crs_combo.currentText().strip()
        idx = self.default_crs_combo.findText(text)
        if idx >= 0:
            code = self.default_crs_combo.itemData(idx)
        else:
            code = text.split()[0] if text else ""
        if not code:
            return None
        from osgeo import osr

        srs = osr.SpatialReference()
        try:
            ok = (
                srs.SetFromUserInput(code) == 0
                and srs.IsProjected() + srs.IsGeographic() > 0
            )
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            QMessageBox.warning(
                self,
                ERR,
                bi(
                    "Nieznany uklad wspolrzednych: {} (wpisz np. EPSG:2180)"
                    .format(code),
                    "Unknown coordinate reference system: {} (type e.g."
                    " EPSG:2180)".format(code),
                ),
            )
            return False
        return code

    def _on_force_8bit_toggled(self, checked):
        self.low_disk_checkbox.setEnabled(
            checked and not self.pure_checkbox.isChecked()
        )

    def _log(self, text, color=None):
        # zawsze jako HTML (inaczej encje typu &#x27; bylyby widoczne
        # doslownie)
        escaped = html.escape(text).replace("  ", "&nbsp; ")
        style = ' style="color:{};"'.format(color) if color else ""
        self.log_view.append(
            '<span{}>{}</span>'.format(style, escaped or "&nbsp;")
        )

    def _validate_dirs(self, need_output):
        input_dir = self.input_dir_edit.text().strip()
        sheets_dir = self.sheets_dir_edit.text().strip()
        output_dir = self.output_dir_edit.text().strip()
        if not input_dir or not os.path.isdir(input_dir):
            QMessageBox.warning(
                self,
                ERR,
                bi(
                    "Wskaz poprawny folder z rastrami.",
                    "Select a valid raster folder.",
                ),
            )
            return None
        if not self.convert_only_checkbox.isChecked() and (
            not sheets_dir or not os.path.isdir(sheets_dir)
        ):
            QMessageBox.warning(
                self,
                ERR,
                bi(
                    "Wskaz poprawny folder z arkuszami (.shp / .gpkg).",
                    "Select a valid sheet folder (.shp / .gpkg).",
                ),
            )
            return None
        if need_output:
            if not output_dir:
                QMessageBox.warning(
                    self,
                    ERR,
                    bi("Wskaz folder wyjsciowy.", "Select the output folder."),
                )
                return None
            if os.path.abspath(input_dir) == os.path.abspath(output_dir):
                QMessageBox.warning(
                    self,
                    ERR,
                    bi(
                        "Folder wyjsciowy musi byc inny niz folder z"
                        " rastrami.",
                        "The output folder must differ from the raster"
                        " folder.",
                    ),
                )
                return None
        return input_dir, sheets_dir, output_dir

    # ------------------------------------------------------------------
    # Akcje
    # ------------------------------------------------------------------

    def _check_pairs(self):
        dirs = self._validate_dirs(need_output=False)
        if dirs is None:
            return
        input_dir, sheets_dir, _ = dirs
        pairs, no_sheet, no_raster = pair_files(input_dir, sheets_dir)
        self.log_view.clear()
        self._log("== PAROWANIE PLIKOW / FILE PAIRING ==")
        self._log(
            bi("Pary raster-arkusz: {}", "Raster-sheet pairs: {}").format(
                len(pairs), len(pairs)
            )
        )
        for raster, sheet, notes in pairs:
            self._log(
                "  {}  <->  {}".format(
                    os.path.basename(raster), os.path.basename(sheet)
                )
            )
            for n in notes:
                self._log("     !  " + n)
        if no_sheet:
            self._log(
                bi(
                    "Rastry bez arkusza (zostana pominiete): {}",
                    "Rasters without a sheet (will be skipped): {}",
                ).format(len(no_sheet), len(no_sheet)),
                color="red",
            )
            for r in no_sheet:
                self._log("  " + os.path.basename(r), color="red")
        if no_raster:
            self._log(
                bi(
                    "Arkusze bez rastra: {}", "Sheets without a raster: {}"
                ).format(len(no_raster), len(no_raster)),
                color="#b06000",
            )
            for s in no_raster:
                self._log("  " + os.path.basename(s), color="#b06000")

    def _run(self):
        if self.tabs.currentIndex() == 1:
            self._run_vector()
        else:
            self._run_raster()

    def _run_raster(self):
        dirs = self._validate_dirs(need_output=True)
        if dirs is None:
            return
        input_dir, sheets_dir, output_dir = dirs

        output_suffix = ""
        if self.suffix_checkbox.isChecked():
            output_suffix = self.suffix_edit.text().strip()
            if not output_suffix:
                QMessageBox.warning(
                    self,
                    ERR,
                    bi(
                        "Zaznaczono dodanie sufiksu, ale nie podano jego"
                        " tresci.",
                        "Adding a suffix is checked but no suffix was given.",
                    ),
                )
                return

        manual_nodata = None
        if self.manual_nodata_checkbox.isChecked():
            try:
                manual_nodata = float(
                    self.nodata_edit.text().strip().replace(",", ".")
                )
            except ValueError:
                QMessageBox.warning(
                    self,
                    ERR,
                    bi(
                        "Wartosc NoData musi byc liczba (np. -9999).",
                        "The NoData value must be a number (e.g. -9999).",
                    ),
                )
                return

        input_dpi = output_dpi = None
        if self.dpi_resize_checkbox.isChecked():
            try:
                input_dpi = float(
                    self.input_dpi_edit.text().strip().replace(",", ".")
                )
                output_dpi = float(
                    self.output_dpi_edit.text().strip().replace(",", ".")
                )
                if input_dpi <= 0 or output_dpi <= 0:
                    raise ValueError
            except ValueError:
                QMessageBox.warning(
                    self,
                    ERR,
                    bi(
                        "DPI wejsciowe i wyjsciowe musza byc liczbami"
                        " dodatnimi (np. 400 i 300).",
                        "Input and output DPI must be positive numbers (e.g."
                        " 400 and 300).",
                    ),
                )
                return

        try:
            pdf_dpi = int(self.pdf_dpi_edit.text().strip())
            if pdf_dpi <= 0:
                raise ValueError
        except ValueError:
            QMessageBox.warning(
                self,
                ERR,
                bi(
                    "DPI rasteryzacji PDF musi byc liczba calkowita dodatnia"
                    " (np. 300).",
                    "PDF rasterization DPI must be a positive integer (e.g."
                    " 300).",
                ),
            )
            return

        default_crs = self._selected_default_crs()
        if default_crs is False:
            return

        force_8bit = self.force_8bit_checkbox.isChecked()
        settings = ClipSettings(
            input_dir=input_dir,
            output_dir=output_dir,
            sheets_dir=sheets_dir,
            resampling_mode=self.resampling_combo.currentData(),
            output_format=self.format_combo.currentData(),
            compression=self.compression_combo.currentData(),
            manual_nodata=manual_nodata,
            input_dpi=input_dpi,
            output_dpi=output_dpi,
            force_8bit=force_8bit,
            default_nodata_black=(
                self.default_nodata_black_checkbox.isChecked()
            ),
            pdf_dpi=pdf_dpi,
            convert_only=self.convert_only_checkbox.isChecked(),
            pure_convert=self.convert_only_checkbox.isChecked()
            and self.pure_checkbox.isChecked(),
            output_suffix=output_suffix,
            outside_mode=self.outside_combo.currentData(),
            reproject_sheets=self.reproject_checkbox.isChecked(),
            low_disk=force_8bit and self.low_disk_checkbox.isChecked(),
            default_crs=default_crs,
        )

        self.log_view.clear()
        if settings.pure_convert:
            self._log(
                bi(
                    "Rastry: czysta konwersja formatu - bez przeliczen (nie"
                    " wykonuje transformacji)",
                    "Rasters: pure format conversion - no transformation",
                )
            )
        else:
            self._log(
                bi(
                    "Rastry - uklad wspolrzednych bez zmian",
                    "Rasters - CRS unchanged",
                )
            )
        self._log(bi("Rozpoczynam przetwarzanie...", "Processing started..."))
        if force_8bit and settings.low_disk:
            self._log(
                bi(
                    "Wymuszony 8-bit, tryb malo miejsca na dysku: zakres z"
                    " wycinkow wirtualnych, potem arkusz po arkuszu"
                    " (wolniej)...",
                    "Forced 8-bit, low disk space mode: range from virtual"
                    " clips, then sheet by sheet (slower)...",
                )
            )
        elif force_8bit:
            self._log(
                bi(
                    "Wymuszony 8-bit: najpierw wycinam wszystkie rastry, potem"
                    " licze wspolny zakres wartosci wewnatrz arkuszy...",
                    "Forced 8-bit: clipping all rasters first, then computing"
                    " the common value range inside the sheets...",
                )
            )

        self._set_busy(True)
        self._task = ClipRastersTask(settings, self._on_task_finished)
        self._task.progressChanged.connect(
            lambda p: self.progress_bar.setValue(int(p))
        )
        QgsApplication.taskManager().addTask(self._task)

    def _cancel(self):
        if self._task is not None:
            self._task.cancel()
            self._log(
                bi(
                    "Anulowano na zyczenie uzytkownika...",
                    "Canceled by the user...",
                )
            )

    def _set_busy(self, busy):
        self.run_button.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)
        if busy:
            self.progress_bar.setValue(0)

    def _notify(self, text, level="info"):
        """Komunikat nieblokujacy (pasek komunikatow QGIS, znika sam).
        Non-blocking message (QGIS message bar, disappears by itself)."""
        title = "Wycinanie i konwersja QGIS 4 / Clipping and conversion QGIS 4"
        levels = {
            "info": (Qgis.MessageLevel.Info, 10),
            "success": (Qgis.MessageLevel.Success, 10),
            "warning": (Qgis.MessageLevel.Warning, 15),
            "critical": (Qgis.MessageLevel.Critical, 20),
        }
        lvl, duration = levels.get(level, levels["info"])
        try:
            if iface is not None and iface.messageBar() is not None:
                iface.messageBar().pushMessage(
                    title, text, level=lvl, duration=duration
                )
                return
        except Exception as exc:  # noqa: BLE001
            # brak paska komunikatow (np. QGIS zamykany) - komunikat trafi do
            # logu okna
            self._log(str(exc))
        self._log(text, color="red" if level == "critical" else None)

    def _on_task_finished(self, success, result, exception):
        self._set_busy(False)
        self._task = None

        if exception is not None:
            self._log(
                bi("BLAD KRYTYCZNY", "CRITICAL ERROR")
                + ": {}".format(exception),
                color="red",
            )
            self._notify(
                bi("Wystapil blad", "An error occurred")
                + ": {}".format(exception),
                "critical",
            )
            return
        if result is None:
            return
        if not success:
            self._log(
                bi("Przetwarzanie przerwane.", "Processing interrupted.")
            )

        self._log("")
        self._log("== PODSUMOWANIE / SUMMARY ==")
        for w in result.warnings:
            self._log("  " + w)

        processed = result.processed
        skipped = result.skipped
        self._log(
            bi(
                "Przetworzono poprawnie: {}", "Processed successfully: {}"
            ).format(len(processed), len(processed))
        )
        for row in processed:
            self._log(
                "  OK  {}{}  [{}]".format(
                    row.raster,
                    "  <-  " + row.sheet if row.sheet else "",
                    row.method or "",
                )
            )
            for n in row.notes:
                self._log("     !  " + n, color="#b06000")

        if skipped:
            self._log(
                bi("Pominieto: {}", "Skipped: {}").format(
                    len(skipped), len(skipped)
                ),
                color="red",
            )
            for row in skipped:
                self._log(
                    "  [{}]  {}{}: {}".format(
                        row.status,
                        row.raster or "-",
                        " / " + row.sheet if row.sheet else "",
                        " | ".join(row.notes),
                    ),
                    color="red",
                )

        if result.report_path:
            self._log("")
            self._log(
                bi("Raport CSV", "CSV report")
                + ": {}".format(result.report_path)
            )

        self.progress_bar.setValue(100)
        self._notify(
            "{} {}: {}, {}: {}. {}".format(
                bi("Rastry - zakonczono.", "Rasters - finished."),
                bi("Przetworzono", "Processed"),
                len(processed),
                bi("Pominieto", "Skipped"),
                len(skipped),
                bi(
                    "Szczegoly w logu i raporcie CSV.",
                    "Details in the log and CSV report.",
                ),
            ),
            "warning" if skipped or not success else "success",
        )

    # ==================================================================
    # ZAKLADKA WEKTORY / VECTORS TAB
    # ==================================================================

    def _build_vector_tab(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        lay = QVBoxLayout(content)

        warn = QLabel(
            "Bez przeliczen (nie wykonuje transformacji): wspolrzedne i uklad"
            " danych pozostaja bez zmian. No recalculation (no"
            " transformation): data coordinates and CRS stay unchanged."
        )
        warn.setWordWrap(True)
        warn.setStyleSheet("color: red; font-weight: bold;")
        lay.addWidget(warn)

        # -- foldery --
        fbox = QGroupBox(bi("Foldery", "Folders"))
        flay = QVBoxLayout()
        self.v_input_edit = self._folder_row(
            flay,
            lb("1. Folder z danymi wektorowymi", "1. Vector data folder"),
            bi("Wybierz folder z danymi...", "Select the data folder..."),
            tt(
                "Wszystkie dane wektorowe czytane przez QGIS w tym folderze:"
                " SHP,\nGPKG, geobaza ESRI (folder .gdb), GML, GeoJSON, KML,"
                " DXF, TAB/MIF,\nCSV, FlatGeobuf, SQLite, GPX. Przetwarzane sa"
                " wszystkie warstwy\nkazdego zrodla. Mozna tez wskazac"
                " bezposrednio folder geobazy .gdb.",
                "All vector data readable by QGIS in this folder: SHP, GPKG,"
                " ESRI\nFile Geodatabase (.gdb folder), GML, GeoJSON, KML,"
                " DXF, TAB/MIF,\nCSV, FlatGeobuf, SQLite, GPX. All layers of"
                " every source are\nprocessed. A .gdb geodatabase folder can"
                " also be selected directly.",
            ),
        )

        self.v_clip_checkbox = QCheckBox(
            lb(
                "Wycinaj poligonami z folderu",
                "Clip by polygons from a folder",
            )
        )
        self.v_clip_checkbox.setToolTip(
            tt(
                "Zaznaczone: kazda warstwa poligonowa w folderze nr 2 to jeden"
                " obszar\nwycinania (arkusz albo poligon o dowolnym"
                " ksztalcie). Dla kazdego\nobszaru powstaje osobny katalog"
                " wynikowy o nazwie tej warstwy\n(1 warstwa = 1 katalog, 5"
                " warstw = 5 katalogow). Kilka poligonow\nw jednej warstwie ="
                " wycinanie po ich sumie.\n\nOdznaczone: sama konwersja"
                " formatu.",
                "Checked: every polygon layer in folder 2 is one clipping area"
                " (a\nmap sheet or a polygon of any shape). Each area gets its"
                " own\noutput folder named after that layer (1 layer = 1"
                " folder, 5 layers\n= 5 folders). Several polygons in one"
                " layer = clipping by their\nunion.\n\nUnchecked: format"
                " conversion only.",
            )
        )
        self.v_clip_checkbox.toggled.connect(self._on_v_clip_toggled)
        flay.addWidget(self.v_clip_checkbox)

        self.v_clip_edit = self._folder_row(
            flay,
            lb(
                "2. Folder z poligonami wycinajacymi (.shp / .gpkg / .gdb"
                " ...)",
                "2. Clipping polygons folder (.shp / .gpkg / .gdb ...)",
            ),
            bi(
                "Wybierz folder z poligonami...",
                "Select the polygons folder...",
            ),
            tt(
                "Nazwa katalogu wynikowego = nazwa warstwy wycinajacej (dla"
                " SHP -\nnazwa pliku, dla GPKG/GDB - nazwa warstwy).",
                "Output folder name = clipping layer name (for SHP - the file"
                " name,\nfor GPKG/GDB - the layer name).",
            ),
        )
        self.v_output_edit = self._folder_row(
            flay,
            lb("3. Folder wyjsciowy", "3. Output folder"),
            bi("Wybierz folder docelowy...", "Select the output folder..."),
            tt(
                "Folder na wyniki i raport raport_wektory_....csv. Musi byc"
                " inny niz\nfolder z danymi.",
                "Folder for the results and the report raport_wektory_....csv."
                " It\nmust differ from the data folder.",
            ),
        )
        fbox.setLayout(flay)
        lay.addWidget(fbox)

        # -- format --
        fmt_box = QGroupBox(bi("Format wyjsciowy", "Output format"))
        fmt_lay = QVBoxLayout()
        self.v_format_combo = QComboBox()
        for key, label in available_vector_formats():
            self.v_format_combo.addItem(label, key)
        self.v_format_combo.setToolTip(
            tt(
                "Formaty dostepne w tej instalacji QGIS/GDAL. Zapis zawsze w"
                " ukladzie\ndanych zrodlowych (KML pominiety - wymaga WGS84,"
                " czyli przeliczenia).\n\nGPKG, geobaza (.gdb), SQLite: jeden"
                " plik na zrodlo, wszystkie warstwy.\nSHP, TAB, GeoJSON, GML,"
                " FlatGeobuf, CSV, DXF: plik na kazda warstwe.\n\nSHP: nazwy"
                " pol do 10 znakow, kodowanie UTF-8 (.cpg). DXF:"
                " bez\natrybutow. CSV: geometria jako WKT, bez ukladu.",
                "Formats available in this QGIS/GDAL installation. Always"
                " written in\nthe source CRS (KML omitted - it requires WGS84,"
                " i.e. a transformation).\n\nGPKG, geodatabase (.gdb), SQLite:"
                " one file per source, all layers.\nSHP, TAB, GeoJSON, GML,"
                " FlatGeobuf, CSV, DXF: one file per layer.\n\nSHP: field"
                " names up to 10 characters, UTF-8 encoding (.cpg). DXF:\nno"
                " attributes. CSV: geometry as WKT, no CRS.",
            )
        )
        self.v_format_combo.currentIndexChanged.connect(
            lambda _i: self._on_v_format_changed()
        )
        self._narrow_combo(self.v_format_combo)
        fmt_lay.addWidget(self.v_format_combo)
        self.v_subfolder_checkbox = QCheckBox(
            lb(
                "Osobny podfolder dla kazdego zrodla (SHP, TAB, GeoJSON...)",
                "Separate subfolder for each source (SHP, TAB, GeoJSON...)",
            )
        )
        self.v_subfolder_checkbox.setChecked(True)
        self.v_subfolder_checkbox.setToolTip(
            tt(
                "Zaznaczone: zrodlo/warstwa.shp (np."
                " baza/budynki.shp).\nOdznaczone: wszystko w jednym folderze"
                " jako zrodlo_warstwa.shp.",
                "Checked: source/layer.shp (e.g. baza/budynki.shp).\n"
                "Unchecked: everything in one folder as source_layer.shp.",
            )
        )
        fmt_lay.addWidget(self.v_subfolder_checkbox)
        fmt_box.setLayout(fmt_lay)
        lay.addWidget(fmt_box)

        # -- wycinanie --
        self.v_clip_box = QGroupBox(bi("Wycinanie", "Clipping"))
        clay = QVBoxLayout()
        clay.addWidget(QLabel(lb("Sposob wycinania:", "Clipping method:")))
        self.v_mode_combo = QComboBox()
        self.v_mode_combo.addItem(
            bi(
                "Przycinaj geometrie na granicy obszaru",
                "Clip geometries at the area boundary",
            ),
            MODE_CLIP,
        )
        self.v_mode_combo.addItem(
            bi(
                "Wybierz obiekty w calosci (bez ciecia)",
                "Select whole features (no cutting)",
            ),
            MODE_SELECT,
        )
        self.v_mode_combo.setToolTip(
            tt(
                "Przycinaj: obiekty sa ciete na granicy obszaru (np. droga"
                " konczy\nsie na ramce arkusza); linie i poligony zapisywane"
                " jako 'multi'.\n\nWybierz w calosci: brane sa obiekty, ktore"
                " przecinaja obszar -\nbez zadnej zmiany geometrii (moga"
                " wystawac poza obszar).",
                "Clip: features are cut at the area boundary (e.g. a road ends"
                " at\nthe sheet frame); lines and polygons are written as"
                " 'multi'.\n\nSelect whole: features intersecting the area are"
                " taken - without\nany change of geometry (they may extend"
                " beyond the area).",
            )
        )
        self._narrow_combo(self.v_mode_combo)
        clay.addWidget(self.v_mode_combo)
        self.v_reproject_checkbox = QCheckBox(
            lb(
                "Przeliczaj poligon z innego ukladu (dane bez zmian)",
                "Reproject the polygon from another CRS (data unchanged)",
            )
        )
        self.v_reproject_checkbox.setToolTip(
            tt(
                "Odznaczone (domyslnie): poligon i dane musza byc w tym samym"
                " ukladzie\n- przy roznych ukladach warstwa jest pomijana"
                " (raport CSV).\n\nZaznaczone: przeliczany jest WYLACZNIE"
                " poligon wycinajacy (do ukladu\ndanych). Dane nigdy nie sa"
                " przeliczane.",
                "Unchecked (default): the polygon and the data must share the"
                " same\nCRS - with different CRSs the layer is skipped (CSV"
                " report).\n\nChecked: ONLY the clipping polygon is"
                " transformed (to the data\nCRS). The data are never"
                " transformed.",
            )
        )
        clay.addWidget(self.v_reproject_checkbox)
        self.v_clip_box.setLayout(clay)
        lay.addWidget(self.v_clip_box)

        info = QLabel(
            lb(
                "Warstwy bez obiektow w obszarze nie tworza pustych plikow -"
                " sa wpisywane do raportu CSV.",
                "Layers without features in the area do not create empty files"
                " - they are listed in the CSV report.",
            )
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: gray; font-size: 11px;")
        lay.addWidget(info)
        lay.addStretch()

        self._on_v_clip_toggled(False)
        self._on_v_format_changed()
        scroll.setWidget(content)
        return scroll

    def _on_v_clip_toggled(self, checked):
        for w in (self.v_clip_edit,) + self.v_clip_edit.row_widgets:
            w.setEnabled(checked)
        self.v_clip_box.setEnabled(checked)
        self._update_run_text()

    def _on_v_format_changed(self):
        from .vector_core import _format_entry

        multi = _format_entry(self.v_format_combo.currentData())[4]
        self.v_subfolder_checkbox.setEnabled(not multi)

    def _run_vector(self):
        input_dir = self.v_input_edit.text().strip()
        clip_dir = self.v_clip_edit.text().strip()
        output_dir = self.v_output_edit.text().strip()
        if not input_dir or not os.path.isdir(input_dir):
            QMessageBox.warning(
                self,
                ERR,
                bi(
                    "Wskaz poprawny folder z danymi wektorowymi.",
                    "Select a valid vector data folder.",
                ),
            )
            return
        if self.v_clip_checkbox.isChecked() and (
            not clip_dir or not os.path.isdir(clip_dir)
        ):
            QMessageBox.warning(
                self,
                ERR,
                bi(
                    "Wskaz poprawny folder z poligonami wycinajacymi.",
                    "Select a valid clipping polygons folder.",
                ),
            )
            return
        if not output_dir:
            QMessageBox.warning(
                self,
                ERR,
                bi("Wskaz folder wyjsciowy.", "Select the output folder."),
            )
            return
        if os.path.abspath(input_dir) == os.path.abspath(output_dir):
            QMessageBox.warning(
                self,
                ERR,
                bi(
                    "Folder wyjsciowy musi byc inny niz folder z danymi.",
                    "The output folder must differ from the data folder.",
                ),
            )
            return
        settings = VectorSettings(
            input_dir=input_dir,
            output_dir=output_dir,
            output_format=self.v_format_combo.currentData(),
            clip_dir=clip_dir if self.v_clip_checkbox.isChecked() else None,
            clip_mode=self.v_mode_combo.currentData(),
            reproject_clip=self.v_reproject_checkbox.isChecked(),
            subfolder_per_source=self.v_subfolder_checkbox.isChecked(),
        )
        self.log_view.clear()
        self._log(
            bi(
                "Wektory - bez przeliczen (nie wykonuje transformacji)",
                "Vectors - no transformation",
            )
        )
        self._log(bi("Rozpoczynam przetwarzanie...", "Processing started..."))
        self._set_busy(True)
        self._task = VectorTask(settings, self._on_vector_finished)
        self._task.progressChanged.connect(
            lambda p: self.progress_bar.setValue(int(p))
        )
        QgsApplication.taskManager().addTask(self._task)

    def _on_vector_finished(self, success, result, exception):
        self._set_busy(False)
        self._task = None
        if exception is not None:
            self._log(
                bi("BLAD KRYTYCZNY", "CRITICAL ERROR")
                + ": {}".format(exception),
                color="red",
            )
            self._notify(
                bi("Wystapil blad", "An error occurred")
                + ": {}".format(exception),
                "critical",
            )
            return
        if result is None:
            return
        if not success:
            self._log(
                bi("Przetwarzanie przerwane.", "Processing interrupted.")
            )
        self._log("")
        self._log("== PODSUMOWANIE / SUMMARY ==")
        for w in result.warnings:
            self._log("  " + w)
        ok = result.processed
        bad = result.skipped
        self._log(
            bi("Warstw zapisanych: {}", "Layers written: {}").format(
                len(ok), len(ok)
            )
        )
        for r in ok:
            self._log(
                "  OK  {}{} : {}  ({} {})".format(
                    r.area + " / " if r.area else "",
                    r.source,
                    r.layer,
                    r.count,
                    bi("obiektow", "features"),
                )
            )
            for n in r.notes:
                self._log("     !  " + n, color="#b06000")
        if bad:
            self._log(
                bi("Pominieto: {}", "Skipped: {}").format(len(bad), len(bad)),
                color="red",
            )
            for r in bad:
                self._log(
                    "  [{}]  {}{} : {} - {}".format(
                        r.status,
                        r.area + " / " if r.area else "",
                        r.source,
                        r.layer,
                        " | ".join(r.notes),
                    ),
                    color=(
                        "#b06000"
                        if r.status.startswith("brak obiektow")
                        else "red"
                    ),
                )
        if result.report_path:
            self._log("")
            self._log(
                bi("Raport CSV", "CSV report")
                + ": {}".format(result.report_path)
            )
        self.progress_bar.setValue(100)
        if not ok and not bad:
            msg = "{} {}".format(
                bi(
                    "Nie znaleziono zadnych danych wektorowych do"
                    " przetworzenia.",
                    "No vector data to process was found.",
                ),
                bi(
                    "Wskaz folder z plikami (SHP, GPKG...) albo folder geobazy"
                    " .gdb.",
                    "Select a folder with files (SHP, GPKG...) or a .gdb"
                    " geodatabase folder.",
                ),
            )
            self._log(msg, color="red")
            self._notify(msg, "warning")
            return
        self._notify(
            "{} {}: {}, {}: {}. {}".format(
                bi("Wektory - zakonczono.", "Vectors - finished."),
                bi("Warstw zapisanych", "Layers written"),
                len(ok),
                bi("Pominieto", "Skipped"),
                len(bad),
                bi(
                    "Szczegoly w logu i raporcie CSV.",
                    "Details in the log and CSV report.",
                ),
            ),
            "warning" if bad or not success else "success",
        )
