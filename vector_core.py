# -*- coding: utf-8 -*-
"""
Silnik konwersji i wycinania danych WEKTOROWYCH - czysty GDAL/OGR (bez QGIS).
Vector conversion and clipping engine (pure GDAL/OGR).

Zasady / rules:
  * BEZ PRZELICZEN: wspolrzedne i uklad wspolrzednych danych nie sa nigdy
    zmieniane (nie ma transformacji danych). Jesli zaznaczono "Przeliczaj
    z innego ukladu", przeliczany jest WYLACZNIE poligon wycinajacy.
  * zrodla: wszystko, co czyta QGIS/OGR - SHP, GPKG, geobaza ESRI (.gdb),
    GML, GeoJSON, KML, DXF, TAB/MIF, CSV, FlatGeobuf, SQLite, GPX,
  * wycinanie: kazda warstwa poligonowa w folderze poligonow to jeden
    obszar; wyniki obszaru trafiaja do podfolderu o nazwie tej warstwy,
  * tryby wycinania: przyciecie geometrii na granicy albo wybor obiektow
    przecinajacych obszar (w calosci, bez ciecia),
  * zrodla wielowarstwowe: do GPKG / GDB / SQLite - jeden plik na
    zrodlo ze wszystkimi warstwami; do formatow jednowarstwowych (SHP,
    TAB, GeoJSON, GML, FlatGeobuf, CSV, DXF) - plik na warstwe, opcjonalnie
    w podfolderze zrodla.
"""

import csv
import datetime
import os
import logging

import re

from .clip_core import bi

_LOG = logging.getLogger(__name__)

# Rozszerzenia plikow wektorowych wykrywanych w folderze zrodlowym.
# Geobaza ESRI (.gdb) to FOLDER - wykrywana osobno.
VECTOR_EXTENSIONS = {
    ".shp",
    ".gpkg",
    ".geojson",
    ".json",
    ".kml",
    ".kmz",
    ".gml",
    ".dxf",
    ".tab",
    ".mif",
    ".csv",
    ".fgb",
    ".sqlite",
    ".gpx",
    ".xml",
    # osobowa geobaza ESRI (Personal GDB) - tylko gdy GDAL ma sterownik
    # PGeo/ODBC
    ".mdb",
}


def is_file_gdb(path):
    """Czy folder jest plikowa geobaza ESRI (.gdb) - po nazwie albo po
    zawartosci (pliki 'gdb' / a0000000*.gdbtable)."""
    if not os.path.isdir(path):
        return False
    if path.rstrip("/\\").lower().endswith(".gdb"):
        return True
    try:
        names = [n.lower() for n in os.listdir(path)]
    except OSError:
        return False
    return "gdb" in names and any(n.endswith(".gdbtable") for n in names)


def _explode(geom):
    """Rozbicie geometrii wieloczesciowej / kolekcji na czesci pojedyncze."""
    from osgeo import ogr

    flat = ogr.GT_Flatten(geom.GetGeometryType())
    if ogr.GT_IsSubClassOf(flat, ogr.wkbGeometryCollection):
        out = []
        for i in range(geom.GetGeometryCount()):
            out.extend(_explode(geom.GetGeometryRef(i).Clone()))
        return out
    return [geom]


def _collect(ogr, parts):
    """Czesci jednego wymiaru -> Multi* (dla warstw o typie nieokreslonym)."""
    single = ogr.GT_Flatten(parts[0].GetGeometryType())
    if all(ogr.GT_Flatten(p.GetGeometryType()) == single for p in parts):
        g = ogr.Geometry(ogr.GT_GetCollection(single))
    else:
        g = ogr.Geometry(ogr.wkbGeometryCollection)
    for p in parts:
        g.AddGeometry(p)
    return g


