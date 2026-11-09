# -*- coding: utf-8 -*-
"""
Silnik wycinania rastrow wg siatki arkuszy - czysty GDAL/OGR (bez QGIS),
dzieki czemu moze byc testowany i uzywany niezaleznie od interfejsu.
Clipping engine (pure GDAL/OGR, no QGIS dependency).

Zalozenia / assumptions:
  * jeden plik wektorowy (.shp / .gpkg) = jeden arkusz mapy i nazywa sie
    tak samo jak raster (np. N3450Cd3.shp <-> N3450Cd3.tif), porownanie
    nazw bez rozrozniania wielkosci liter,
  * uklad wspolrzednych rastra NIE jest zmieniany,
  * plik wynikowy jest przyciety do zasiegu poligonu arkusza (zasieg
    dopasowany do siatki pikseli rastra zrodlowego - bez przesuniecia
    pikseli), a wszystko poza poligonem staje sie NoData (albo
    przezroczyste - kanal alfa, do wyboru),
  * pliki robocze trzymane sa w podfolderze katalogu wejsciowego; kazdy
    roboczy plik jest usuwany zaraz po uzyciu, a folder - po zakonczeniu.

Wszystkie komunikaty dla uzytkownika sa dwujezyczne: "polski / English".
"""

import csv
import datetime
import math
import os
import logging

import shutil
import tempfile

_LOG = logging.getLogger(__name__)

# Rozszerzenia plikow rastrowych wykrywanych w folderze wejsciowym.
# .pdf traktowany jest wylacznie jako zrodlo rastrowe (GeoPDF) - tresc
# wektorowa/OCG jest rasteryzowana przy zadanym DPI.
RASTER_EXTENSIONS = {
    ".tif",
    ".tiff",
    ".img",
    ".asc",
    ".jp2",
    ".bil",
    ".bsq",
    ".bip",
    ".dem",
    ".hgt",
    ".nc",
    ".grd",
    ".png",
    ".bmp",
    ".ecw",
    ".sid",
    ".pdf",
    ".jpg",
    ".jpeg",
    ".gif",
}

# Rozszerzenia plikow arkuszy (siatki) - w kolejnosci pierwszenstwa,
# gdyby w folderze byly oba pliki o tej samej nazwie.
SHEET_EXTENSIONS = [".shp", ".gpkg"]

# Kolejnosc i kody metod resamplingu (nazwy zgodne z gdalwarp -r).
RESAMPLING_METHODS = [
    ("near", "Najblizszy sasiad / Nearest neighbour"),
    ("bilinear", "Dwuliniowa / Bilinear"),
    ("cubic", "Szescienna / Cubic"),
    ("cubicspline", "Szescienna B-spline / Cubic spline"),
    ("lanczos", "Lanczos"),
    ("average", "Srednia / Average"),
    ("mode", "Dominanta / Mode"),
    ("max", "Maksimum / Maximum"),
    ("min", "Minimum"),
    ("med", "Mediana / Median"),
    ("q1", "Pierwszy kwartyl / First quartile (Q1)"),
    ("q3", "Trzeci kwartyl / Third quartile (Q3)"),
]

# Tryby oznaczania obszaru poza arkuszem
OUTSIDE_NODATA = "nodata"
OUTSIDE_ALPHA = "alpha"

# Statusy w raporcie CSV (dwujezyczne)
ST_OK = "OK"
ST_NO_SHEET = "brak arkusza / no sheet file"
ST_SHEET_NO_RASTER = "arkusz bez rastra / sheet without raster"
ST_CRS_MISMATCH = "niezgodny uklad / CRS mismatch"
ST_NO_POLYGON = "brak poligonu / no polygon"
ST_NO_NAMED_POLYGON = (
    "brak poligonu o nazwie arkusza / no polygon with sheet name"
)
ST_DUP_NAMED_POLYGON = (
    "kilka poligonow o tej samej nazwie / several polygons with the same name"
)
ST_OUTSIDE = "arkusz poza zasiegiem rastra / sheet outside raster extent"
ST_NAME_COLLISION = (
    "kolizja nazwy pliku wyjsciowego / output file name collision"
)
ST_ERROR = "blad / error"
ST_EMPTY = "pusty wycinek / empty clip"
ST_CANCELED = "nie przetworzono (anulowano) / not processed (canceled)"

CSV_HEADER = ["raster", "arkusz / sheet", "status", "uwagi / notes"]

# Formaty wyjsciowe: (klucz, etykieta, sterownik GDAL, rozszerzenie, opcje
# tworzenia)
FORMAT_ORIGINAL = "original"
OUTPUT_FORMATS = [
    (FORMAT_ORIGINAL, "Jak plik zrodlowy / Same as source", None, None, []),
    ("GTiff", "GeoTIFF (.tif)", "GTiff", ".tif", []),
    ("COG", "Cloud Optimized GeoTIFF (.tif)", "COG", ".tif", []),
    ("JPEG", "JPEG (.jpg)", "JPEG", ".jpg", ["QUALITY=90"]),
    ("PNG", "PNG (.png)", "PNG", ".png", []),
    ("JP2OpenJPEG", "JPEG 2000 (.jp2)", "JP2OpenJPEG", ".jp2", ["QUALITY=90"]),
    ("WEBP", "WebP (.webp)", "WEBP", ".webp", ["QUALITY=90"]),
    ("BMP", "BMP (.bmp)", "BMP", ".bmp", []),
    ("HFA", "ERDAS Imagine (.img)", "HFA", ".img", []),
]

# Plik georeferencyjny (world file) dopisywany automatycznie wg rozszerzenia
# pliku wynikowego. Formaty z georeferencja w naglowku (np. .img) - brak.
WORLDFILE_EXT = {
    ".tif": ".tfw",
    ".tiff": ".tfw",
    ".jpg": ".jgw",
    ".jpeg": ".jgw",
    ".png": ".pgw",
    ".bmp": ".bpw",
    ".gif": ".gfw",
    ".jp2": ".j2w",
    ".webp": ".wld",
}

# Formaty bez obslugi palety barw - raster z paleta rozwijany do RGB
NO_PALETTE = ("JPEG", "WEBP", "JP2OpenJPEG")

# Jak dany format zapisuje przezroczystosc: maska wewnetrzna / kanal alfa
# (brak wpisu = format nie obsluguje przezroczystosci).
TRANSPARENCY = {
    "GTiff": "mask",
    "COG": "mask",
    "JPEG": "mask",
    "PNG": "alpha",
    "WEBP": "alpha",
    "JP2OpenJPEG": "alpha",
}


def available_output_formats():
    """Formaty wyjsciowe dostepne w tej instalacji GDAL.

    Zwraca liste [(klucz, etykieta)].
    """
    from osgeo import gdal

    out = []
    for key, label, drv, _ext, _opts in OUTPUT_FORMATS:
        if drv is None or gdal.GetDriverByName(drv) is not None:
            out.append((key, label))
    return out


def _format_entry(key):
    for entry in OUTPUT_FORMATS:
        if entry[0] == key:
            return entry
    return OUTPUT_FORMATS[0]


WORK_DIR_PREFIX = "_wycinanie_tmp_"

# Tolerancja (w pikselach) przy dociaganiu zasiegu do siatki - chroni
# przed "dodatkowym" pikselem wynikajacym z bledow zaokraglen float.
_SNAP_EPS = 1e-6


def bi(pl, en):
    """Komunikat dwujezyczny / bilingual message."""
    return "{} / {}".format(pl, en)


class ClipCanceled(Exception):
    """Przerwanie przetwarzania na zyczenie uzytkownika."""


