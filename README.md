# Wycinanie i konwersja QGIS 4 (rastry i wektory) — QGIS 4.x

**Wersja dla QGIS 4 (Qt6 / PyQt6).** To osobna, niezależna wtyczka: folder
`wycinanie_i_konwersja_qgis4`, własna nazwa i menu „Wycinanie i konwersja
QGIS 4”. Nie zastępuje i nie wymaga wersji dla QGIS 3 (`wycinanie_i_konwersja`)
— w QGIS 3 się nie wczyta, w QGIS 4 działa samodzielnie. Funkcje jak w wersji
2.1.2 dla QGIS 3.

*Clipping and conversion (rasters and vectors). Interfejs, log i raporty CSV
są dwujęzyczne (polski / English).*

Wtyczka ma dwie zakładki: **Rastry** i **Wektory**. Układ współrzędnych danych
nigdy nie jest zmieniany.

### Instalacja
QGIS 4 → Wtyczki → Zarządzaj wtyczkami → Instaluj z pliku ZIP →
`wycinanie_i_konwersja_qgis4.zip`.

---

## Zakładka „Rastry”

### Wycinanie wg arkuszy
- **Trzy foldery**: rastry, arkusze (.shp/.gpkg), folder wyjściowy.
- **Jeden plik arkusza = jeden arkusz** o tej samej nazwie co raster
  (`N3450Cd3.shp` + `N3450Cd3.tif`, wielkość liter bez znaczenia).
- Kilka poligonów w pliku: wybierany jest ten, który w polu tekstowym ma nazwę
  arkusza. GPKG z kilkoma warstwami: warstwa o nazwie arkusza, a gdy jej nie
  ma — pierwsza.
- Wynik przycięty do zasięgu arkusza, zasięg dopasowany do siatki pikseli
  (bez przesunięcia; „najbliższy sąsiad” kopiuje piksele 1:1).
- Poza arkuszem: **tło białe/czarne + maska** (domyślnie) albo **kanał alfa**.
  Nieoznaczony 4. kanał (0/255) jest rozpoznawany jako przezroczystość.
- Układ arkusza = układ rastra; przy różnych układach raster jest pomijany,
  chyba że zaznaczono „Przeliczaj z innego układu” (przeliczana jest tylko
  granica arkusza).
- **Pliki bez układu** (np. JPG z samym `.jgw`, arkusz bez `.prj`) — lista
  „Układ dla plików bez układu” (domyślnie EPSG:2180):
  1. raster bez układu dostaje układ arkusza (i odwrotnie),
  2. gdy oba są bez układu — układ wybrany na liście,
  3. przy „(brak)” — wycinanie w samych współrzędnych, wynik bez układu
     (tylko plik georeferencyjny).

  Przyjęty układ jest zapisywany w wyniku (GeoTIFF — w pliku, JPG/PNG —
  w `.aux.xml`) i opisany w raporcie CSV. Działa też w trybach konwersji.

### Konwersja formatu
- **„Tylko konwersja formatu (bez wycinania)”** — z pozostałymi opcjami
  (DPI, resampling, 8-bit…). Raster nieobrócony kopiowany 1:1, obrócony
  sprowadzany do siatki północ-góra.
- **„Bez przeliczeń (nie wykonuje transformacji)”** — czysta konwersja
  (`gdal_translate`): piksele, georeferencja (także obrót) i układ dokładnie
  jak w źródle, zmienia się tylko format. Wyłączone: resampling, DPI.
  „Wymuś 8-bit” działa tylko dla plików 16-bit / float.

### Format wyjściowy
Jak plik źródłowy, GeoTIFF, COG, JPEG, PNG, JPEG 2000, WebP, BMP, ERDAS IMG
(lista pokazuje tylko formaty dostępne w danej instalacji). **Plik
georeferencyjny dopisywany automatycznie**: `.tif→.tfw`, `.jpg→.jgw`,
`.png→.pgw`, `.jp2→.j2w`, `.bmp→.bpw`, `.webp→.wld`. Przezroczystość:
GeoTIFF/COG/JPEG — maska, PNG/WebP/JPEG 2000 — kanał alfa, BMP/IMG — tylko
tło. Raster z paletą barw zapisywany do JPEG/WebP/JPEG 2000 jest rozwijany do
RGB. Dane float do PNG/JPEG/BMP — błąd z podpowiedzią (zaznacz „Wymuś 8-bit”
albo wybierz GeoTIFF).