# Formaty wyjsciowe: (klucz, etykieta, sterownik, rozszerzenie, wielowarstwowy,
#                     opcje warstwy)
VECTOR_FORMATS = [
    ("GPKG", "GeoPackage (.gpkg)", "GPKG", ".gpkg", True, []),
    (
        "OpenFileGDB",
        "Geobaza ESRI / ESRI File Geodatabase (.gdb)",
        "OpenFileGDB",
        ".gdb",
        True,
        [],
    ),
    (
        "ESRI Shapefile",
        "ESRI Shapefile (.shp)",
        "ESRI Shapefile",
        ".shp",
        False,
        ["ENCODING=UTF-8"],
    ),
    (
        "GeoJSON",
        "GeoJSON (.geojson)",
        "GeoJSON",
        ".geojson",
        False,
        ["RFC7946=NO"],
    ),
    ("GML", "GML (.gml)", "GML", ".gml", False, []),
    ("MapInfo File", "MapInfo TAB (.tab)", "MapInfo File", ".tab", False, []),
    ("FlatGeobuf", "FlatGeobuf (.fgb)", "FlatGeobuf", ".fgb", False, []),
    ("SQLite", "SQLite (.sqlite)", "SQLite", ".sqlite", True, []),
    ("DXF", "AutoCAD DXF (.dxf)", "DXF", ".dxf", False, []),
    ("CSV", "CSV + WKT (.csv)", "CSV", ".csv", False, ["GEOMETRY=AS_WKT"]),
]
# Uwaga: KML/KMZ celowo NIE ma na liscie wyjsciowej - format wymaga WGS84,
# wiec zapis zawsze oznaczalby przeliczenie wspolrzednych.

MODE_CLIP = "clip"  # przyciecie geometrii na granicy obszaru
MODE_SELECT = "select"  # wybor obiektow przecinajacych obszar (w calosci)

ST_OK = "OK"
ST_EMPTY = "brak obiektow w obszarze / no features in the area"
ST_SKIP_NO_GEOM = (
    "tabela bez geometrii - pominieta / table without geometry - skipped"
)
ST_CRS = "niezgodny uklad / CRS mismatch"
ST_ERROR = "blad / error"
ST_CANCELED = "nie przetworzono (anulowano) / not processed (canceled)"
ST_BAD_SOURCE = "nie mozna otworzyc / cannot open"

CSV_HEADER = [
    "zrodlo / source",
    "warstwa / layer",
    "obszar / area",
    "obiekty / features",
    "status",
    "uwagi / notes",
]


class VectorCanceled(Exception):
    pass


class VectorSettings:
    def __init__(
        self,
        input_dir,
        output_dir,
        output_format="GPKG",
        clip_dir=None,
        clip_mode=MODE_CLIP,
        reproject_clip=False,
        subfolder_per_source=True,
    ):
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.output_format = output_format
        self.clip_dir = clip_dir  # None = sama konwersja
        self.clip_mode = clip_mode
        self.reproject_clip = reproject_clip
        self.subfolder_per_source = subfolder_per_source


class VectorRow:
    def __init__(self, source, layer, area, count, status, notes=None):
        self.source = source
        self.layer = layer
        self.area = area
        self.count = count
        self.status = status
        self.notes = list(notes or [])


class VectorResult:
    def __init__(self):
        self.rows = []
        self.warnings = []
        self.report_path = None

    @property
    def processed(self):
        return [r for r in self.rows if r.status == ST_OK]

    @property
    def skipped(self):
        return [r for r in self.rows if r.status != ST_OK]


# ---------------------------------------------------------------------------


def available_vector_formats():
    from osgeo import ogr

    out = []
    for key, label, drv, _ext, _multi, _opts in VECTOR_FORMATS:
        d = ogr.GetDriverByName(drv)
        if d is not None and d.GetMetadataItem("DCAP_CREATE") == "YES":
            out.append((key, label))
    return out


def _format_entry(key):
    for e in VECTOR_FORMATS:
        if e[0] == key:
            return e
    return VECTOR_FORMATS[0]


def _safe_name(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" .")
    return name or "warstwa"


def collect_vector_sources(folder):
    """Lista sciezek zrodel wektorowych w folderze (pliki + foldery .gdb).
    Jesli wskazany folder SAM jest geobaza plikowa (.gdb) - jest jedynym
    zrodlem."""
    if is_file_gdb(folder):
        return [folder.rstrip("/\\")]
    found = []
    for name in sorted(os.listdir(folder), key=str.casefold):
        path = os.path.join(folder, name)
        low = name.lower()
        if os.path.isdir(path):
            if is_file_gdb(path):
                found.append(path)
            continue
        ext = os.path.splitext(low)[1]
        if ext not in VECTOR_EXTENSIONS:
            continue
        if ext == ".xml" and not low.endswith(".gml.xml"):
            continue
        found.append(path)
    return found


