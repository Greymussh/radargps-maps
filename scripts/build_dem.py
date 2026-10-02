#!/usr/bin/env python3
"""
Terrain add-on for a map pack: elevation tiles (Terrarium encoding) for 3D mountains, hill shading and contour lines.

Source: AWS Open Data "Terrain Tiles" (Mapzen; SRTM, GMTED, NED, ETOPO and other public datasets).
Only tiles that touch the pack's real coverage (the 'cells' grid in the search db) are kept.
To keep it small the sub-metre fraction (blue channel) is dropped and tiles are stored as lossless WebP.

usage: build_dem.py <pack.search> <id> <out_dir> [maxzoom=10]
writes <out_dir>/<id>.dem.mbtiles
"""
import base64, io, json, math, os, sqlite3, sys, time, urllib.request, zlib
from concurrent.futures import ThreadPoolExecutor
from PIL import Image

SRC = 'https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png'
MINZ = 4


def lon2x(lon, z): return int((lon + 180) / 360 * (1 << z))
def lat2y(lat, z):
    lat = max(-85.05, min(85.05, lat)); r = math.radians(lat)
    return int((1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * (1 << z))
def x2lon(x, z): return x / (1 << z) * 360 - 180
def y2lat(y, z): return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / (1 << z)))))


def coverage(search_db):
    db = sqlite3.connect(search_db)
    meta = dict(db.execute('select k, v from meta').fetchall())
    bbox = json.loads(meta['bbox'])
    cells = json.loads(meta['cells']) if 'cells' in meta else None
    bits = None
    if cells:
        raw = zlib.decompress(base64.b64decode(cells['bits']))
        bits = (cells, raw)
    return bbox, bits


def covered(bits, w, s, e, n):
    if not bits: return True
    c, raw = bits
    x0 = max(0, int((w - c['west']) / c['cell'])); x1 = min(c['cols'] - 1, int((e - c['west']) / c['cell']))
    y0 = max(0, int((s - c['south']) / c['cell'])); y1 = min(c['rows'] - 1, int((n - c['south']) / c['cell']))
    for yy in range(y0, y1 + 1):
        for xx in range(x0, x1 + 1):
            i = yy * c['cols'] + xx
            if raw[i >> 3] & (1 << (i & 7)): return True
    return False


def fetch(z, x, y):
    url = SRC.format(z=z, x=x, y=y)
    for k in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'map-pack-builder'}), timeout=60) as r:
                data = r.read()
            im = Image.open(io.BytesIO(data)).convert('RGB')
            # drop the sub-metre fraction (blue) -> compresses ~3x better
            r_, g_, b_ = im.split()
            b_ = b_.point(lambda v: 0)
            im = Image.merge('RGB', (r_, g_, b_))
            out = io.BytesIO(); im.save(out, 'WEBP', lossless=True, quality=100, method=4)
            return z, x, y, out.getvalue()
        except urllib.error.HTTPError as e:
            if e.code == 404: return z, x, y, None
            time.sleep(1 + k)
        except Exception:
            time.sleep(1 + k)
    return z, x, y, None


def main():
    search, pid, out = sys.argv[1], sys.argv[2], sys.argv[3]
    maxz = int(sys.argv[4]) if len(sys.argv) > 4 else 10
    bbox, bits = coverage(search)
    w, s, e, n = bbox
    jobs = []
    for z in range(MINZ, maxz + 1):
        for x in range(lon2x(w, z), lon2x(e, z) + 1):
            for y in range(lat2y(n, z), lat2y(s, z) + 1):
                if z >= 8 and not covered(bits, x2lon(x, z), y2lat(y + 1, z), x2lon(x + 1, z), y2lat(y, z)):
                    continue
                jobs.append((z, x, y))
    print(len(jobs), 'terrain tiles', file=sys.stderr)
    path = os.path.join(out, pid + '.dem.mbtiles')
    if os.path.exists(path): os.remove(path)
    db = sqlite3.connect(path)
    db.executescript('''PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
      CREATE TABLE metadata(name TEXT, value TEXT);
      CREATE TABLE tiles(zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB);''')
    db.executemany('INSERT INTO metadata VALUES(?,?)', [('name', pid + ' terrain'), ('format', 'webp'), ('encoding', 'terrarium'),
        ('minzoom', str(MINZ)), ('maxzoom', str(maxz)), ('bounds', ','.join(map(str, bbox))),
        ('attribution', 'Terrain Tiles: Mapzen / AWS Open Data (SRTM, GMTED2010, ETOPO1, NED and others)')])
    n_ok = size = 0
    with ThreadPoolExecutor(24) as ex:
        for z, x, y, data in ex.map(lambda j: fetch(*j), jobs):
            if data is None: continue
            db.execute('INSERT INTO tiles VALUES(?,?,?,?)', (z, x, (1 << z) - 1 - y, sqlite3.Binary(data)))
            n_ok += 1; size += len(data)
    db.execute('CREATE UNIQUE INDEX tile_index ON tiles(zoom_level, tile_column, tile_row)')
    db.commit(); db.execute('VACUUM'); db.close()
    print(json.dumps({'tiles': n_ok, 'bytes': os.path.getsize(path)}))


if __name__ == '__main__':
    main()