### Wymuszony 8-bit
Wspólny zakres per kanał liczony tylko wewnątrz arkuszy; pliki już 8-bit nie
są rozciągane. Opcja **„Użyj, gdy jest mało miejsca na dysku”**: najwyżej
jeden roboczy plik naraz (wolniej).

Raport: `raport_wycinania_RRRR-MM-DD_GGMM.csv`.

---

## Zakładka „Wektory” — bez przeliczeń

**Współrzędne i układ danych nie są nigdy zmieniane** (nie wykonuje
transformacji).

- **Źródła**: wszystko, co czyta QGIS — SHP, GPKG, **geobaza ESRI (.gdb)**,
  GML, GeoJSON, KML, DXF, TAB/MIF, CSV, FlatGeobuf, SQLite, GPX. Przetwarzane
  są wszystkie warstwy każdego źródła.
- **Geobaza plikowa**: można wskazać folder, w którym leży `nazwa.gdb`, albo
  bezpośrednio sam folder `nazwa.gdb` (nazwa musi kończyć się na `.gdb`).
  Geobaza osobista (`.mdb`) wymaga sterownika PGeo/ODBC w GDAL.
- **Formaty wyjściowe**: GPKG, **geobaza ESRI (.gdb)**, SHP, GeoJSON, GML,
  MapInfo TAB, FlatGeobuf, SQLite, DXF, CSV (KML pominięty — wymaga WGS84).
  - GPKG / GDB / SQLite: jeden plik na źródło, wszystkie warstwy.
  - SHP, TAB, GeoJSON, GML, FlatGeobuf, CSV, DXF: plik na warstwę; opcja
    **„Osobny podfolder dla każdego źródła”** (`źródło/warstwa.shp`) albo
    wszystko razem (`źródło_warstwa.shp`).
  - SHP: nazwy pól do 10 znaków, UTF-8 (.cpg). DXF: bez atrybutów.
    CSV: geometria jako WKT, bez układu.
- **„Wycinaj poligonami z folderu”**: każda warstwa poligonowa w folderze
  poligonów = jeden obszar (arkusz albo dowolny kształt) = **osobny katalog
  wynikowy o nazwie tej warstwy** (SHP — nazwa pliku, GPKG/GDB — nazwa
  warstwy). Kilka poligonów w warstwie — wycinanie po ich sumie.
  - **Przycinaj geometrie na granicy** albo **wybierz obiekty w całości**
    (bez cięcia, geometria identyczna ze źródłem).
  - „Przeliczaj poligon z innego układu” — przeliczany jest wyłącznie
    poligon wycinający, nigdy dane. Domyślnie wyłączone: przy różnych
    układach warstwa jest pomijana.
  - Przycinanie zostawia tylko części o wymiarze źródła (poligon → poligony,
    linia → linie). Obiekty sąsiednie, które tylko stykają się z granicą
    arkusza, są pomijane w obu trybach — nie powstają kolekcje geometrii,
    których nie przyjmują FlatGeobuf, SHP, TAB ani GDB.
  - FlatGeobuf: warstwa z obiektami bez geometrii zapisywana jest bez indeksu
    przestrzennego (z indeksem format ich nie przyjmuje).
  - Warstwy bez obiektów w obszarze nie tworzą pustych plików.

Raport: `raport_wektory_RRRR-MM-DD_GGMM.csv` (źródło, warstwa, obszar, liczba
obiektów, status, uwagi).

---

## Praca w tle
Przetwarzanie idzie w tle (`QgsTask`): okno wtyczki i QGIS pozostają aktywne,
blokowany jest tylko przycisk „Uruchom” (jedno zadanie naraz). Po zakończeniu
pojawia się nieblokujący komunikat na pasku QGIS (znika sam), szczegóły są
w logu i raporcie CSV.

## Silnik
GDAL/OGR: `gdal.Warp` (wycinanie rastrów), `gdal.Translate` (konwersja),
`gdal.VectorTranslate` / ogr2ogr (wektory). Rdzenie `clip_core.py` i
`vector_core.py` nie zależą od QGIS; okno i zadania w tle (`QgsTask`) —
`clip_dialog.py`, `clip_task.py`.