def _source_base(path):
    base = os.path.basename(path.rstrip("/\\"))
    if base.lower().endswith(".gdb"):
        return base[:-4]
    return os.path.splitext(base)[0]


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
    except TypeError:
        return bool(a.IsSame(b))


def _srs_label(srs):
    if srs is None:
        return "-"
    try:
        c = srs.Clone()
        c.AutoIdentifyEPSG()
        code = c.GetAuthorityCode(None)
        if code:
            return "EPSG:{} ({})".format(code, srs.GetName())
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("Nie rozpoznano kodu EPSG: %s", exc)
    return srs.GetName() or "?"


class ClipArea:
    def __init__(self, name, geom, srs, origin):
        self.name = name
        self.geom = geom
        self.srs = srs
        self.origin = origin  # plik / warstwa zrodlowa (do raportu)


def collect_clip_areas(folder, warnings):
    """Kazda warstwa poligonowa w folderze = jeden obszar wycinania.
    Nazwa obszaru: nazwa pliku (formaty jednowarstwowe, np. SHP) albo
    nazwa warstwy (GPKG, GDB). Kilka poligonow w warstwie = ich suma."""
    from osgeo import ogr

    areas = []
    used = {}
    for path in collect_vector_sources(folder):
        ds = ogr.Open(path, 0)
        if ds is None:
            warnings.append(
                bi(
                    "Poligony: nie mozna otworzyc {}".format(
                        os.path.basename(path)
                    ),
                    "Polygons: cannot open {}".format(os.path.basename(path)),
                )
            )
            continue
        multi = ds.GetLayerCount() > 1 or path.lower().endswith(
            (".gpkg", ".gdb", ".sqlite")
        )
        for i in range(ds.GetLayerCount()):
            lyr = ds.GetLayerByIndex(i)
            name = lyr.GetName() if multi else _source_base(path)
            union = None
            n = 0
            lyr.ResetReading()
            for feat in lyr:
                g = feat.GetGeometryRef()
                if g is None or g.IsEmpty():
                    continue
                flat = ogr.GT_Flatten(g.GetGeometryType())
                if flat not in (
                    ogr.wkbPolygon,
                    ogr.wkbMultiPolygon,
                    ogr.wkbCurvePolygon,
                    ogr.wkbMultiSurface,
                ):
                    continue
                g = g.Clone()
                if not g.IsValid():
                    g = (
                        g.MakeValid()
                        if hasattr(g, "MakeValid")
                        else g.Buffer(0)
                    )
                union = g if union is None else union.Union(g)
                n += 1
            if union is None or union.IsEmpty():
                warnings.append(
                    bi(
                        "Poligony: warstwa '{}' w {} nie zawiera poligonow -"
                        " pominieta".format(
                            lyr.GetName(), os.path.basename(path)
                        ),
                        "Polygons: layer '{}' in {} contains no polygons -"
                        " skipped".format(
                            lyr.GetName(), os.path.basename(path)
                        ),
                    )
                )
                continue
            safe = _safe_name(name)
            key = safe.casefold()
            if key in used:
                used[key] += 1
                safe = "{}_{}".format(safe, used[key])
                warnings.append(
                    bi(
                        "Poligony: powtorzona nazwa obszaru '{}' - zapisano"
                        " jako '{}'".format(name, safe),
                        "Polygons: duplicated area name '{}' - saved as '{}'"
                        .format(name, safe),
                    )
                )
            else:
                used[key] = 1
            srs = lyr.GetSpatialRef()
            areas.append(
                ClipArea(
                    safe,
                    union,
                    srs.Clone() if srs else None,
                    "{}{}".format(
                        os.path.basename(path),
                        ":" + lyr.GetName() if multi else "",
                    ),
                )
            )
            if n > 1:
                warnings.append(
                    bi(
                        "Obszar '{}': {} poligonow - wycinanie po ich sumie"
                        .format(safe, n),
                        "Area '{}': {} polygons - clipping by their union"
                        .format(safe, n),
                    )
                )
        ds = None
    return areas


# ---------------------------------------------------------------------------