class SkipPair(Exception):
    """Para raster-arkusz pominieta z okreslonym statusem do raportu."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class ClipSettings:
    """Wszystkie ustawienia przebiegu w jednym miejscu."""

    def __init__(
        self,
        input_dir,
        output_dir,
        sheets_dir=None,
        resampling_mode="near",
        output_format=FORMAT_ORIGINAL,
        compression=None,
        manual_nodata=None,
        input_dpi=None,
        output_dpi=None,
        force_8bit=False,
        default_nodata_black=False,
        pdf_dpi=300,
        output_suffix="",
        convert_only=False,
        pure_convert=False,
        outside_mode=OUTSIDE_NODATA,
        reproject_sheets=False,
        default_crs=None,
        low_disk=False,
    ):
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.sheets_dir = sheets_dir or input_dir
        self.resampling_mode = resampling_mode
        self.output_format = output_format
        self.compression = compression
        self.manual_nodata = manual_nodata
        self.input_dpi = input_dpi
        self.output_dpi = output_dpi
        self.force_8bit = force_8bit
        self.default_nodata_black = default_nodata_black
        self.pdf_dpi = pdf_dpi
        # convert_only: tylko konwersja formatu (bez wycinania po arkuszach)
        self.convert_only = convert_only or pure_convert
        # pure_convert: CZYSTA konwersja formatu (gdal_translate) - bez
        # zadnych przeliczen: piksele, georeferencja (takze obrot) i uklad
        # bez zmian; bez wycinania, resamplingu i DPI. 8-bit tylko dla
        # plikow, ktore nie sa juz 8-bit.
        self.pure_convert = pure_convert
        self.output_suffix = output_suffix or ""
        self.outside_mode = outside_mode
        self.reproject_sheets = reproject_sheets
        # default_crs: uklad dla plikow BEZ ukladu (np. JPG z samym .jgw),
        # gdy nie da sie go wziac z drugiego pliku (raster <-> arkusz);
        # np. "EPSG:2180". None = wycinanie bez ukladu (same wspolrzedne).
        self.default_crs = default_crs
        # low_disk: tryb "malo miejsca na dysku" (dotyczy wymuszonego
        # 8-bit) - na dysku jest najwyzej jeden roboczy plik naraz,
        # kosztem dluzszego przetwarzania.
        self.low_disk = low_disk


class ReportRow:
    def __init__(self, raster, sheet, status, notes=None, method=None):
        self.raster = raster or ""
        self.sheet = sheet or ""
        self.status = status
        self.notes = list(notes or [])
        self.method = method


class ClipResult:
    def __init__(self):
        self.rows = []  # ReportRow - jeden wiersz na raster/arkusz
        self.warnings = []  # ogolne komunikaty (nie dotyczace pary)
        self.report_path = None
        self.work_dir = None
        self.max_work_files = 0  # najwieksza liczba plikow roboczych naraz

    @property
    def processed(self):
        return [r for r in self.rows if r.status == ST_OK]

    @property
    def skipped(self):
        return [r for r in self.rows if r.status != ST_OK]


# ---------------------------------------------------------------------------
# Parowanie plikow
# ---------------------------------------------------------------------------


def _key(name):
    return name.strip().casefold()


def collect_rasters(folder):
    found = []
    for name in sorted(os.listdir(folder), key=str.casefold):
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        if os.path.splitext(name)[1].lower() in RASTER_EXTENSIONS:
            found.append(path)
    return found


def collect_sheets(folder):
    """Zwraca slownik klucz_nazwy -> lista sciezek (.shp przed .gpkg)."""
    sheets = {}
    for name in sorted(os.listdir(folder), key=str.casefold):
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        base, ext = os.path.splitext(name)
        if ext.lower() in SHEET_EXTENSIONS:
            sheets.setdefault(_key(base), []).append(path)
    for key in sheets:
        sheets[key].sort(
            key=lambda p: SHEET_EXTENSIONS.index(
                os.path.splitext(p)[1].lower()
            )
        )
    return sheets


def pair_files(input_dir, sheets_dir):
    """Paruje rastry z arkuszami po nazwie (bez rozszerzenia, bez
    rozrozniania wielkosci liter).

    Zwraca (pairs, rasters_without_sheet, sheets_without_raster)
      pairs: lista (raster_path, sheet_path, [uwagi])
    """
    rasters = collect_rasters(input_dir)
    sheets = collect_sheets(sheets_dir)
    pairs = []
    no_sheet = []
    used = set()
    for raster in rasters:
        key = _key(os.path.splitext(os.path.basename(raster))[0])
        candidates = sheets.get(key)
        if not candidates:
            no_sheet.append(raster)
            continue
        notes = []
        if len(candidates) > 1:
            names = ", ".join(os.path.basename(c) for c in candidates)
            used_name = os.path.basename(candidates[0])
            notes.append(
                bi(
                    "Znaleziono kilka plikow arkusza o tej nazwie ({}) -"
                    " uzyto {}".format(names, used_name),
                    "Several sheet files with this name ({}) - used {}".format(
                        names, used_name
                    ),
                )
            )
        pairs.append((raster, candidates[0], notes))
        used.add(key)
    sheets_without_raster = [
        paths[0] for key, paths in sheets.items() if key not in used
    ]
    sheets_without_raster.sort(key=lambda p: os.path.basename(p).casefold())
    return pairs, no_sheet, sheets_without_raster


# ---------------------------------------------------------------------------
# Odczyt arkusza (warstwa, poligon, uklad)
# ---------------------------------------------------------------------------


def _polygon_geometry(feature, ogr):
    geom = feature.GetGeometryRef()
    if geom is None or geom.IsEmpty():
        return None
    flat = ogr.GT_Flatten(geom.GetGeometryType())
    if flat in (
        ogr.wkbPolygon,
        ogr.wkbMultiPolygon,
        ogr.wkbCurvePolygon,
        ogr.wkbMultiSurface,
    ):
        return geom.Clone()
    if flat == ogr.wkbGeometryCollection:
        # tylko poligony z kolekcji
        polys = ogr.Geometry(ogr.wkbMultiPolygon)
        for i in range(geom.GetGeometryCount()):
            part = geom.GetGeometryRef(i)
            pflat = ogr.GT_Flatten(part.GetGeometryType())
            if pflat == ogr.wkbPolygon:
                polys.AddGeometry(part)
            elif pflat == ogr.wkbMultiPolygon:
                for j in range(part.GetGeometryCount()):
                    polys.AddGeometry(part.GetGeometryRef(j))
        return polys if polys.GetGeometryCount() else None
    return None


def read_sheet_polygon(sheet_path, sheet_name):
    """Zwraca (geometria_ogr, srs_osr_lub_None, [uwagi]).

    Rzuca SkipPair, jesli nie da sie jednoznacznie wybrac poligonu.
    """
    from osgeo import ogr, osr  # noqa: F401 - osr potrzebny do obiektow SRS

    notes = []
    ds = ogr.Open(sheet_path, 0)
    if ds is None:
        raise SkipPair(
            ST_ERROR,
            bi(
                "Nie udalo sie otworzyc pliku arkusza",
                "Could not open the sheet file",
            ),
        )

    layer_count = ds.GetLayerCount()
    if layer_count == 0:
        raise SkipPair(
            ST_NO_POLYGON,
            bi(
                "Plik arkusza nie zawiera zadnej warstwy",
                "The sheet file contains no layers",
            ),
        )

    layer = None
    if layer_count > 1:
        names = [ds.GetLayerByIndex(i).GetName() for i in range(layer_count)]
        for i, lname in enumerate(names):
            if _key(lname) == _key(sheet_name):
                layer = ds.GetLayerByIndex(i)
                notes.append(
                    bi(
                        "GPKG ma {} warstw - uzyto warstwy o nazwie arkusza"
                        " '{}'".format(layer_count, lname),
                        "GPKG has {} layers - used the layer named like the"
                        " sheet '{}'".format(layer_count, lname),
                    )
                )
                break
        if layer is None:
            layer = ds.GetLayerByIndex(0)
            joined = ", ".join(names)
            notes.append(
                bi(
                    "GPKG ma {} warstw ({}), zadna nie nazywa sie jak arkusz -"
                    " uzyto pierwszej: '{}'".format(
                        layer_count, joined, names[0]
                    ),
                    "GPKG has {} layers ({}), none is named like the sheet -"
                    " used the first one: '{}'".format(
                        layer_count, joined, names[0]
                    ),
                )
            )
    else:
        layer = ds.GetLayerByIndex(0)

    srs = layer.GetSpatialRef()
    srs = srs.Clone() if srs is not None else None

    defn = layer.GetLayerDefn()
    string_fields = [
        defn.GetFieldDefn(i).GetName()
        for i in range(defn.GetFieldCount())
        if defn.GetFieldDefn(i).GetType() in (ogr.OFTString, ogr.OFTWideString)
    ]

    polygons = []  # (geom, [(pole, wartosc)], fid)
    layer.ResetReading()
    for feat in layer:
        geom = _polygon_geometry(feat, ogr)
        if geom is None:
            continue
        values = []
        for fname in string_fields:
            if feat.IsFieldSetAndNotNull(fname):
                values.append((fname, feat.GetFieldAsString(fname)))
        polygons.append((geom, values, feat.GetFID()))

    ds = None

    if not polygons:
        raise SkipPair(
            ST_NO_POLYGON,
            bi(
                "W pliku arkusza nie ma zadnego poligonu",
                "The sheet file contains no polygon",
            ),
        )

    if len(polygons) == 1:
        return polygons[0][0], srs, notes

    # Kilka poligonow -> szukamy tego, ktory w ktoryms polu tekstowym ma
    # wartosc rowna nazwie arkusza (rastra).
    wanted = _key(sheet_name)
    matches = []
    for geom, values, fid in polygons:
        hit = [fname for fname, val in values if _key(val) == wanted]
        if hit:
            matches.append((geom, hit[0], fid))

    if not matches:
        raise SkipPair(
            ST_NO_NAMED_POLYGON,
            bi(
                "Plik ma {} poligonow i zaden nie ma w atrybutach tekstowych"
                " wartosci '{}'".format(len(polygons), sheet_name),
                "The file has {} polygons and none has the value '{}' in a"
                " text attribute".format(len(polygons), sheet_name),
            ),
        )
    if len(matches) > 1:
        fids = ", ".join(str(m[2]) for m in matches)
        raise SkipPair(
            ST_DUP_NAMED_POLYGON,
            bi(
                "Plik ma {} poligonow, a wartosc '{}' ma {} z nich (FID: {})"
                .format(len(polygons), sheet_name, len(matches), fids),
                "The file has {} polygons and {} of them have the value '{}'"
                " (FID: {})".format(
                    len(polygons), len(matches), sheet_name, fids
                ),
            ),
        )

    geom, fname, fid = matches[0]
    notes.append(
        bi(
            "Plik ma {} poligonow - wybrano poligon FID {} (pole '{}' = '{}')"
            .format(len(polygons), fid, fname, sheet_name),
            "The file has {} polygons - selected polygon FID {} (field '{}' ="
            " '{}')".format(len(polygons), fid, fname, sheet_name),
        )
    )
    return geom, srs, notes


def _srs_equal(a, b):
    try:
        return bool(
            a.IsSame(
                b,
                [
                    "IGNORE_DATA_AXIS_TO_SRS_AXIS_MAPPING=YES",
                    "CRITERION=EQUIVALENT_EXCEPT_AXIS_ORDER_GEOGCRS",
                ],
            )
        )
    except TypeError:  # starsze GDAL bez opcji
        return bool(a.IsSame(b))


def _srs_label(srs):
    if srs is None:
        return "-"
    try:
        srs_copy = srs.Clone()
        srs_copy.AutoIdentifyEPSG()
        code = srs_copy.GetAuthorityCode(None)
        if code:
            return "EPSG:{} ({})".format(code, srs.GetName())
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("Nie rozpoznano kodu EPSG: %s", exc)
    return srs.GetName() or "?"


def prepare_cutline(geom, sheet_srs, raster_srs, reproject):
    """Zwraca geometrie w ukladzie rastra + uwagi. Rzuca SkipPair przy
    niezgodnych ukladach (gdy przeliczanie jest wylaczone)."""
    from osgeo import osr

    notes = []
    if sheet_srs is None:
        notes.append(
            bi(
                "Plik arkusza nie ma ukladu wspolrzednych - przyjeto uklad"
                " rastra",
                "The sheet file has no CRS - the raster CRS was assumed",
            )
        )
        return geom, notes

    if _srs_equal(sheet_srs, raster_srs):
        return geom, notes

    sheet_lbl, raster_lbl = _srs_label(sheet_srs), _srs_label(raster_srs)
    if not reproject:
        raise SkipPair(
            ST_CRS_MISMATCH,
            bi(
                "Arkusz: {}, raster: {}. Zaznacz 'Przeliczaj z innego ukladu',"
                " jesli to zamierzone".format(sheet_lbl, raster_lbl),
                "Sheet: {}, raster: {}. Tick 'Reproject from another CRS' if"
                " this is intended".format(sheet_lbl, raster_lbl),
            ),
        )

    # Zageszczenie wierzcholkow przed przeliczeniem - prosta krawedz w
    # ukladzie arkusza moze byc lekko zakrzywiona w ukladzie rastra.
    env = geom.GetEnvelope()  # minx, maxx, miny, maxy
    span = max(env[1] - env[0], env[3] - env[2])
    if span > 0:
        geom = geom.Clone()
        geom.Segmentize(span / 200.0)

    src = sheet_srs.Clone()
    dst = raster_srs.Clone()
    src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    transform = osr.CoordinateTransformation(src, dst)
    err = geom.Transform(transform)
    if err != 0:
        raise SkipPair(
            ST_ERROR,
            bi(
                "Nie udalo sie przeliczyc granicy arkusza do ukladu rastra",
                "Could not transform the sheet boundary to the raster CRS",
            ),
        )
    notes.append(
        bi(
            "Granice arkusza przeliczono z {} do {}".format(
                sheet_lbl, raster_lbl
            ),
            "Sheet boundary transformed from {} to {}".format(
                sheet_lbl, raster_lbl
            ),
        )
    )
    return geom, notes


# ---------------------------------------------------------------------------
# Pomocnicze GDAL
# ---------------------------------------------------------------------------


def suggest_resampling(ds):
    """Heurystyka metody resamplingu z naglowka.

    Bierze pod uwage typ danych i liczbe kanalow.
    """
    from osgeo import gdal

    try:
        band_count = ds.RasterCount
        dtype = ds.GetRasterBand(1).DataType
        is_float = dtype in (
            gdal.GDT_Float32,
            gdal.GDT_Float64,
            gdal.GDT_CFloat32,
            gdal.GDT_CFloat64,
        )
        if band_count == 1 and not is_float:
            return "near"
        if is_float:
            return "cubic"
        return "bilinear"
    except Exception:  # noqa: BLE001
        return "bilinear"


def default_nodata_for(gdal, dtype, black):
    if dtype == gdal.GDT_Byte:
        return 0 if black else 255
    if dtype == gdal.GDT_UInt16:
        return 65535
    if dtype == gdal.GDT_Int16:
        return -32768
    if dtype == gdal.GDT_UInt32:
        return 4294967295
    if dtype == gdal.GDT_Int32:
        return -2147483648
    return -9999.0


def snap_bounds(gt, bounds, res_x, res_y):
    """Dociaga zasieg (minx, miny, maxx, maxy) do siatki pikseli o
    poczatku w (gt[0], gt[3]) i rozmiarze res_x x res_y (oba dodatnie) -
    zasieg jest rozszerzany na zewnatrz do pelnych pikseli."""
    ox, oy = gt[0], gt[3]
    minx, miny, maxx, maxy = bounds
    c0 = math.floor((minx - ox) / res_x + _SNAP_EPS)
    c1 = math.ceil((maxx - ox) / res_x - _SNAP_EPS)
    r0 = math.floor((oy - maxy) / res_y + _SNAP_EPS)
    r1 = math.ceil((oy - miny) / res_y - _SNAP_EPS)
    if c1 <= c0:
        c1 = c0 + 1
    if r1 <= r0:
        r1 = r0 + 1
    return (ox + c0 * res_x, oy - r1 * res_y, ox + c1 * res_x, oy - r0 * res_y)


def write_worldfile(gdal, raster_path, extensions):
    ds = gdal.Open(raster_path)
    if ds is None:
        raise RuntimeError(
            bi(
                "nie udalo sie otworzyc pliku wynikowego",
                "could not open the output file",
            )
        )
    gt = ds.GetGeoTransform(can_return_null=True)
    ds = None
    if gt is None:
        raise RuntimeError(
            bi(
                "plik wynikowy nie ma geotransformacji",
                "the output file has no geotransform",
            )
        )
    A, B, D, E = gt[1], gt[2], gt[4], gt[5]
    C = gt[0] + 0.5 * gt[1] + 0.5 * gt[2]
    F = gt[3] + 0.5 * gt[4] + 0.5 * gt[5]
    # pelna precyzja - plik georeferencyjny odtwarza geotransformacje 1:1
    content = "".join("{:.15f}\n".format(v) for v in (A, D, B, E, C, F))
    base = os.path.splitext(raster_path)[0]
    for wext in extensions:
        with open(base + wext, "w") as f:
            f.write(content)


def _iter_row_blocks(ysize, rows=512):
    y = 0
    while y < ysize:
        n = min(rows, ysize - y)
        yield y, n
        y += n


def data_bands(ds):
    """Numery kanalow danych (z pominieciem kanalu alfa)."""
    from osgeo import gdal

    return [
        b
        for b in range(1, ds.RasterCount + 1)
        if ds.GetRasterBand(b).GetColorInterpretation() != gdal.GCI_AlphaBand
    ]


def detect_hidden_alpha(ds, sample=512):
    """Wykrywa kanal alfa, ktory NIE jest oznaczony w pliku jako alfa.

    Typowy przypadek: skan mapy RGBA, w ktorym 4. kanal ma interpretacje
    'Undefined' zamiast 'Alpha'. Bez rozpoznania traktowany jest jak zwykly
    kanal danych (np. caly = 255 = NoData -> brak waznych pikseli).

    Warunki: dokladnie 4 kanaly Byte, kanaly 1-3 to R, G, B, kanal 4 nie
    jest oznaczony jako alfa i (na probce) zawiera wylacznie 0 i/lub 255.
    Kanal podczerwieni (RGB+NIR w ortofotomapach) ma rozne wartosci, wiec
    nie zostanie pomylony z alfa.

    Zwraca numer kanalu (4) albo None."""
    from osgeo import gdal
    import numpy as np

    if ds.RasterCount != 4:
        return None
    rgb = [ds.GetRasterBand(b).GetColorInterpretation() for b in (1, 2, 3)]
    if rgb != [gdal.GCI_RedBand, gdal.GCI_GreenBand, gdal.GCI_BlueBand]:
        return None
    band4 = ds.GetRasterBand(4)
    if (
        band4.DataType != gdal.GDT_Byte
        or band4.GetColorInterpretation() == gdal.GCI_AlphaBand
    ):
        return None
    w = min(sample, ds.RasterXSize)
    h = min(sample, ds.RasterYSize)
    arr = band4.ReadAsArray(
        buf_xsize=w, buf_ysize=h, resample_alg=gdal.GRIORA_NearestNeighbour
    )
    if arr is None:
        return None
    if np.isin(arr, (0, 255)).all():
        return 4
    return None


def masked_min_max(ds, cancel_check=None):
    """Min/max kazdego kanalu danych z pominieciem pikseli zamaskowanych
    (NoData albo przezroczyste w kanale alfa). Czyta blokami wierszy,
    zeby nie ladowac calego rastra do RAM."""
    import numpy as np

    bands = data_bands(ds)
    ranges = []
    for b in bands:
        band = ds.GetRasterBand(b)
        mask_band = band.GetMaskBand()
        bmin = None
        bmax = None
        for y, n in _iter_row_blocks(ds.RasterYSize):
            if cancel_check and cancel_check():
                raise ClipCanceled()
            arr = band.ReadAsArray(0, y, ds.RasterXSize, n)
            mask = mask_band.ReadAsArray(0, y, ds.RasterXSize, n) > 0
            if np.issubdtype(arr.dtype, np.floating):
                mask &= np.isfinite(arr)
            if not mask.any():
                continue
            vals = arr[mask]
            vmin = vals.min()
            vmax = vals.max()
            bmin = vmin if bmin is None or vmin < bmin else bmin
            bmax = vmax if bmax is None or vmax > bmax else bmax
        ranges.append(
            (
                None if bmin is None else float(bmin),
                None if bmax is None else float(bmax),
            )
        )
    return ranges


def _merge_ranges(total, part):
    for b, (bmin, bmax) in enumerate(part):
        if bmin is None:
            continue
        if total[b][0] is None or bmin < total[b][0]:
            total[b][0] = bmin
        if total[b][1] is None or bmax > total[b][1]:
            total[b][1] = bmax


# ---------------------------------------------------------------------------
# Glowny przebieg
# ---------------------------------------------------------------------------


class ClipRunner:
    """Wykonuje caly przebieg. Interfejs (QgsTask) przekazuje funkcje
    progress(procent) i is_canceled()."""

    def __init__(self, settings, progress=None, is_canceled=None):
        self.s = settings
        self._progress = progress or (lambda p: None)
        self._is_canceled = is_canceled or (lambda: False)
        self.result = ClipResult()
        self.work_dir = None

    # -- narzedzia --

    def _check_cancel(self):
        if self._is_canceled():
            raise ClipCanceled()

    def _gdal_callback(self, start, span):
        def cb(complete, _message, _data):
            self._progress(start + span * complete)
            return 0 if self._is_canceled() else 1

        return cb

    def _make_work_dir(self):
        try:
            self.work_dir = tempfile.mkdtemp(
                prefix=WORK_DIR_PREFIX, dir=self.s.input_dir
            )
        except OSError as exc:
            self.work_dir = tempfile.mkdtemp(prefix=WORK_DIR_PREFIX)
            self.result.warnings.append(
                bi(
                    "Nie udalo sie utworzyc folderu roboczego w folderze z"
                    " rastrami ({}) - uzyto folderu tymczasowego systemu: {}"
                    .format(exc, self.work_dir),
                    "Could not create the work folder in the raster folder"
                    " ({}) - the system temp folder was used: {}".format(
                        exc, self.work_dir
                    ),
                )
            )
        self.result.work_dir = self.work_dir

    def _note_work_usage(self):
        """Rejestruje, ile plikow roboczych lezy naraz w folderze roboczym
        (kontrola trybu 'malo miejsca na dysku')."""
        if self.work_dir and os.path.isdir(self.work_dir):
            n = len(
                [
                    f
                    for f in os.listdir(self.work_dir)
                    if f.lower().endswith((".tif", ".vrt"))
                ]
            )
            self.result.max_work_files = max(self.result.max_work_files, n)

    def _cleanup(self):
        if self.work_dir and os.path.isdir(self.work_dir):
            shutil.rmtree(self.work_dir, ignore_errors=True)
            if os.path.isdir(self.work_dir):
                self.result.warnings.append(
                    bi(
                        "Nie udalo sie w pelni usunac folderu roboczego: {}"
                        .format(self.work_dir),
                        "Could not fully remove the work folder: {}".format(
                            self.work_dir
                        ),
                    )
                )

    # -- przebieg --

    def run(self):
        from osgeo import gdal, ogr, osr

        gdal.UseExceptions()
        ogr.UseExceptions()
        osr.UseExceptions()

        canceled = False
        try:
            if self.s.convert_only:
                pairs = [
                    (r, "", []) for r in collect_rasters(self.s.input_dir)
                ]
                no_sheet, sheet_no_raster = [], []
            else:
                pairs, no_sheet, sheet_no_raster = pair_files(
                    self.s.input_dir, self.s.sheets_dir
                )
            for r in no_sheet:
                base = os.path.splitext(os.path.basename(r))[0]
                self.result.rows.append(
                    ReportRow(
                        os.path.basename(r),
                        "",
                        ST_NO_SHEET,
                        [
                            bi(
                                "Brak pliku .shp/.gpkg o nazwie '{}'".format(
                                    base
                                ),
                                "No .shp/.gpkg file named '{}'".format(base),
                            )
                        ],
                    )
                )
            for sh in sheet_no_raster:
                self.result.rows.append(
                    ReportRow(
                        "",
                        os.path.basename(sh),
                        ST_SHEET_NO_RASTER,
                        [
                            bi(
                                "Brak rastra o tej nazwie w folderze z"
                                " rastrami",
                                "No raster with this name in the raster"
                                " folder",
                            )
                        ],
                    )
                )

            if self.s.pure_convert:
                self.result.warnings.insert(
                    0,
                    bi(
                        "Czysta konwersja formatu - bez przeliczen (nie"
                        " wykonuje transformacji): {} rastrow".format(
                            len(pairs)
                        ),
                        "Pure format conversion - no transformation: {}"
                        " rasters".format(len(pairs)),
                    ),
                )
            elif self.s.convert_only:
                self.result.warnings.insert(
                    0,
                    bi(
                        "Tylko konwersja (bez wycinania): {} rastrow".format(
                            len(pairs)
                        ),
                        "Conversion only (no clipping): {} rasters".format(
                            len(pairs)
                        ),
                    ),
                )
            else:
                self.result.warnings.insert(
                    0,
                    bi(
                        "Parowanie: {} par raster-arkusz, {} rastrow bez"
                        " arkusza, {} arkuszy bez rastra".format(
                            len(pairs), len(no_sheet), len(sheet_no_raster)
                        ),
                        "Pairing: {} raster-sheet pairs, {} rasters without"
                        " sheet, {} sheets without raster".format(
                            len(pairs), len(no_sheet), len(sheet_no_raster)
                        ),
                    ),
                )

            if not pairs:
                self.result.warnings.append(
                    bi(
                        "Brak rastrow do przetworzenia",
                        "No rasters to process",
                    )
                )
                return True

            os.makedirs(self.s.output_dir, exist_ok=True)
            self._make_work_dir()

            # kolizje nazw wyjsciowych (np. N1.tif i N1.jpg przy wymuszonym
            # GeoTIFF)
            jobs = []
            seen_out = {}
            for raster, sheet, notes in pairs:
                job = _Job(raster, sheet, notes)
                job.out_path = self._output_path(raster)
                k = os.path.normcase(os.path.abspath(job.out_path))
                if k in seen_out:
                    out_name = os.path.basename(job.out_path)
                    self.result.rows.append(
                        ReportRow(
                            os.path.basename(raster),
                            os.path.basename(sheet),
                            ST_NAME_COLLISION,
                            [
                                bi(
                                    "Plik wyjsciowy {} powstaje juz z {}"
                                    .format(out_name, seen_out[k]),
                                    "Output file {} is already produced"
                                    " from {}".format(out_name, seen_out[k]),
                                )
                            ],
                        )
                    )
                    continue
                seen_out[k] = os.path.basename(raster)
                jobs.append(job)

            try:
                if self.s.pure_convert:
                    self._run_pure(gdal, jobs)
                elif self.s.force_8bit and self.s.low_disk:
                    self._run_8bit_low_disk(gdal, jobs)
                elif self.s.force_8bit:
                    self._run_8bit(gdal, jobs)
                else:
                    self._run_direct(gdal, jobs)
            except ClipCanceled:
                canceled = True
                for job in jobs:
                    if not job.done:
                        self.result.rows.append(
                            ReportRow(
                                os.path.basename(job.raster),
                                os.path.basename(job.sheet),
                                ST_CANCELED,
                                job.notes,
                            )
                        )
            self._progress(100.0)
            return not canceled
        finally:
            self._cleanup()
            self._write_report()

    def _handle_job_error(self, job, exc, partial_paths=()):
        """Wspolna obsluga bledow jednej pary. Zwraca True, jesli trzeba
        przerwac caly przebieg (anulowanie)."""
        if isinstance(exc, SkipPair):
            self._fail(job, exc.status, exc.message)
            return False
        if isinstance(exc, ClipCanceled) or self._is_canceled():
            for p in partial_paths:
                self._remove_partial(p)
            return True
        self._fail(job, ST_ERROR, str(exc))
        return False

    def _run_direct(self, gdal, jobs):
        total = len(jobs)
        for i, job in enumerate(jobs):
            self._check_cancel()
            start = 100.0 * i / total
            span = 100.0 / total
            self._progress(start)
            try:
                clip_path = self._clip(
                    gdal, job, final=True, cb=self._gdal_callback(start, span)
                )
                if clip_path != job.out_path:
                    # roboczy GTiff -> format docelowy, roboczy plik od razu
                    # usuwany
                    self._copy_to_format(gdal, job, clip_path)
                self._finish_job(gdal, job)
            except Exception as exc:  # noqa: BLE001
                if self._handle_job_error(job, exc, [job.out_path]):
                    raise ClipCanceled()

    def _run_8bit(self, gdal, jobs):
        """Tryb domyslny (szybszy): najpierw wszystkie wycinki do folderu
        roboczego, potem wspolny zakres i skalowanie. W czasie pracy na
        dysku leza wszystkie wycinki naraz."""
        total = len(jobs)
        # Przebieg 1: wyciecie wszystkich rastrow do folderu roboczego (0-60%)
        clipped = []
        for i, job in enumerate(jobs):
            self._check_cancel()
            start = 60.0 * i / total
            try:
                job.work_path = self._clip(
                    gdal,
                    job,
                    final=False,
                    cb=self._gdal_callback(start, 60.0 / total),
                )
                self._note_work_usage()
                clipped.append(job)
            except Exception as exc:  # noqa: BLE001
                if self._handle_job_error(job, exc):
                    raise ClipCanceled()

        if not clipped:
            return

        # Przebieg 2: wspolny zakres wartosci - tylko piksele wewnatrz arkuszy,
        # tylko dla plikow, ktore NIE sa juz 8-bit (60-75%)
        for i, job in enumerate(clipped):
            self._check_cancel()
            self._progress(60.0 + 15.0 * i / len(clipped))
            ds = gdal.Open(job.work_path)
            self._inspect_bands(gdal, ds, job)
            if not job.is_byte:
                job.ranges = masked_min_max(ds, self._is_canceled)
            ds = None

        clipped, ranges, band_count = self._common_ranges(clipped)
        if not clipped:
            return

        # Przebieg 3: skalowanie do 8-bit i zapis wynikow (75-100%)
        for i, job in enumerate(clipped):
            self._check_cancel()
            start = 75.0 + 25.0 * i / len(clipped)
            try:
                self._scale_to_8bit(
                    gdal,
                    job,
                    self._job_ranges(job, ranges, band_count),
                    self._gdal_callback(start, 25.0 / len(clipped)),
                )
                job.method += " + 8-bit"
                self._finish_job(gdal, job)
            except Exception as exc:  # noqa: BLE001
                if self._handle_job_error(job, exc, [job.out_path]):
                    raise ClipCanceled()
            finally:
                self._remove_partial(job.work_path)

    def _run_8bit_low_disk(self, gdal, jobs):
        """Tryb 'malo miejsca na dysku': zakres wartosci liczony z wycinkow
        wirtualnych (VRT - wycinanie w locie przy odczycie, bez zapisu
        rastra na dysk), a potem kazdy arkusz po kolei: wyciecie do
        roboczego .tif -> skalowanie 8-bit -> zapis -> natychmiastowe
        usuniecie roboczego pliku. Na dysku jest najwyzej jeden roboczy
        plik naraz; kosztem jest podwojne wycinanie (wolniej)."""
        total = len(jobs)

        # Przebieg 1: statystyki z wycinkow wirtualnych (0-45%)
        ok_jobs = []
        for i, job in enumerate(jobs):
            self._check_cancel()
            self._progress(45.0 * i / total)
            vrt_path = None
            try:
                vrt_path = self._clip(
                    gdal, job, final=False, cb=None, virtual=True
                )
                self._note_work_usage()
                ds = gdal.Open(vrt_path)
                self._inspect_bands(gdal, ds, job)
                if not job.is_byte:
                    job.ranges = masked_min_max(ds, self._is_canceled)
                ds = None
                ok_jobs.append(job)
            except Exception as exc:  # noqa: BLE001
                if self._handle_job_error(job, exc):
                    raise ClipCanceled()
            finally:
                self._remove_partial(vrt_path)
                self._remove_partial(job.pdf_tmp)
                job.pdf_tmp = None

        if not ok_jobs:
            return

        ok_jobs, ranges, band_count = self._common_ranges(ok_jobs)
        if not ok_jobs:
            return

        # Przebieg 2: arkusz po arkuszu - wyciecie, 8-bit, usuniecie roboczego
        # (45-100%)
        n = len(ok_jobs)
        for i, job in enumerate(ok_jobs):
            self._check_cancel()
            start = 45.0 + 55.0 * i / n
            job.notes = job.notes[: job.notes_after_pairing]
            job.work_path = None
            try:
                job_ranges = self._job_ranges(job, ranges, band_count)
                job.work_path = self._clip(
                    gdal,
                    job,
                    final=False,
                    cb=self._gdal_callback(start, 35.0 / n),
                )
                self._note_work_usage()
                self._scale_to_8bit(
                    gdal,
                    job,
                    job_ranges,
                    self._gdal_callback(start + 35.0 / n, 20.0 / n),
                )
                job.method += " + 8-bit"
                self._finish_job(gdal, job)
            except Exception as exc:  # noqa: BLE001
                if self._handle_job_error(job, exc, [job.out_path]):
                    raise ClipCanceled()
            finally:
                self._remove_partial(job.work_path)
                job.work_path = None

    # -- czysta konwersja (gdal_translate, bez przeliczen) --

    def _run_pure(self, gdal, jobs):
        """Czysta konwersja: gdal_translate zrodlo -> format docelowy. Nic nie
        jest przeliczane geometrycznie (brak resamplingu, wycinania, zmiany
        siatki czy ukladu). Przy 'Wymus 8-bit' rozciagane sa tylko pliki,
        ktore nie sa juz 8-bit (wspolny zakres z calych plikow)."""
        total = len(jobs)
        ranges = band_count = None
        if self.s.force_8bit:
            stat_jobs = []
            for i, job in enumerate(jobs):
                self._check_cancel()
                self._progress(30.0 * i / total)
                try:
                    src_path = self._pure_source(gdal, job)
                    ds = gdal.Open(src_path)
                    self._inspect_bands(gdal, ds, job)
                    if not job.is_byte:
                        job.ranges = masked_min_max(ds, self._is_canceled)
                    ds = None
                    stat_jobs.append(job)
                except Exception as exc:  # noqa: BLE001
                    if self._handle_job_error(job, exc):
                        raise ClipCanceled()
                finally:
                    self._remove_partial(job.pdf_tmp)
                    job.pdf_tmp = None
            jobs, ranges, band_count = self._common_ranges(stat_jobs)
            total = max(1, len(jobs))

        base = 30.0 if self.s.force_8bit else 0.0
        for i, job in enumerate(jobs):
            self._check_cancel()
            start = base + (100.0 - base) * i / total
            span = (100.0 - base) / total
            try:
                self._pure_one(
                    gdal,
                    job,
                    ranges,
                    band_count,
                    self._gdal_callback(start, span),
                )
                self._finish_job(gdal, job)
            except Exception as exc:  # noqa: BLE001
                if self._handle_job_error(job, exc, [job.out_path]):
                    raise ClipCanceled()
            finally:
                self._remove_partial(job.pdf_tmp)
                job.pdf_tmp = None
                job.work_path = (
                    None  # wskazywal na plik ZRODLOWY - nigdy go nie usuwamy
                )

    def _pure_source(self, gdal, job):
        if os.path.splitext(job.raster)[1].lower() == ".pdf":
            if self.work_dir is None:
                self._make_work_dir()
            job.pdf_tmp = self._rasterize_pdf(gdal, job.raster)
            return job.pdf_tmp
        return job.raster

    def _pure_one(self, gdal, job, ranges, band_count, cb):
        src_path = self._pure_source(gdal, job)
        if src_path != job.raster:
            job.notes.append(
                bi(
                    "PDF zrasteryzowany przy {} DPI (tylko PDF typu"
                    " rastrowego!)".format(self.s.pdf_dpi),
                    "PDF rasterized at {} DPI (raster-type PDF only!)".format(
                        self.s.pdf_dpi
                    ),
                )
            )
        src = gdal.Open(src_path)
        if src is None:
            raise RuntimeError(
                bi(
                    "Nie udalo sie otworzyc rastra",
                    "Could not open the raster",
                )
            )
        if (
            src.GetGeoTransform(can_return_null=True) is None
            and src.GetGCPCount() == 0
        ):
            job.notes.append(
                bi(
                    "Raster nie ma georeferencji - skopiowano bez niej",
                    "The raster has no georeferencing - copied without it",
                )
            )
        job.assigned_wkt = None
        if not src.GetProjection() and not src.GetGCPProjection():
            fb = self._fallback_srs()
            if (
                fb is not None
                and src.GetGeoTransform(can_return_null=True) is not None
            ):
                job.assigned_wkt = fb.ExportToWkt()
                job.notes.append(
                    bi(
                        "Raster bez ukladu - przyjeto uklad z ustawien: {}"
                        .format(_srs_label(fb)),
                        "Raster without CRS - the CRS from the settings was"
                        " assumed: {}".format(_srs_label(fb)),
                    )
                )
            else:
                job.notes.append(
                    bi(
                        "Raster nie ma ukladu wspolrzednych - wynik bez"
                        " ukladu",
                        "The raster has no CRS - output without CRS",
                    )
                )
        if job.band_count is None:
            self._inspect_bands(gdal, src, job)

        drv_out, can_create, can_copy = self._target_driver(
            gdal, src, job.out_path
        )
        if not can_create and not can_copy:
            job.notes.append(
                bi(
                    "Format {} nie pozwala na zapis - wynik zapisano jako"
                    " GeoTIFF".format(drv_out.ShortName),
                    "Format {} is read-only - the output was saved as GeoTIFF"
                    .format(drv_out.ShortName),
                )
            )
            job.out_path = os.path.splitext(job.out_path)[0] + ".tif"
            drv_out = gdal.GetDriverByName("GTiff")
        fmt = drv_out.ShortName
        job.out_driver = fmt
        job.mask_mode = False
        job.method = bi(
            "czysta konwersja (gdal_translate)",
            "pure conversion (gdal_translate)",
        )

        if self.s.force_8bit and not job.is_byte:
            # 8-bit dla plikow 16-bit / float: skalowanie wspolnym zakresem
            job.work_path = src_path  # tylko do odczytu!
            src = None
            self._scale_to_8bit(
                gdal, job, self._job_ranges(job, ranges, band_count), cb
            )
            job.method += " + 8-bit"
            return
        if self.s.force_8bit and job.is_byte:
            job.notes.append(
                bi(
                    "Plik jest juz 8-bit - wartosci bez zmian",
                    "The file is already 8-bit - values unchanged",
                )
            )

        supported = (
            drv_out.GetMetadataItem("DMD_CREATIONDATATYPES") or ""
        ).split()
        type_name = gdal.GetDataTypeName(src.GetRasterBand(1).DataType)
        if supported and type_name not in supported:
            raise SkipPair(
                ST_ERROR,
                bi(
                    "Format {} nie obsluguje danych typu {} (tylko: {}) -"
                    " zaznacz 'Wymus 8-bit' albo wybierz GeoTIFF".format(
                        fmt, type_name, ", ".join(supported)
                    ),
                    "Format {} does not support {} data (only: {}) - tick"
                    " 'Force 8-bit' or choose GeoTIFF".format(
                        fmt, type_name, ", ".join(supported)
                    ),
                ),
            )

        kwargs = {"format": fmt, "callback": cb}
        keep_alpha = False
        if detect_hidden_alpha(src):
            # nieoznaczona alfa (kanal 4 = 0/255) -> przezroczystosc, nie dane
            transp = TRANSPARENCY.get(fmt)
            if transp == "mask":
                kwargs["bandList"] = [1, 2, 3]
                kwargs["maskBand"] = 4
            elif transp == "alpha":
                keep_alpha = True
            else:
                kwargs["bandList"] = [1, 2, 3]
                job.notes.append(
                    bi(
                        "Format {} nie obsluguje przezroczystosci - kanal 4"
                        " (alfa) pominiety".format(fmt),
                        "Format {} does not support transparency - band 4"
                        " (alpha) dropped".format(fmt),
                    )
                )
            job.notes.append(
                bi(
                    "Kanal 4 rozpoznany jako przezroczystosc (alfa)",
                    "Band 4 recognised as transparency (alpha)",
                )
            )
        else:
            alpha = [
                b
                for b in range(1, src.RasterCount + 1)
                if b not in data_bands(src)
            ]
            keep_alpha = bool(alpha)
            if alpha and fmt in ("JPEG", "BMP"):
                kwargs["bandList"] = data_bands(src)
                if fmt == "JPEG":
                    kwargs["maskBand"] = alpha[0]
        if (
            fmt in NO_PALETTE
            and src.GetRasterBand(1).GetColorTable() is not None
            and "bandList" not in kwargs
        ):
            kwargs["rgbExpand"] = "rgb"
            job.notes.append(
                bi(
                    "Paleta barw rozwinieta do RGB (format {} nie obsluguje"
                    " palety)".format(fmt),
                    "Colour table expanded to RGB (format {} does not support"
                    " palettes)".format(fmt),
                )
            )
        kwargs["creationOptions"] = self._format_options(
            fmt, nbands_alpha=keep_alpha
        )
        src = None

        if os.path.exists(job.out_path):
            os.remove(job.out_path)
        old_cfg = gdal.GetConfigOption("GDAL_TIFF_INTERNAL_MASK")
        gdal.SetConfigOption("GDAL_TIFF_INTERNAL_MASK", "YES")
        try:
            out = gdal.Translate(job.out_path, src_path, **kwargs)
            if out is None:
                raise RuntimeError(
                    bi(
                        "gdal.Translate nie zwrocil wyniku",
                        "gdal.Translate returned no result",
                    )
                )
            out = None
        finally:
            gdal.SetConfigOption("GDAL_TIFF_INTERNAL_MASK", old_cfg)

    # -- 8-bit: wspolne pomocnicze --

    def _inspect_bands(self, gdal, ds, job):
        bands = data_bands(ds)
        job.band_count = len(bands)
        job.is_byte = bool(bands) and all(
            ds.GetRasterBand(b).DataType == gdal.GDT_Byte for b in bands
        )

    def _common_ranges(self, jobs):
        """Wspolny zakres per kanal - liczony tylko z plikow, ktore NIE sa
        juz 8-bit. Pliki 8-bit (Byte) nie sa rozciagane (kolory bez zmian).
        Zwraca (zadania_do_zapisu, zakresy_lub_None, wzorcowa_liczba_kanalow).
        """
        byte_jobs = [j for j in jobs if j.is_byte]
        stretch = [j for j in jobs if not j.is_byte]
        ranges = None
        band_count = None
        if stretch:
            stretch = self._drop_empty_clips(stretch)
            if stretch:
                band_count = self._reference_band_count(stretch)
                ranges = [[None, None] for _ in range(band_count)]
                for j in stretch:
                    if j.band_count == band_count:
                        _merge_ranges(ranges, j.ranges)
                if not self._ranges_ok(stretch, ranges):
                    stretch = []
        keep = {id(j) for j in byte_jobs + stretch}
        return [j for j in jobs if id(j) in keep], ranges, band_count

    def _job_ranges(self, job, ranges, band_count):
        """Zakresy dla jednego pliku: 8-bit -> 0-255 (bez zmian), inne ->
        wspolny zakres (o ile liczba kanalow sie zgadza)."""
        if job.is_byte:
            job.notes.append(
                bi(
                    "Plik jest juz 8-bit - wartosci bez rozciagania (kolory"
                    " bez zmian)",
                    "The file is already 8-bit - values not stretched (colours"
                    " unchanged)",
                )
            )
            return [(0.0, 255.0)] * job.band_count
        self._check_band_count(job, band_count)
        return ranges

    def _reference_band_count(self, jobs):
        """Wzorcowa liczba kanalow = najczestsza wsrod plikow."""
        counts = {}
        for job in jobs:
            counts[job.band_count] = counts.get(job.band_count, 0) + 1
        return max(sorted(counts), key=lambda k: counts[k])

    def _check_band_count(self, job, band_count):
        if job.band_count != band_count:
            raise SkipPair(
                ST_ERROR,
                bi(
                    "Raster ma {} kanalow, a pozostale {} - nie mozna"
                    " zastosowac wspolnego zakresu 8-bit".format(
                        job.band_count, band_count
                    ),
                    "The raster has {} bands, the others {} - the common 8-bit"
                    " range cannot be applied".format(
                        job.band_count, band_count
                    ),
                ),
            )

    def _drop_empty_clips(self, jobs):
        """Arkusz, ktorego wycinek nie ma ANI JEDNEGO waznego piksela, jest
        pomijany osobno (status 'pusty wycinek') - nie psuje calego
        przebiegu. Zwraca liste pozostalych zadan."""
        kept = []
        for job in jobs:
            if job.ranges is not None and all(
                r[0] is None for r in job.ranges
            ):
                self._fail(
                    job,
                    ST_EMPTY,
                    bi(
                        "Wycinek nie zawiera zadnego waznego piksela"
                        " (wszystkie = NoData)",
                        "The clip contains no valid pixel (all = NoData)",
                    ),
                )
                self._remove_partial(job.work_path)
            else:
                kept.append(job)
        return kept

    def _ranges_ok(self, jobs, ranges):
        if all(r[0] is None for r in ranges):
            for job in jobs:
                self._fail(
                    job,
                    ST_ERROR,
                    bi(
                        "Nie udalo sie wyliczyc zakresu wartosci do 8-bit"
                        " (brak waznych pikseli wewnatrz arkuszy)",
                        "Could not compute the 8-bit value range (no valid"
                        " pixels inside the sheets)",
                    ),
                )
                self._remove_partial(job.work_path)
            return False
        # Pojedynczy kanal bez waznych pikseli (np. caly = NoData) nie
        # blokuje przebiegu - dostaje zakres 0-255 (bez rozciagania).
        for i, r in enumerate(ranges):
            if r[0] is None:
                r[0], r[1] = 0.0, 255.0
                self.result.warnings.append(
                    bi(
                        "Kanal {} nie ma waznych pikseli w zadnym arkuszu -"
                        " przyjeto zakres 0-255".format(i + 1),
                        "Band {} has no valid pixels in any sheet - range"
                        " 0-255 used".format(i + 1),
                    )
                )
        desc = ", ".join(
            "{}: {:g} - {:g}".format(i + 1, r[0], r[1])
            for i, r in enumerate(ranges)
        )
        self.result.warnings.append(
            bi(
                "Wspolny zakres wartosci (wewnatrz arkuszy) do 8-bit,"
                " kanaly {}".format(desc),
                "Common value range (inside sheets) for 8-bit, bands {}"
                .format(desc),
            )
        )
        return True

    # -- kroki dla jednej pary --

    def _output_path(self, raster):
        name = os.path.basename(raster)
        base, ext = os.path.splitext(name)
        entry = _format_entry(self.s.output_format)
        if entry[0] != FORMAT_ORIGINAL:
            ext = entry[3]
        elif ext.lower() == ".pdf":
            ext = ".tif"
        return os.path.join(
            self.s.output_dir, base + self.s.output_suffix + ext
        )

    def _fail(self, job, status, message):
        job.done = True
        self.result.rows.append(
            ReportRow(
                os.path.basename(job.raster),
                os.path.basename(job.sheet),
                status,
                job.notes + [message],
            )
        )

    def _remove_partial(self, path):
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    def _fallback_srs(self):
        """Uklad z ustawien (dla plikow bez ukladu) albo None."""
        from osgeo import osr

        if not self.s.default_crs:
            return None
        if not hasattr(self, "_fallback_cache"):
            srs = osr.SpatialReference()
            try:
                srs.SetFromUserInput(self.s.default_crs)
                srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                self._fallback_cache = srs
            except Exception:  # noqa: BLE001
                self._fallback_cache = None
                self.result.warnings.append(
                    bi(
                        "Nieprawidlowy uklad w ustawieniach: {} - pomijam"
                        .format(self.s.default_crs),
                        "Invalid CRS in the settings: {} - ignored".format(
                            self.s.default_crs
                        ),
                    )
                )
        return self._fallback_cache

    def _assign_output_crs(self, gdal, job):
        """Dla rastra bez ukladu: zapis przyjetego ukladu w pliku wynikowym
        (GeoTIFF - w pliku, inne formaty - w .aux.xml). Wspolrzedne i piksele
        bez zmian."""
        if not job.assigned_wkt or not os.path.exists(job.out_path):
            return
        ds = gdal.Open(job.out_path)
        has = bool(ds.GetProjection()) if ds is not None else True
        ds = None
        if has:
            return
        try:
            ds = gdal.Open(job.out_path, gdal.GA_Update)
        except Exception:  # noqa: BLE001
            ds = gdal.Open(job.out_path)  # tylko .aux.xml (PAM)
        if ds is not None:
            ds.SetProjection(job.assigned_wkt)
            ds.FlushCache()
            ds = None

    def _finish_job(self, gdal, job):
        self._assign_output_crs(gdal, job)
        wext = WORLDFILE_EXT.get(os.path.splitext(job.out_path)[1].lower())
        if wext:
            try:
                write_worldfile(gdal, job.out_path, [wext])
            except Exception as exc:  # noqa: BLE001
                job.notes.append(
                    bi(
                        "Nie udalo sie zapisac pliku georeferencyjnego ({})"
                        .format(exc),
                        "Could not write the world file ({})".format(exc),
                    )
                )
        job.done = True
        self.result.rows.append(
            ReportRow(
                os.path.basename(job.raster),
                os.path.basename(job.sheet),
                ST_OK,
                job.notes,
                method=job.method,
            )
        )

    def _rasterize_pdf(self, gdal, pdf_path):
        src = gdal.OpenEx(
            pdf_path,
            gdal.OF_RASTER,
            open_options=["DPI={}".format(self.s.pdf_dpi)],
        )
        if src is None:
            raise RuntimeError(
                bi(
                    "Nie udalo sie otworzyc PDF jako rastra (brak"
                    " georeferencji albo brak sterownika PDF w GDAL)",
                    "Could not open the PDF as a raster (no georeferencing or"
                    " no PDF driver in GDAL)",
                )
            )
        tmp = os.path.join(
            self.work_dir,
            os.path.splitext(os.path.basename(pdf_path))[0]
            + "_zrasteryzowany.tif",
        )
        out = gdal.Translate(
            tmp, src, format="GTiff", creationOptions=["BIGTIFF=IF_SAFER"]
        )
        src = None
        if out is None:
            raise RuntimeError(
                bi(
                    "Nie udalo sie zrasteryzowac pliku PDF",
                    "Could not rasterize the PDF file",
                )
            )
        out = None
        return tmp

    def _target_driver(self, gdal, src_ds, out_path):
        """Sterownik dla pliku wyjsciowego + informacja, czy obsluguje
        Create (zapis bezposredni z gdal.Warp) / CreateCopy."""
        entry = _format_entry(self.s.output_format)
        ext = os.path.splitext(out_path)[1].lower()
        if entry[2] is not None:
            drv = gdal.GetDriverByName(entry[2])
        elif ext in (".tif", ".tiff"):
            drv = gdal.GetDriverByName("GTiff")
        else:
            drv = src_ds.GetDriver()
        md = drv.GetMetadata() or {}
        can_create = md.get("DCAP_CREATE") == "YES"
        can_copy = md.get("DCAP_CREATECOPY") == "YES"
        return drv, can_create, can_copy

    def _format_options(self, fmt, nbands_alpha=False):
        """Opcje tworzenia pliku dla danego sterownika."""
        if fmt == "GTiff":
            return self._gtiff_options(True)
        if fmt == "COG":
            opts = ["BIGTIFF=IF_SAFER"]
            if self.s.compression and self.s.compression != "NONE":
                opts.append("COMPRESS={}".format(self.s.compression))
            return opts
        opts = []
        for entry in OUTPUT_FORMATS:
            if entry[2] == fmt:
                opts = list(entry[4])
                break
        if fmt == "JP2OpenJPEG" and nbands_alpha:
            opts.append("ALPHA=YES")
        return opts

    def _gtiff_options(self, final):
        opts = ["BIGTIFF=IF_SAFER"]
        if final and self.s.compression and self.s.compression != "NONE":
            opts.append("COMPRESS={}".format(self.s.compression))
        return opts

    def _clip(self, gdal, job, final, cb, virtual=False):
        """Wycina raster po poligonie arkusza. Zwraca sciezke wyniku:
        - final=True: job.out_path (jesli format pozwala na zapis
          bezposredni) albo roboczy GTiff do pozniejszej konwersji,
        - final=False: roboczy GTiff (do skalowania 8-bit),
        - virtual=True: wycinek wirtualny .vrt (bez zapisu pikseli na
          dysk) - zrasteryzowany PDF zostaje wtedy w job.pdf_tmp do
          usuniecia przez wywolujacego."""
        from osgeo import ogr, osr

        if job.notes_after_pairing is None:
            job.notes_after_pairing = len(job.notes)

        name = os.path.splitext(os.path.basename(job.raster))[0]
        src_path = job.raster
        if os.path.splitext(src_path)[1].lower() == ".pdf":
            src_path = self._rasterize_pdf(gdal, src_path)
            job.pdf_tmp = src_path
            job.notes.append(
                bi(
                    "PDF zrasteryzowany przy {} DPI (tylko PDF typu"
                    " rastrowego!)".format(self.s.pdf_dpi),
                    "PDF rasterized at {} DPI (raster-type PDF only!)".format(
                        self.s.pdf_dpi
                    ),
                )
            )

        cut_path = "/vsimem/cutline_{}_{}.gpkg".format(os.getpid(), id(job))
        src_ds = None
        try:
            src_ds = gdal.Open(src_path)
            if src_ds is None:
                raise RuntimeError(
                    bi(
                        "Nie udalo sie otworzyc rastra",
                        "Could not open the raster",
                    )
                )

            gt = src_ds.GetGeoTransform(can_return_null=True)
            if gt is None:
                raise SkipPair(
                    ST_ERROR,
                    bi(
                        "Raster nie ma georeferencji (brak geotransformacji i"
                        " pliku .jgw/.tfw/.wld)",
                        "The raster has no georeferencing (no geotransform and"
                        " no .jgw/.tfw/.wld file)",
                    ),
                )

            wkt = src_ds.GetProjection()
            raster_srs = None
            if wkt:
                raster_srs = osr.SpatialReference()
                raster_srs.ImportFromWkt(wkt)
            job.assigned_wkt = None

            # Zasieg rastra: rzeczywisty obrys z 4 naroznikow (dla rastrow
            # obroconych prostokat z 2 naroznikow bylby za duzy lub
            # przesuniety).
            rotated = abs(gt[2]) > 0 or abs(gt[4]) > 0
            W, H = src_ds.RasterXSize, src_ds.RasterYSize
            corners = [
                (gt[0] + c * gt[1] + r * gt[2], gt[3] + c * gt[4] + r * gt[5])
                for c, r in ((0, 0), (W, 0), (W, H), (0, H), (0, 0))
            ]
            raster_poly = ogr.CreateGeometryFromWkt(
                "POLYGON(("
                + ",".join("{!r} {!r}".format(x, y) for x, y in corners)
                + "))"
            )

            sheet_srs = None
            if self.s.convert_only:
                # Tylko konwersja: "arkuszem" jest caly raster
                geom = raster_poly.Clone()
            else:
                geom, sheet_srs, notes = read_sheet_polygon(job.sheet, name)
                job.notes.extend(notes)

            if raster_srs is None:
                # Raster bez ukladu (np. JPG z samym .jgw): 1) uklad arkusza,
                # 2) uklad z ustawien, 3) wycinanie w samych wspolrzednych.
                if sheet_srs is not None:
                    raster_srs = sheet_srs.Clone()
                    job.notes.append(
                        bi(
                            "Raster bez ukladu - przyjeto uklad arkusza: {}"
                            .format(_srs_label(raster_srs)),
                            "Raster without CRS - the sheet CRS was"
                            " assumed: {}".format(_srs_label(raster_srs)),
                        )
                    )
                elif self._fallback_srs() is not None:
                    raster_srs = self._fallback_srs().Clone()
                    job.notes.append(
                        bi(
                            "Raster{} bez ukladu - przyjeto uklad z"
                            " ustawien: {}".format(
                                "" if self.s.convert_only else " i arkusz",
                                _srs_label(raster_srs),
                            ),
                            "Raster{} without CRS - the CRS from the settings"
                            " was assumed: {}".format(
                                "" if self.s.convert_only else " and sheet",
                                _srs_label(raster_srs),
                            ),
                        )
                    )
                else:
                    job.notes.append(
                        bi(
                            "Brak ukladu{} - wycieto w samych wspolrzednych,"
                            " wynik bez ukladu (tylko plik georeferencyjny)"
                            .format(
                                ""
                                if self.s.convert_only
                                else " w rastrze i arkuszu"
                            ),
                            "No CRS{} - processed in plain coordinates, output"
                            " without CRS (world file only)".format(
                                ""
                                if self.s.convert_only
                                else " in the raster and the sheet"
                            ),
                        )
                    )
                if raster_srs is not None:
                    job.assigned_wkt = raster_srs.ExportToWkt()
                    if sheet_srs is None:
                        # oba bez ukladu - ten sam przyjety uklad
                        sheet_srs = raster_srs

            if not self.s.convert_only:
                if raster_srs is not None:
                    geom, notes = prepare_cutline(
                        geom, sheet_srs, raster_srs, self.s.reproject_sheets
                    )
                    job.notes.extend(notes)

                if not geom.IsValid():
                    fixed = (
                        geom.MakeValid()
                        if hasattr(geom, "MakeValid")
                        else geom.Buffer(0)
                    )
                    if fixed is not None and not fixed.IsEmpty():
                        geom = fixed
                        job.notes.append(
                            bi(
                                "Geometria arkusza byla niepoprawna -"
                                " naprawiono (MakeValid)",
                                "The sheet geometry was invalid - repaired"
                                " (MakeValid)",
                            )
                        )

            # Przy konwersji rastra nieobroconego nie ma obszaru "poza" -
            # linia ciecia jest zbedna (kopia 1:1 calego rastra).
            use_cutline = not (self.s.convert_only and not rotated)

            if not geom.Intersects(raster_poly):
                raise SkipPair(
                    ST_OUTSIDE,
                    bi(
                        "Poligon arkusza nie pokrywa sie z zasiegiem rastra",
                        "The sheet polygon does not overlap the raster extent",
                    ),
                )
            if not self.s.convert_only and not raster_poly.Contains(geom):
                job.notes.append(
                    bi(
                        "Arkusz wychodzi poza zasieg rastra - brakujaca czesc"
                        " bedzie NoData",
                        "The sheet extends beyond the raster - the missing"
                        " part will be NoData",
                    )
                )

            # Rozdzielczosc wyjsciowa
            res_x = abs(gt[1]) if not rotated else math.hypot(gt[1], gt[4])
            res_y = abs(gt[5]) if not rotated else math.hypot(gt[2], gt[5])
            if self.s.input_dpi and self.s.output_dpi:
                factor = self.s.input_dpi / self.s.output_dpi
                res_x *= factor
                res_y *= factor

            env = geom.GetEnvelope()  # minx, maxx, miny, maxy
            bounds = (env[0], env[2], env[1], env[3])
            if rotated:
                job.notes.append(
                    bi(
                        "Raster jest obrocony - wynik bedzie w siatce"
                        " polnoc-gora (resampling nieunikniony)",
                        "The raster is rotated - the output will be north-up"
                        " (resampling unavoidable)",
                    )
                )
            else:
                bounds = snap_bounds(gt, bounds, res_x, res_y)

            # Metoda resamplingu
            if self.s.resampling_mode == "auto":
                method = suggest_resampling(src_ds)
            else:
                method = self.s.resampling_mode
            label = dict(RESAMPLING_METHODS)[method]
            job.method = "{} ({})".format(
                label,
                (
                    "auto"
                    if self.s.resampling_mode == "auto"
                    else bi("wymuszona", "forced")
                ),
            )

            # Linia ciecia zapisana w ukladzie rastra (plik w pamieci). Bez
            # ukladu - shapefile bez .prj (GPKG dopisalby "nieokreslony uklad",
            # co GDAL zglasza jako niezgodnosc z rastrem bez ukladu).
            if raster_srs is None:
                cut_path = cut_path[:-5] + ".shp"
                drv = ogr.GetDriverByName("ESRI Shapefile")
            else:
                drv = ogr.GetDriverByName("GPKG")
            cut_ds = drv.CreateDataSource(cut_path)
            cut_layer = cut_ds.CreateLayer(
                "cutline", srs=raster_srs, geom_type=ogr.wkbMultiPolygon
            )
            if job.assigned_wkt:
                # uklad przyjety dla rastra bez ukladu - zapisany w wyniku
                warp_kwargs_crs = {
                    "srcSRS": job.assigned_wkt,
                    "dstSRS": job.assigned_wkt,
                }
            else:
                warp_kwargs_crs = {}
            feat = ogr.Feature(cut_layer.GetLayerDefn())
            feat.SetGeometry(ogr.ForceToMultiPolygon(geom))
            cut_layer.CreateFeature(feat)
            feat = None
            cut_ds = None

            # NoData / alfa
            dtype = src_ds.GetRasterBand(1).DataType
            src_nodata = src_ds.GetRasterBand(1).GetNoDataValue()
            warp_kwargs = {}
            hidden_alpha = detect_hidden_alpha(src_ds)
            if hidden_alpha:
                # 4. kanal = nieoznaczona alfa: uzywana jako przezroczystosc
                # zrodla (piksele z alfa=0 -> poza trescia), a nie jako dane.
                # W trybie NoData wynik ma 3 kanaly RGB, w trybie alfa - RGB
                # + poprawnie oznaczony kanal alfa.
                warp_kwargs["srcAlpha"] = True
                job.notes.append(
                    bi(
                        "Kanal 4 rozpoznany jako przezroczystosc (alfa) - nie"
                        " jest traktowany jako dane",
                        "Band 4 recognised as transparency (alpha) - not"
                        " treated as data",
                    )
                )
            job.mask_mode = False
            if self.s.outside_mode == OUTSIDE_ALPHA:
                warp_kwargs["dstAlpha"] = True
                if src_nodata is not None:
                    warp_kwargs["dstNodata"] = src_nodata
            elif self.s.manual_nodata is not None or src_nodata is not None:
                # NoData jako WARTOSC: reczna albo z pliku zrodlowego
                # (typowo NMT/DEM - tam wartosc NoData nie koliduje z trescia)
                warp_kwargs["dstNodata"] = (
                    self.s.manual_nodata
                    if self.s.manual_nodata is not None
                    else src_nodata
                )
            elif use_cutline or hidden_alpha:
                # Plik bez wlasnej wartosci NoData (typowo skan mapy RGB):
                # tlo biale/czarne + MASKA. Obszar poza arkuszem oznaczany
                # jest maska wewnetrzna, a nie wartoscia 255/0 - dzieki temu
                # biale tlo papieru czy zielony kanal lasow (G=255) WEWNATRZ
                # arkusza nie staja sie NoData.
                warp_kwargs["dstAlpha"] = True
                job.mask_mode = True
                job.fill = (
                    (0 if self.s.default_nodata_black else 255)
                    if dtype == gdal.GDT_Byte
                    else 0
                )

            # Format docelowy
            drv_out, can_create, can_copy = self._target_driver(
                gdal, src_ds, job.out_path
            )
            if not can_create and not can_copy:
                # format tylko do odczytu (np. ECW/MrSID) -> GeoTIFF
                if not virtual:
                    job.notes.append(
                        bi(
                            "Format {} nie pozwala na zapis - wynik zapisano"
                            " jako GeoTIFF".format(drv_out.ShortName),
                            "Format {} is read-only - the output was saved as"
                            " GeoTIFF".format(drv_out.ShortName),
                        )
                    )
                job.out_path = os.path.splitext(job.out_path)[0] + ".tif"
                drv_out = gdal.GetDriverByName("GTiff")
                can_create = True
            job.out_driver = drv_out.ShortName
            if final:
                # (przy 8-bit wynik i tak jest typu Byte - sprawdzenie zbedne)
                supported = (
                    drv_out.GetMetadataItem("DMD_CREATIONDATATYPES") or ""
                ).split()
                type_name = gdal.GetDataTypeName(dtype)
                if supported and type_name not in supported:
                    raise SkipPair(
                        ST_ERROR,
                        bi(
                            "Format {} nie obsluguje danych typu {} (tylko:"
                            " {}) - zaznacz 'Wymus 8-bit' albo wybierz GeoTIFF"
                            .format(
                                drv_out.ShortName,
                                type_name,
                                ", ".join(supported),
                            ),
                            "Format {} does not support {} data (only: {}) -"
                            " tick 'Force 8-bit' or choose GeoTIFF".format(
                                drv_out.ShortName,
                                type_name,
                                ", ".join(supported),
                            ),
                        ),
                    )
            # Zapis bezposredni z gdal.Warp tylko wtedy, gdy format sam
            # przyjmie wynik (bez maski; kanal alfa - tylko GeoTIFF).
            direct = (
                final
                and can_create
                and not job.mask_mode
                and (
                    not warp_kwargs.get("dstAlpha")
                    or drv_out.ShortName == "GTiff"
                )
            )

            if virtual:
                target = os.path.join(self.work_dir, name + "_wirtualny.vrt")
                fmt = "VRT"
                copts = []
            elif final and direct:
                target = job.out_path
                fmt = drv_out.ShortName
                copts = self._format_options(fmt)
            else:
                target = os.path.join(self.work_dir, name + "_wyciety.tif")
                fmt = "GTiff"
                copts = self._gtiff_options(False)

            if os.path.exists(target):
                drv_del = gdal.IdentifyDriver(target)
                if drv_del is not None:
                    drv_del.Delete(target)
                else:
                    os.remove(target)

            options = gdal.WarpOptions(
                format=fmt,
                outputBounds=bounds,
                xRes=res_x,
                yRes=res_y,
                cutlineDSName=cut_path if use_cutline else None,
                cutlineLayer=(
                    "cutline"
                    if use_cutline and cut_path.endswith(".gpkg")
                    else None
                ),
                cropToCutline=False,
                resampleAlg=method,
                creationOptions=copts,
                multithread=True,
                warpOptions=["NUM_THREADS=ALL_CPUS"],
                callback=cb,
                **warp_kwargs,
                **warp_kwargs_crs
            )
            out = gdal.Warp(target, src_ds, options=options)
            if out is None:
                raise RuntimeError(
                    bi(
                        "gdal.Warp nie zwrocil wyniku",
                        "gdal.Warp returned no result",
                    )
                )
            out = None
        finally:
            src_ds = None
            try:
                if cut_path.endswith(".shp"):
                    for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
                        try:
                            gdal.Unlink(cut_path[:-4] + ext)
                        except Exception as exc:  # noqa: BLE001
                            _LOG.debug(
                                "Nie usunieto %s: %s", cut_path[:-4] + ext, exc
                            )
                else:
                    gdal.Unlink(cut_path)
            except Exception as exc:  # noqa: BLE001
                _LOG.debug("Nie usunieto %s: %s", cut_path, exc)
            if job.pdf_tmp and not virtual:
                # zrasteryzowany PDF juz niepotrzebny - usuwany od razu
                self._remove_partial(job.pdf_tmp)
                job.pdf_tmp = None

        return target

    def _copy_to_format(self, gdal, job, work_path):
        """Zapis roboczego GTiff w formacie docelowym (JPG, PNG, JP2, COG...,
        ktorych gdal.Warp nie zapisze bezposrednio). Przezroczystosc poza
        arkuszem zapisywana jest tak, jak pozwala format: maska wewnetrzna
        (GeoTIFF, COG, JPEG), kanal alfa (PNG, WebP, JPEG 2000) albo - gdy
        format jej nie obsluguje (BMP, IMG) - zostaje samo tlo biale/czarne.
        Roboczy plik jest usuwany zaraz po zapisie."""
        fmt = job.out_driver
        ds = gdal.Open(work_path)
        bands = data_bands(ds)
        alpha = [b for b in range(1, ds.RasterCount + 1) if b not in bands]
        ds = None

        kwargs = {"format": fmt}
        keep_alpha = False
        if alpha:
            transp = TRANSPARENCY.get(fmt)
            if job.mask_mode:
                self._fill_outside(gdal, work_path, job.fill)
                if transp == "mask":
                    kwargs["bandList"] = bands
                    kwargs["maskBand"] = alpha[0]
                elif transp == "alpha":
                    keep_alpha = True
                    job.notes.append(
                        bi(
                            "Format {} nie obsluguje maski - poza arkuszem"
                            " zapisano kanal alfa".format(fmt),
                            "Format {} does not support masks - an alpha band"
                            " was written outside the sheet".format(fmt),
                        )
                    )
                else:
                    kwargs["bandList"] = bands
                    job.notes.append(
                        bi(
                            "Format {} nie obsluguje przezroczystosci - poza"
                            " arkuszem samo tlo".format(fmt),
                            "Format {} does not support transparency - plain"
                            " background outside the sheet".format(fmt),
                        )
                    )
            else:  # tryb "kanal alfa"
                if fmt in ("GTiff", "COG") or transp == "alpha":
                    keep_alpha = True
                elif transp == "mask":
                    kwargs["bandList"] = bands
                    kwargs["maskBand"] = alpha[0]
                    job.notes.append(
                        bi(
                            "Format {} nie ma kanalu alfa - przezroczystosc"
                            " zapisana jako maska".format(fmt),
                            "Format {} has no alpha band - transparency saved"
                            " as a mask".format(fmt),
                        )
                    )
                else:
                    kwargs["bandList"] = bands
                    job.notes.append(
                        bi(
                            "Format {} nie obsluguje przezroczystosci - kanal"
                            " alfa pominiety".format(fmt),
                            "Format {} does not support transparency - alpha"
                            " band dropped".format(fmt),
                        )
                    )
        if fmt in NO_PALETTE:
            ds = gdal.Open(work_path)
            has_ct = ds.GetRasterBand(1).GetColorTable() is not None
            ds = None
            if has_ct:
                kwargs["rgbExpand"] = "rgb"
                if "bandList" not in kwargs:
                    kwargs["bandList"] = [1]
                job.notes.append(
                    bi(
                        "Paleta barw rozwinieta do RGB (format {} nie"
                        " obsluguje palety)".format(fmt),
                        "Colour table expanded to RGB (format {} does not"
                        " support palettes)".format(fmt),
                    )
                )
        kwargs["creationOptions"] = self._format_options(
            fmt, nbands_alpha=keep_alpha
        )

        old_cfg = gdal.GetConfigOption("GDAL_TIFF_INTERNAL_MASK")
        gdal.SetConfigOption("GDAL_TIFF_INTERNAL_MASK", "YES")
        try:
            if os.path.exists(job.out_path):
                os.remove(job.out_path)
            out = gdal.Translate(job.out_path, work_path, **kwargs)
            if out is None:
                raise RuntimeError(
                    bi(
                        "gdal.Translate nie zwrocil wyniku",
                        "gdal.Translate returned no result",
                    )
                )
            out = None
        finally:
            gdal.SetConfigOption("GDAL_TIFF_INTERNAL_MASK", old_cfg)
            self._remove_partial(work_path)

    def _fill_outside(self, gdal, path, fill):
        """W roboczym pliku z kanalem alfa wpisuje wartosc tla (biel/czern)
        w kanaly danych tam, gdzie alfa = 0 (poza arkuszem)."""
        ds = gdal.Open(path, gdal.GA_Update)
        try:
            dbands = data_bands(ds)
            alpha = [
                b for b in range(1, ds.RasterCount + 1) if b not in dbands
            ]
            if not alpha:
                return
            a_band = ds.GetRasterBand(alpha[0])
            xsize = ds.RasterXSize
            for y, n in _iter_row_blocks(ds.RasterYSize):
                self._check_cancel()
                outside = a_band.ReadAsArray(0, y, xsize, n) == 0
                if not outside.any():
                    continue
                for b in dbands:
                    band = ds.GetRasterBand(b)
                    arr = band.ReadAsArray(0, y, xsize, n)
                    arr[outside] = fill
                    band.WriteArray(arr, 0, y)
            ds.FlushCache()
        finally:
            ds = None

    def _scale_to_8bit(self, gdal, job, ranges, cb):
        """Skalowanie do 8-bit. Wynik od razu jako GeoTIFF, a dla innych
        formatow - przez roboczy GeoTIFF (usuwany zaraz po konwersji)."""
        src = gdal.Open(job.work_path)
        dbands = data_bands(src)
        alpha = [b for b in range(1, src.RasterCount + 1) if b not in dbands]
        src = None

        final_gtiff = job.out_driver == "GTiff"
        if final_gtiff:
            dest = job.out_path
        else:
            dest = os.path.join(
                self.work_dir,
                os.path.splitext(os.path.basename(job.raster))[0]
                + "_8bit.tif",
            )

        if (
            job.mask_mode
            and alpha
            and TRANSPARENCY.get(job.out_driver) == "mask"
        ):
            self._scale_8bit_mask(
                gdal, job, ranges, cb, dbands, alpha[0], dest, final_gtiff
            )
        else:
            self._scale_8bit_plain(
                gdal, job, ranges, cb, dbands, alpha, dest, final_gtiff
            )

        if not final_gtiff:
            self._copy_to_format(gdal, job, dest)

    def _scale_8bit_plain(
        self, gdal, job, ranges, cb, dbands, alpha, dest, final_gtiff
    ):
        scale_params = []
        bandlist = []
        for i, b in enumerate(dbands):
            lo, hi = ranges[i]
            if hi <= lo:
                hi = lo + 1
            scale_params.append([lo, hi, 0, 255])
            bandlist.append(b)
        for a in alpha:
            scale_params.append([0, 255, 0, 255])
            bandlist.append(a)

        copts = self._gtiff_options(final_gtiff)
        if alpha:
            copts.append("ALPHA=YES")
            if len(dbands) == 3:
                copts.append("PHOTOMETRIC=RGB")
        out = gdal.Translate(
            dest,
            job.work_path,
            format="GTiff",
            outputType=gdal.GDT_Byte,
            bandList=bandlist,
            scaleParams=scale_params,
            creationOptions=copts,
            noData="none",
            callback=cb,
        )
        if out is None:
            raise RuntimeError(
                bi(
                    "gdal.Translate (8-bit) nie zwrocil wyniku",
                    "gdal.Translate (8-bit) returned no result",
                )
            )
        out = None

        if alpha:
            return

        # Piksele NoData (poza arkuszem i NoData zrodla) -> wartosc tla
        # 255/0, oznaczona jako NoData w pliku wynikowym. Maska liczona z
        # pliku roboczego (przed skalowaniem), blokami.
        sentinel = 0 if self.s.default_nodata_black else 255
        src = gdal.Open(job.work_path)
        dst = gdal.Open(dest, gdal.GA_Update)
        try:
            mask_band = src.GetRasterBand(1).GetMaskBand()
            xsize = src.RasterXSize
            any_masked = False
            for y, n in _iter_row_blocks(src.RasterYSize):
                self._check_cancel()
                mask = mask_band.ReadAsArray(0, y, xsize, n) == 0
                if not mask.any():
                    continue
                any_masked = True
                for b in range(1, dst.RasterCount + 1):
                    band = dst.GetRasterBand(b)
                    arr = band.ReadAsArray(0, y, xsize, n)
                    arr[mask] = sentinel
                    band.WriteArray(arr, 0, y)
            if any_masked or src.GetRasterBand(1).GetNoDataValue() is not None:
                for b in range(1, dst.RasterCount + 1):
                    dst.GetRasterBand(b).SetNoDataValue(sentinel)
            dst.FlushCache()
        finally:
            src = None
            dst = None

    def _scale_8bit_mask(
        self, gdal, job, ranges, cb, dbands, alpha_band, dest, final_gtiff
    ):
        """8-bit w trybie 'tlo + maska': kanaly danych skalowane, kanal alfa
        zamieniany na maske wewnetrzna GeoTIFF, poza arkuszem tlo 255/0."""
        scale_params = []
        for i, _b in enumerate(dbands):
            lo, hi = ranges[i]
            if hi <= lo:
                hi = lo + 1
            scale_params.append([lo, hi, 0, 255])
        old_cfg = gdal.GetConfigOption("GDAL_TIFF_INTERNAL_MASK")
        gdal.SetConfigOption("GDAL_TIFF_INTERNAL_MASK", "YES")
        try:
            out = gdal.Translate(
                dest,
                job.work_path,
                format="GTiff",
                outputType=gdal.GDT_Byte,
                bandList=dbands,
                scaleParams=scale_params,
                maskBand=alpha_band,
                creationOptions=self._gtiff_options(final_gtiff),
                noData="none",
                callback=cb,
            )
            if out is None:
                raise RuntimeError(
                    bi(
                        "gdal.Translate (8-bit) nie zwrocil wyniku",
                        "gdal.Translate (8-bit) returned no result",
                    )
                )
            out = None
        finally:
            gdal.SetConfigOption("GDAL_TIFF_INTERNAL_MASK", old_cfg)

        # tlo poza arkuszem (po skalowaniu) = biel/czern
        sentinel = 0 if self.s.default_nodata_black else 255
        src = gdal.Open(job.work_path)
        dst = gdal.Open(dest, gdal.GA_Update)
        try:
            a_band = src.GetRasterBand(alpha_band)
            xsize = src.RasterXSize
            for y, n in _iter_row_blocks(src.RasterYSize):
                self._check_cancel()
                outside = a_band.ReadAsArray(0, y, xsize, n) == 0
                if not outside.any():
                    continue
                for b in range(1, dst.RasterCount + 1):
                    band = dst.GetRasterBand(b)
                    arr = band.ReadAsArray(0, y, xsize, n)
                    arr[outside] = sentinel
                    band.WriteArray(arr, 0, y)
            dst.FlushCache()
        finally:
            src = None
            dst = None

    # -- raport CSV --

    def _write_report(self):
        if not self.result.rows or not os.path.isdir(self.s.output_dir):
            return
        stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
        path = os.path.join(
            self.s.output_dir, "raport_wycinania_{}.csv".format(stamp)
        )
        n = 1
        while os.path.exists(path):
            n += 1
            path = os.path.join(
                self.s.output_dir,
                "raport_wycinania_{}_{}.csv".format(stamp, n),
            )
        order = {ST_OK: 1}
        rows = sorted(
            self.result.rows,
            key=lambda r: (
                order.get(r.status, 0),
                (r.raster or r.sheet).casefold(),
            ),
        )
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(CSV_HEADER)
                for r in rows:
                    w.writerow(
                        [r.raster, r.sheet, r.status, " | ".join(r.notes)]
                    )
            self.result.report_path = path
        except OSError as exc:
            self.result.warnings.append(
                bi(
                    "Nie udalo sie zapisac raportu CSV ({})".format(exc),
                    "Could not write the CSV report ({})".format(exc),
                )
            )


class _Job:
    def __init__(self, raster, sheet, notes):
        self.raster = raster
        self.sheet = sheet
        self.notes = list(notes)
        self.notes_after_pairing = None  # do odrzucenia uwag z przebiegu VRT
        self.out_path = None
        self.out_driver = None
        self.work_path = None
        self.pdf_tmp = None
        self.method = ""
        self.done = False
        self.band_count = None
        self.ranges = None
        self.mask_mode = False
        self.fill = 255
        self.is_byte = False
        self.assigned_wkt = None  # uklad przyjety dla rastra bez ukladu