class VectorRunner:

    def __init__(self, settings, progress=None, is_canceled=None):
        self.s = settings
        self._progress = progress or (lambda p: None)
        self._is_canceled = is_canceled or (lambda: False)
        self.result = VectorResult()
        self._created = (
            set()
        )  # pliki wielowarstwowe juz utworzone w tym przebiegu

    def _check_cancel(self):
        if self._is_canceled():
            raise VectorCanceled()

    def run(self):
        from osgeo import gdal, ogr, osr

        gdal.UseExceptions()
        ogr.UseExceptions()
        osr.UseExceptions()
        canceled = False
        try:
            sources = collect_vector_sources(self.s.input_dir)
            if self.s.clip_dir:
                areas = collect_clip_areas(
                    self.s.clip_dir, self.result.warnings
                )
                self.result.warnings.insert(
                    0,
                    bi(
                        "Wycinanie: {} zrodel x {} obszarow ({}) - BEZ"
                        " przeliczania danych".format(
                            len(sources),
                            len(areas),
                            (
                                "przyciecie geometrii"
                                if self.s.clip_mode == MODE_CLIP
                                else "wybor obiektow w calosci"
                            ),
                        ),
                        "Clipping: {} sources x {} areas ({}) - data NOT"
                        " transformed".format(
                            len(sources),
                            len(areas),
                            (
                                "geometry clipped"
                                if self.s.clip_mode == MODE_CLIP
                                else "whole features selected"
                            ),
                        ),
                    ),
                )
                if not areas:
                    self.result.warnings.append(
                        bi(
                            "Brak poligonow do wycinania",
                            "No clipping polygons",
                        )
                    )
                    return True
            else:
                areas = [None]
                self.result.warnings.insert(
                    0,
                    bi(
                        "Konwersja: {} zrodel - BEZ przeliczania (uklad i"
                        " wspolrzedne bez zmian)".format(len(sources)),
                        "Conversion: {} sources - NO transformation (CRS and"
                        " coordinates unchanged)".format(len(sources)),
                    ),
                )
            if not sources:
                self.result.warnings.append(
                    bi(
                        "Brak danych wektorowych w folderze",
                        "No vector data in the folder",
                    )
                )
                return True

            os.makedirs(self.s.output_dir, exist_ok=True)
            bases = self._unique_bases(sources)
            total = len(sources) * len(areas)
            step = 0
            try:
                for area in areas:
                    for src in sources:
                        self._check_cancel()
                        self._progress(100.0 * step / total)
                        step += 1
                        self._process_source(
                            gdal, ogr, osr, src, bases[src], area
                        )
            except VectorCanceled:
                canceled = True
                self.result.warnings.append(
                    bi("Przetwarzanie anulowane", "Processing canceled")
                )
            self._progress(100.0)
            return not canceled
        finally:
            self._remove_empty_area_dirs()
            self._write_report()

    def _remove_empty_area_dirs(self):
        """Obszar, w ktorym nic nie wpadlo - bez pustego folderu."""
        if not self.s.clip_dir or not os.path.isdir(self.s.output_dir):
            return
        for name in os.listdir(self.s.output_dir):
            path = os.path.join(self.s.output_dir, name)
            if os.path.isdir(path):
                for dp, dn, fn in os.walk(path, topdown=False):
                    if not fn and not os.listdir(dp):
                        try:
                            os.rmdir(dp)
                        except OSError:
                            pass

    def _unique_bases(self, sources):
        """Nazwa wyjsciowa zrodla; przy powtorzeniu (a.shp i a.gpkg) dopisywane
        jest rozszerzenie (a_shp, a_gpkg)."""
        counts = {}
        for s in sources:
            k = _source_base(s).casefold()
            counts[k] = counts.get(k, 0) + 1
        out = {}
        for s in sources:
            base = _safe_name(_source_base(s))
            if counts[base.casefold()] > 1:
                ext = os.path.splitext(s.rstrip("/\\"))[1].lstrip(".").lower()
                base = "{}_{}".format(base, ext)
            out[s] = base
        return out

    # -----------------------------------------------------------------

    def _process_source(self, gdal, ogr, osr, src_path, base, area):
        src_name = os.path.basename(src_path.rstrip("/\\"))
        area_name = area.name if area else ""
        try:
            ds = gdal.OpenEx(src_path, gdal.OF_VECTOR | gdal.OF_READONLY)
        except Exception:  # noqa: BLE001
            ds = None
        if ds is None:
            self.result.rows.append(
                VectorRow(src_name, "", area_name, "", ST_BAD_SOURCE)
            )
            return

        entry = _format_entry(self.s.output_format)
        _key, _label, drv_name, ext, multi, lco = entry
        out_root = (
            os.path.join(self.s.output_dir, area.name)
            if area
            else self.s.output_dir
        )
        os.makedirs(out_root, exist_ok=True)

        for i in range(ds.GetLayerCount()):
            self._check_cancel()
            lyr = ds.GetLayerByIndex(i)
            lname = lyr.GetName()
            notes = []
            try:
                count = self._process_layer(
                    gdal,
                    ogr,
                    osr,
                    ds,
                    lyr,
                    src_path,
                    base,
                    area,
                    out_root,
                    drv_name,
                    ext,
                    multi,
                    lco,
                    notes,
                )
                if count is None:
                    continue  # wiersz juz dopisany (pominiecie)
                self.result.rows.append(
                    VectorRow(src_name, lname, area_name, count, ST_OK, notes)
                )
            except VectorCanceled:
                raise
            except _Skip as sk:
                self.result.rows.append(
                    VectorRow(
                        src_name,
                        lname,
                        area_name,
                        "",
                        sk.status,
                        notes + [sk.msg],
                    )
                )
            except Exception as exc:  # noqa: BLE001
                if self._is_canceled():
                    raise VectorCanceled()
                self.result.rows.append(
                    VectorRow(
                        src_name,
                        lname,
                        area_name,
                        "",
                        ST_ERROR,
                        notes + [str(exc)],
                    )
                )
        ds = None

    def _process_layer(
        self,
        gdal,
        ogr,
        osr,
        ds,
        lyr,
        src_path,
        base,
        area,
        out_root,
        drv_name,
        ext,
        multi,
        lco,
        notes,
    ):
        lname = lyr.GetName()
        has_geom = lyr.GetGeomType() != ogr.wkbNone
        lsrs = lyr.GetSpatialRef()

        clip_geom = None
        if area is not None:
            if not has_geom:
                raise _Skip(
                    ST_SKIP_NO_GEOM,
                    bi(
                        "Warstwa nie ma geometrii - nie mozna jej wyciac",
                        "The layer has no geometry - it cannot be clipped",
                    ),
                )
            clip_geom = self._area_geom_for_layer(osr, area, lsrs, notes)
            # wstepny wybor obiektow (dokladny test przeciecia z obszarem)
            lyr.SetSpatialFilter(clip_geom)
            n = lyr.GetFeatureCount()
            lyr.SetSpatialFilter(None)
            if n == 0:
                raise _Skip(
                    ST_EMPTY,
                    bi(
                        "Zaden obiekt warstwy nie przecina obszaru",
                        "No feature of the layer intersects the area",
                    ),
                )

        # -- sciezka i nazwa warstwy wynikowej --
        out_layer = _safe_name(lname)
        if multi:
            dst = os.path.join(out_root, base + ext)
            # pierwsza warstwa tworzy plik (nadpisuje stary), kolejne dopisuja
            first = dst not in self._created
        else:
            if self.s.subfolder_per_source:
                folder = os.path.join(out_root, base)
                os.makedirs(folder, exist_ok=True)
                dst = os.path.join(folder, out_layer + ext)
            else:
                single_layer_src = ds.GetLayerCount() == 1
                name = (
                    base
                    if single_layer_src
                    else "{}_{}".format(base, out_layer)
                )
                dst = os.path.join(out_root, name + ext)
            first = True

        if first:
            self._delete_existing(gdal, ogr, dst, drv_name)

        opts = dict(
            format=drv_name,
            layers=[lname],
            layerName=out_layer,
            layerCreationOptions=list(lco),
            callback=lambda c, m, d: 0 if self._is_canceled() else 1,
        )
        if not first:
            opts["accessMode"] = "update"
        is_point = ogr.GT_Flatten(lyr.GetGeomType()) == ogr.wkbPoint
        if (
            drv_name in (
                "ESRI Shapefile",
                "MapInfo File",
                "OpenFileGDB",
                "FlatGeobuf",
            )
            and has_geom
            and not is_point
        ):
            # formaty z jednym typem geometrii na warstwe (linia + multilinia)
            opts["geometryType"] = "PROMOTE_TO_MULTI"
        if drv_name == "ESRI Shapefile":
            notes_shp = bi(
                "SHP: nazwy pol skracane do 10 znakow, kodowanie UTF-8 (.cpg)",
                "SHP: field names shortened to 10 characters, UTF-8 encoding"
                " (.cpg)",
            )
            if any(
                len(lyr.GetLayerDefn().GetFieldDefn(k).GetName()) > 10
                for k in range(lyr.GetLayerDefn().GetFieldCount())
            ):
                notes.append(notes_shp)

        src_for_translate = ds
        mem_ds = None
        if clip_geom is not None:
            if self.s.clip_mode == MODE_CLIP:
                # wlasne przyciecie (zamiast -clipSrc): z wyniku zostaja tylko
                # czesci o wymiarze zrodla, wiec sasiad stykajacy sie z granica
                # arkusza nie daje GeometryCollection (linia/punkt), ktorej nie
                # przyjmuja FlatGeobuf, SHP, TAB ani GDB
                mem_ds = self._clip_to_memory(
                    gdal, ogr, lyr, lname, clip_geom, notes
                )
                src_for_translate = mem_ds
            else:
                # wybor obiektow w calosci: kopia przefiltrowanych obiektow
                # (dokladny test przeciecia) do warstwy w pamieci
                mem_ds = self._select_to_memory(
                    gdal, ogr, lyr, lname, clip_geom
                )
                src_for_translate = mem_ds
            if mem_ds.GetLayer(0).GetFeatureCount() == 0:
                # np. tylko sasiedzi stykajacy sie z granica - bez pustych
                # plikow
                raise _Skip(
                    ST_EMPTY,
                    bi(
                        "Zaden obiekt warstwy nie lezy w obszarze (tylko styki"
                        " z granica)",
                        "No feature of the layer lies in the area (boundary"
                        " contacts only)",
                    ),
                )

        try:
            out = gdal.VectorTranslate(
                dst,
                src_for_translate,
                options=gdal.VectorTranslateOptions(**opts),
            )
        except Exception as exc:  # noqa: BLE001
            out = None
            if drv_name == "FlatGeobuf" and "NULL geometry" in str(exc):
                # FlatGeobuf z indeksem przestrzennym nie przyjmuje obiektow
                # bez
                # geometrii (np. tabele i puste geometrie w geobazie) - zapis
                # bez indeksu
                self._delete_existing(gdal, ogr, dst, drv_name)
                opts["layerCreationOptions"] = list(lco) + ["SPATIAL_INDEX=NO"]
                try:
                    out = gdal.VectorTranslate(
                        dst,
                        src_for_translate,
                        options=gdal.VectorTranslateOptions(**opts),
                    )
                except Exception:  # noqa: BLE001
                    out = None
                if out is not None:
                    notes.append(
                        bi(
                            "FlatGeobuf: obiekty bez geometrii - zapisano bez"
                            " indeksu przestrzennego",
                            "FlatGeobuf: features without geometry - saved"
                            " without a spatial index",
                        )
                    )
            if out is None:
                if not multi:
                    self._delete_existing(
                        gdal, ogr, dst, drv_name
                    )  # bez polowicznych plikow
                raise exc
        if out is None:
            raise RuntimeError(
                bi("ogr2ogr nie zwrocil wyniku", "ogr2ogr returned no result")
            )
        out = None
        mem_ds = None
        if multi:
            self._created.add(dst)

        if lsrs is None and has_geom:
            notes.append(
                bi(
                    "Warstwa nie ma ukladu wspolrzednych - zapisano bez"
                    " ukladu",
                    "The layer has no CRS - saved without CRS",
                )
            )
        if drv_name == "DXF":
            notes.append(
                bi(
                    "DXF: atrybuty nie sa zachowane",
                    "DXF: attributes are not preserved",
                )
            )
        if drv_name == "CSV" and has_geom:
            notes.append(
                bi(
                    "CSV: geometria jako tekst WKT, uklad wspolrzednych nie"
                    " jest zapisywany",
                    "CSV: geometry as WKT text, the CRS is not stored",
                )
            )

        return self._count_features(ogr, dst, out_layer, drv_name)

    def _select_to_memory(self, gdal, ogr, lyr, lname, clip_geom):
        """Obiekty, ktore maja czesc wspolna z wnetrzem obszaru, w calosci
        (bez zmiany geometrii). Obiekty tylko stykajace sie z granica arkusza
        (sasiedzi) nie sa wybierane.
        Features sharing part of the area interior, unchanged; features only
        touching the boundary are not selected."""
        mem_drv = gdal.GetDriverByName("Memory") or gdal.GetDriverByName("MEM")
        mem_ds = mem_drv.Create("sel", 0, 0, 0, gdal.GDT_Unknown)
        defn = lyr.GetLayerDefn()
        out = mem_ds.CreateLayer(lname, lyr.GetSpatialRef(), lyr.GetGeomType())
        for k in range(defn.GetFieldCount()):
            out.CreateField(defn.GetFieldDefn(k))
        out_defn = out.GetLayerDefn()
        lyr.SetSpatialFilter(clip_geom)
        lyr.ResetReading()
        try:
            for feat in lyr:
                self._check_cancel()
                g = feat.GetGeometryRef()
                if g is None or g.IsEmpty():
                    continue
                test = g.GetLinearGeometry() if g.HasCurveGeometry() else g
                try:
                    keep = test.Intersects(clip_geom) and (
                        test.GetDimension() == 0 or not test.Touches(clip_geom)
                    )
                except Exception:  # noqa: BLE001
                    keep = (
                        True  # niepoprawna geometria - decyduje filtr zasiegu
                    )
                if not keep:
                    continue
                nf = ogr.Feature(out_defn)
                nf.SetFrom(feat)
                nf.SetFID(feat.GetFID())
                out.CreateFeature(nf)
        finally:
            lyr.SetSpatialFilter(None)
        return mem_ds

    def _clip_to_memory(self, gdal, ogr, lyr, lname, clip_geom, notes):
        """Przyciecie obiektow warstwy do obszaru, wynik w warstwie w pamieci.
        Zostaja tylko czesci o tym samym wymiarze co obiekt zrodlowy
        (poligon -> poligony, linia -> linie); styki z granica sa pomijane.
        Clips the features to the area into an in-memory layer, keeping only
        parts of the source dimension (touching parts are dropped)."""
        src_type = lyr.GetGeomType()
        flat = ogr.GT_Flatten(src_type)
        known = flat not in (ogr.wkbUnknown, ogr.wkbGeometryCollection)
        if known:
            target = ogr.GT_GetLinear(src_type)  # krzywe -> odcinki (jak GEOS)
            if ogr.GT_Flatten(
                target
            ) != ogr.wkbPoint and not ogr.GT_IsSubClassOf(
                ogr.GT_Flatten(target), ogr.wkbGeometryCollection
            ):
                target = ogr.GT_GetCollection(
                    target
                )  # Polygon -> MultiPolygon
        else:
            target = src_type
        mem_drv = gdal.GetDriverByName("Memory") or gdal.GetDriverByName("MEM")
        mem_ds = mem_drv.Create("clip", 0, 0, 0, gdal.GDT_Unknown)
        defn = lyr.GetLayerDefn()
        out = mem_ds.CreateLayer(lname, lyr.GetSpatialRef(), target)
        for k in range(defn.GetFieldCount()):
            out.CreateField(defn.GetFieldDefn(k))
        out_defn = out.GetLayerDefn()
        n_fixed = 0
        lyr.SetSpatialFilter(clip_geom)
        lyr.ResetReading()
        try:
            for feat in lyr:
                self._check_cancel()
                g = feat.GetGeometryRef()
                if g is None or g.IsEmpty():
                    continue
                dim = g.GetDimension()
                if g.HasCurveGeometry():
                    g = g.GetLinearGeometry()  # luki -> odcinki (GEOS)
                try:
                    inter = g.Intersection(clip_geom)
                except Exception:  # noqa: BLE001
                    inter = None
                if inter is None:
                    # niepoprawna geometria (np. samoprzeciecie) - naprawa i
                    # ponowna proba
                    try:
                        inter = g.MakeValid().Intersection(clip_geom)
                        n_fixed += 1
                    except Exception:  # noqa: BLE001
                        inter = None
                if inter is None or inter.IsEmpty():
                    continue
                parts = [
                    p
                    for p in _explode(inter)
                    if p.GetDimension() == dim and not p.IsEmpty()
                ]
                if not parts:
                    continue  # tylko styk z granica
                if known:
                    if ogr.GT_Flatten(target) == ogr.wkbPoint:
                        geom = parts[0]
                    else:
                        geom = ogr.Geometry(target)
                        for p in parts:
                            geom.AddGeometry(p)
                else:
                    geom = (
                        parts[0] if len(parts) == 1 else _collect(ogr, parts)
                    )
                nf = ogr.Feature(out_defn)
                nf.SetFrom(feat)
                nf.SetGeometry(geom)
                out.CreateFeature(nf)
        finally:
            lyr.SetSpatialFilter(None)
        if n_fixed:
            notes.append(
                bi(
                    "Naprawiono {} niepoprawnych geometrii przed przycieciem"
                    .format(n_fixed),
                    "{} invalid geometries were repaired before clipping"
                    .format(n_fixed),
                )
            )
        return mem_ds

    def _area_geom_for_layer(self, osr, area, lsrs, notes):
        geom = area.geom
        if area.srs is None or lsrs is None:
            if area.srs is None:
                notes.append(
                    bi(
                        "Poligon wycinajacy nie ma ukladu - przyjeto uklad"
                        " danych",
                        "The clipping polygon has no CRS - the data CRS was"
                        " assumed",
                    )
                )
            return geom
        if _srs_equal(area.srs, lsrs):
            return geom
        if not self.s.reproject_clip:
            raise _Skip(
                ST_CRS,
                bi(
                    "Dane: {}, poligon: {}. Zaznacz 'Przeliczaj poligon z"
                    " innego ukladu', jesli to zamierzone".format(
                        _srs_label(lsrs), _srs_label(area.srs)
                    ),
                    "Data: {}, polygon: {}. Tick 'Reproject the polygon from"
                    " another CRS' if intended".format(
                        _srs_label(lsrs), _srs_label(area.srs)
                    ),
                ),
            )
        g = geom.Clone()
        env = g.GetEnvelope()
        span = max(env[1] - env[0], env[3] - env[2])
        if span > 0:
            g.Segmentize(span / 200.0)
        src = area.srs.Clone()
        dst = lsrs.Clone()
        src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        g.Transform(osr.CoordinateTransformation(src, dst))
        notes.append(
            bi(
                "Poligon wycinajacy przeliczono z {} do {} (dane bez zmian)"
                .format(_srs_label(area.srs), _srs_label(lsrs)),
                "Clipping polygon transformed from {} to {} (data unchanged)"
                .format(_srs_label(area.srs), _srs_label(lsrs)),
            )
        )
        return g

    def _delete_existing(self, gdal, ogr, path, drv_name):
        if not os.path.exists(path):
            return
        drv = ogr.GetDriverByName(drv_name)
        try:
            drv.DeleteDataSource(path)
        except Exception as exc:  # noqa: BLE001
            _LOG.debug("DeleteDataSource %s: %s", path, exc)
        if os.path.exists(path):
            if os.path.isdir(path):
                import shutil

                shutil.rmtree(path, ignore_errors=True)
            else:
                os.remove(path)

    def _count_features(self, ogr, dst, layer_name, drv_name):
        try:
            ds = ogr.Open(dst, 0)
            if ds is None:
                return ""
            lyr = (
                ds.GetLayerByName(layer_name)
                if ds.GetLayerCount() > 1
                else ds.GetLayer(0)
            )
            if lyr is None:
                lyr = ds.GetLayer(ds.GetLayerCount() - 1)
            return lyr.GetFeatureCount()
        except Exception:  # noqa: BLE001
            return ""

    # -----------------------------------------------------------------

    def _write_report(self):
        if not self.result.rows or not os.path.isdir(self.s.output_dir):
            return
        stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
        path = os.path.join(
            self.s.output_dir, "raport_wektory_{}.csv".format(stamp)
        )
        n = 1
        while os.path.exists(path):
            n += 1
            path = os.path.join(
                self.s.output_dir, "raport_wektory_{}_{}.csv".format(stamp, n)
            )
        rows = sorted(
            self.result.rows,
            key=lambda r: (
                r.status == ST_OK,
                r.area.casefold(),
                r.source.casefold(),
                r.layer.casefold(),
            ),
        )
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(CSV_HEADER)
                for r in rows:
                    w.writerow(
                        [
                            r.source,
                            r.layer,
                            r.area,
                            r.count,
                            r.status,
                            " | ".join(r.notes),
                        ]
                    )
            self.result.report_path = path
        except OSError as exc:
            self.result.warnings.append(
                bi(
                    "Nie udalo sie zapisac raportu CSV ({})".format(exc),
                    "Could not write the CSV report ({})".format(exc),
                )
            )


class _Skip(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status = status
        self.msg = msg
