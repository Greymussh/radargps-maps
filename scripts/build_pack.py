#!/usr/bin/env python3
"""
Radar GPS pack builder.

Reads one OpenStreetMap extract (.osm.pbf) and writes, into OUT_DIR:
  <id>.route   routing graph for cars (compact binary, see FORMAT below)
  <id>.search  SQLite database: places/POIs/streets/addresses for offline search,
               POI details for the place card, and street points for "where am I"
The map tiles (<id>.mbtiles) are built separately with Planetiler.

FORMAT of .route (little endian):
  b'RGR2', int32 version=2
  int32 nV, nE, nAdj, geomBytes, namesBytes
  int32[nV*2]   vertex lat*1e6, lon*1e6
  int32[nV+1]   adjacency offsets into adj[]
  int32[nAdj]   adj entries: edge*2 + dir  (dir 0 = from->to, 1 = to->from)
  int32[nE]     from vertex
  int32[nE]     to vertex
  float32[nE]   length (m)
  float32[nE]   travel time (s)
  int32[nE]     name index (-1 = unnamed)
  int32[nE+1]   byte offset of each edge's geometry in the geometry blob
  uint8[nE]     flags: 1 = oneway (from->to only), 2 = roundabout, bits 4..7 = road class
  bytes         geometry: per edge, the points between its two vertices as zigzag-varint
                deltas (lat, lon, in 1e-6 degrees) starting from the 'from' vertex
  bytes         names, utf-8, each terminated by \\0

Text normalisation (norm()) MUST stay identical to Pack.norm() in the Android app.
"""
import base64, json, math, os, re, sqlite3, struct, sys, unicodedata, zlib
from array import array
import osmium

# ---------------------------------------------------------------- normalisation
GEO = dict(zip('აბგდევზთიკლმნოპჟრსტუფქღყშჩცძწჭხჯჰ',
               ['a', 'b', 'g', 'd', 'e', 'v', 'z', 't', 'i', 'k', 'l', 'm', 'n', 'o', 'p', 'zh', 'r', 's', 't', 'u',
                'p', 'k', 'gh', 'q', 'sh', 'ch', 'ts', 'dz', 'ts', 'ch', 'kh', 'j', 'h']))
CYR = dict(zip('абвгдеёжзийклмнопрстуфхцчшщъыьэюяіїєґ',
               ['a', 'b', 'v', 'g', 'd', 'e', 'e', 'zh', 'z', 'i', 'i', 'k', 'l', 'm', 'n', 'o', 'p', 'r', 's', 't', 'u',
                'f', 'kh', 'ts', 'ch', 'sh', 'shch', '', 'y', '', 'e', 'yu', 'ya', 'i', 'i', 'e', 'g']))


def translit(s):
    out = []
    for ch in s:
        out.append(GEO.get(ch) or CYR.get(ch) or ch)
    return ''.join(out)


def norm(s):
    if not s:
        return ''
    s = s.lower()
    s = translit(s)
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = s.replace('f', 'p').replace('q', 'k').replace('w', 'v')
    s = re.sub(r'[^a-z0-9]+', ' ', s)
    return s.strip()


def tokens(s):
    return [t for t in norm(s).split(' ') if t]


def latin_title(s):
    """Readable Latin version of a Georgian/Cyrillic name (for display)."""
    if not s or not re.search('[Ⴀ-ჿЀ-ӿ]', s):
        return s
    t = translit(s.lower())
    for a, b in (('kucha', 'Street'), ('gamziri', 'Avenue'), ('moedani', 'Square'), ('shesakhvevi', 'Lane'),
                 ('chikhi', 'Dead End'), ('sanapiro', 'Embankment'), ('gzatketsili', 'Highway')):
        t = re.sub(r'\b' + a + r'\b', b, t)
    return re.sub(r'\b\w', lambda m: m.group(0).upper(), t)

# ---------------------------------------------------------------- roads
SPEED = {  # km/h defaults
    'motorway': 110, 'motorway_link': 60, 'trunk': 90, 'trunk_link': 50, 'primary': 65, 'primary_link': 45,
    'secondary': 55, 'secondary_link': 40, 'tertiary': 45, 'tertiary_link': 35, 'unclassified': 35,
    'residential': 28, 'living_street': 10, 'service': 15, 'road': 25,
}
CLASS = {'motorway': 1, 'motorway_link': 1, 'trunk': 2, 'trunk_link': 2, 'primary': 3, 'primary_link': 3,
         'secondary': 4, 'secondary_link': 4, 'tertiary': 5, 'tertiary_link': 5, 'unclassified': 6,
         'residential': 7, 'living_street': 8, 'service': 9, 'road': 7}


def parse_speed(v):
    if not v:
        return None
    m = re.match(r'\s*(\d+(?:\.\d+)?)\s*(mph)?', v)
    if not m:
        return None
    s = float(m.group(1))
    return s * 1.609 if m.group(2) else s


def car_ok(t):
    hw = t.get('highway')
    if hw not in SPEED:
        return False
    if t.get('area') == 'yes':
        return False
    if hw == 'service' and t.get('service') in ('parking_aisle', 'driveway', 'emergency_access'):
        return False
    acc = t.get('motorcar') or t.get('motor_vehicle') or t.get('vehicle') or t.get('access')
    if acc in ('no', 'private', 'agricultural', 'forestry', 'delivery'):
        return False
    return True


POI_KEYS = ('amenity', 'shop', 'tourism', 'leisure', 'office', 'craft', 'healthcare', 'historic')
POI_SKIP = {'amenity': {'bench', 'waste_basket', 'parking_space', 'vending_machine', 'bicycle_parking', 'shelter',
                        'drinking_water', 'recycling', 'waste_disposal', 'parking_entrance', 'clock', 'grit_bin',
                        'telephone', 'post_box', 'hunting_stand'}}
KEEP_TAGS = ('opening_hours', 'phone', 'contact:phone', 'website', 'contact:website', 'url', 'cuisine', 'wheelchair',
             'operator', 'brand', 'internet_access', 'outdoor_seating', 'takeaway', 'delivery', 'payment:cards',
             'payment:credit_cards', 'religion', 'denomination', 'description', 'name:en', 'name:ka', 'name:ru',
             'fuel:diesel', 'fuel:octane_95', 'fuel:lpg', 'addr:street', 'addr:housenumber', 'addr:city',
             'amenity', 'shop', 'tourism', 'leisure', 'office', 'craft', 'healthcare', 'historic', 'railway',
             'public_transport', 'station', 'stars', 'rooms')
PLACE_RANK = {'city': 100, 'town': 85, 'suburb': 70, 'village': 60, 'quarter': 55, 'neighbourhood': 50,
              'hamlet': 40, 'locality': 30, 'island': 45}


class Collector(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.ways = []          # (node_ids, coords, name, cls, oneway, roundabout, speed)
        self.places = []        # dicts for the search DB
        self.addrs = {}
        self.street_pts = {}    # (cell, name) -> (lat, lon)
        self.node_use = {}

    # -- helpers
    def add_poi(self, t, lat, lon):
        name = t.get('name') or t.get('name:en') or t.get('brand')
        key = next((k for k in POI_KEYS if k in t), None)
        if key is None and t.get('railway') in ('station', 'halt', 'tram_stop'):
            key = 'railway'
        if key is None and t.get('highway') == 'bus_stop':
            key = 'highway'
        if key is None:
            return False
        val = t.get(key)
        if val in POI_SKIP.get(key, ()):
            return False
        if not name and key not in ('amenity', 'shop'):
            return False
        if not name and val not in ('fuel', 'pharmacy', 'hospital', 'atm', 'bank', 'toilets', 'police'):
            return False
        tags = {k: t.get(k) for k in KEEP_TAGS if t.get(k)}
        self.places.append(dict(name=name or '', name_en=t.get('name:en') or '', kind='poi', sub=val, key=key,
                                lat=lat, lon=lon, rank=30, tags=tags,
                                addr=((t.get('addr:street') or '') + ' ' + (t.get('addr:housenumber') or '')).strip()))
        return True

    def add_addr(self, t, lat, lon):
        st, hn = t.get('addr:street'), t.get('addr:housenumber')
        if st and hn:
            self.addrs[(st, hn)] = (lat, lon, t.get('addr:city') or '')

    # -- osmium callbacks
    def node(self, n):
        t = n.tags
        if not len(t):
            return
        loc = n.location
        if not loc.valid():
            return
        lat, lon = loc.lat, loc.lon
        if 'place' in t and t.get('name') and t['place'] in PLACE_RANK:
            self.places.append(dict(name=t['name'], name_en=t.get('name:en') or '', kind='place', sub=t['place'],
                                    key='place', lat=lat, lon=lon, rank=PLACE_RANK[t['place']], tags={}, addr=''))
            return
        if not self.add_poi(t, lat, lon):
            self.add_addr(t, lat, lon)

    def way(self, w):
        t = w.tags
        pts = []
        ids = []
        for n in w.nodes:
            if n.location.valid():
                pts.append((n.location.lat, n.location.lon))
                ids.append(n.ref)
        if len(pts) < 2:
            return
        hw = t.get('highway')
        name = t.get('name') or t.get('ref')
        if hw and car_ok(t):
            ow = t.get('oneway')
            rb = t.get('junction') in ('roundabout', 'circular')
            oneway = 1 if (ow in ('yes', 'true', '1') or (rb and ow != 'no') or (hw in ('motorway', 'motorway_link') and ow != 'no')) \
                else (-1 if ow in ('-1', 'reverse') else 0)
            sp = parse_speed(t.get('maxspeed')) or SPEED[hw]
            sp = min(sp * 0.9, SPEED[hw] * 1.25)
            if oneway == -1:
                ids.reverse(); pts.reverse(); oneway = 1
            self.ways.append((ids, pts, t.get('name') or t.get('ref') or '', CLASS[hw], oneway, rb, sp))
            for i in ids:
                self.node_use[i] = self.node_use.get(i, 0) + 1
            self.node_use[ids[0]] += 1
            self.node_use[ids[-1]] += 1
        if hw and name and hw not in ('footway', 'path', 'cycleway', 'steps', 'bridleway', 'corridor', 'proposed', 'construction'):
            for (lat, lon) in pts[::2] + [pts[-1]]:
                c = (int(math.floor(lat / 0.002)), int(math.floor(lon / 0.002)))
                self.street_pts.setdefault((c, t.get('name') or name), (lat, lon))
            return
        # building/area POIs and addresses: use the way's centre
        lat = sum(p[0] for p in pts) / len(pts)
        lon = sum(p[1] for p in pts) / len(pts)
        if not self.add_poi(t, lat, lon):
            self.add_addr(t, lat, lon)


def haversine(a, b):
    R = 6371008.8
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * R * math.asin(min(1, math.sqrt(h)))


def zz(v):
    return (v << 1) ^ (v >> 63)


def put_varint(buf, v):
    while v >= 0x80:
        buf.append((v & 0x7f) | 0x80)
        v >>= 7
    buf.append(v)


def build_graph(c, out_path):
    vid = {}
    vlat, vlon = array('i'), array('i')
    efrom, eto, elen, etime, ename = (array('i'), array('i'), array('f'), array('f'), array('i'))
    goff = array('i', [0])
    eflag = bytearray()
    geom = bytearray()
    names, name_idx = [], {}
    npts = 0

    def vertex(nid, p):
        v = vid.get(nid)
        if v is None:
            v = len(vlat)
            vid[nid] = v
            vlat.append(int(round(p[0] * 1e6))); vlon.append(int(round(p[1] * 1e6)))
        return v

    for ids, pts, name, cls, oneway, rb, sp in c.ways:
        ni = -1
        if name:
            ni = name_idx.get(name)
            if ni is None:
                ni = len(names); name_idx[name] = ni; names.append(name)
        start = 0
        for i in range(1, len(ids)):
            last = i == len(ids) - 1
            if not last and c.node_use.get(ids[i], 0) < 2:
                continue
            seg = pts[start:i + 1]
            if len(seg) >= 2 and ids[start] != ids[i] or len(seg) > 2:
                length = sum(haversine(seg[k - 1], seg[k]) for k in range(1, len(seg)))
                if length > 0.1:
                    a = vertex(ids[start], seg[0]); b = vertex(ids[i], seg[-1])
                    efrom.append(a); eto.append(b); elen.append(length)
                    etime.append(length / (sp / 3.6)); ename.append(ni)
                    # intermediate points only, delta + zigzag varint from the start vertex
                    pl, pn = vlat[a], vlon[a]
                    for p in seg[1:-1]:
                        la, lo = int(round(p[0] * 1e6)), int(round(p[1] * 1e6))
                        put_varint(geom, zz(la - pl)); put_varint(geom, zz(lo - pn))
                        pl, pn = la, lo
                        npts += 1
                    goff.append(len(geom))
                    eflag.append((1 if oneway else 0) | (2 if rb else 0) | (cls << 4))
            start = i

    nV, nE = len(vlat), len(efrom)
    adj_lists = [[] for _ in range(nV)]
    for e in range(nE):
        adj_lists[efrom[e]].append(e * 2)
        if not (eflag[e] & 1):
            adj_lists[eto[e]].append(e * 2 + 1)
    off, adj = array('i', [0]), array('i')
    for lst in adj_lists:
        adj.extend(lst); off.append(len(adj))
    vert = array('i')
    for i in range(nV):
        vert.append(vlat[i]); vert.append(vlon[i])
    nb = b''.join(n.encode('utf-8') + b'\0' for n in names)
    arrs = (vert, off, adj, efrom, eto, elen, etime, ename, goff)
    if sys.byteorder != 'little':
        for arr in arrs:
            arr.byteswap()
    with open(out_path, 'wb') as f:
        f.write(b'RGR2'); f.write(struct.pack('<6i', 2, nV, nE, len(adj), len(geom), len(nb)))
        for arr in arrs:
            f.write(arr.tobytes())
        f.write(bytes(eflag)); f.write(bytes(geom)); f.write(nb)
    return nV, nE, npts


DROP_TAGS = ('name:ka', 'name:ru', 'public_transport', 'station', 'addr:housenumber', 'addr:street', 'addr:city')
KIND = {'place': 1, 'poi': 2, 'street': 3, 'address': 4}


def build_search(c, out_path, bbox, cells=None):
    if os.path.exists(out_path):
        os.remove(out_path)
    db = sqlite3.connect(out_path)
    db.executescript('''
      PRAGMA page_size=4096; PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
      CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
      CREATE TABLE names(id INTEGER PRIMARY KEY, name TEXT, latin TEXT);
      CREATE TABLE places(id INTEGER PRIMARY KEY, name TEXT, latin TEXT, kind INTEGER, sub TEXT,
                          lat INTEGER, lon INTEGER, rank INTEGER, sid INTEGER, tags TEXT);
      CREATE TABLE tok(t TEXT, id INTEGER, PRIMARY KEY(t, id)) WITHOUT ROWID;
      CREATE TABLE spts(c INTEGER, sid INTEGER, lat INTEGER, lon INTEGER, PRIMARY KEY(c, sid)) WITHOUT ROWID;
    ''')
    # street names, stored once
    sids = {}

    def sid_of(name):
        i = sids.get(name)
        if i is None:
            i = len(sids) + 1
            sids[name] = i
            lt = latin_title(name)
            db.execute('INSERT INTO names VALUES(?,?,?)', (i, name, lt if lt != name else None))
        return i

    E6 = lambda v: int(round(v * 1e6))
    rows = []   # (name, latin, kind, sub, lat, lon, rank, sid, tags, words)
    for p in c.places:
        tags = {k: v for k, v in p['tags'].items() if k not in DROP_TAGS}
        if p['key'] == 'highway':
            tags = {}
        if p.get('addr'):
            tags['addr'] = p['addr']
        latin = p['name_en'] or latin_title(p['name'])
        words = set(tokens(p['name']) + tokens(p['name_en']))
        if p['kind'] == 'poi':
            words |= set(tokens(p['sub'].replace('_', ' ')))
            for cu in (p['tags'].get('cuisine') or '').split(';'):
                words |= set(tokens(cu.replace('_', ' ')))
            words |= set(tokens(p['tags'].get('brand') or ''))
        rows.append((p['name'] or None, latin if latin and latin != p['name'] else None, KIND[p['kind']], p['sub'],
                     E6(p['lat']), E6(p['lon']), p['rank'], None,
                     json.dumps(tags, ensure_ascii=False, separators=(',', ':')) if tags else None, words))
    # streets: one searchable entry per name per ~2 km cluster
    seen = set()
    for (cell, name), (lat, lon) in c.street_pts.items():
        k = (name, cell[0] // 10, cell[1] // 10)
        if k in seen:
            continue
        seen.add(k)
        rows.append((None, None, 3, None, E6(lat), E6(lon), 45, sid_of(name), None, set(tokens(name) + tokens(latin_title(name)))))
    for (st, hn), (lat, lon, city) in c.addrs.items():
        rows.append((hn, None, 4, None, E6(lat), E6(lon), 20, sid_of(st), None, None))
    toks = []
    for i, r in enumerate(rows, 1):
        db.execute('INSERT INTO places VALUES(?,?,?,?,?,?,?,?,?,?)', (i,) + r[:9])
        if r[9]:
            for w in r[9]:
                toks.append((w, i))
    db.executemany('INSERT OR IGNORE INTO tok VALUES(?,?)', toks)
    sp = []
    for (cell, name), (lat, lon) in c.street_pts.items():
        sp.append(((cell[0] + 100000) * 1000000 + (cell[1] + 100000), sid_of(name), E6(lat), E6(lon)))
    db.executemany('INSERT OR IGNORE INTO spts VALUES(?,?,?,?)', sp)
    db.executescript('''
      CREATE INDEX places_ll ON places(lat, lon);
      CREATE INDEX places_addr ON places(sid, name) WHERE kind=4;
    ''')
    db.executemany('INSERT INTO meta VALUES(?,?)', [('version', '2'), ('bbox', json.dumps(bbox)),
                                                     ('places', str(len(rows)))] + ([('cells', json.dumps(cells))] if cells else []))
    db.commit()
    db.execute('VACUUM')
    db.close()
    return len(rows), len(toks), len(sp)


CELL = 0.05   # degrees


def coverage(c, bbox):
    """Which ~5 km cells of the bbox this pack really covers (roads/places, grown by one cell).
    Neighbouring packs (e.g. US states) have overlapping boxes; the app uses this to pick the right one."""
    w, s_, e, n = bbox
    cols, rows = max(1, int(math.ceil((e - w) / CELL))), max(1, int(math.ceil((n - s_) / CELL)))
    hit = bytearray(cols * rows)

    def mark(lat, lon):
        x, y = int((lon - w) / CELL), int((lat - s_) / CELL)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                xx, yy = x + dx, y + dy
                if 0 <= xx < cols and 0 <= yy < rows:
                    hit[yy * cols + xx] = 1
    for wy in c.ways:
        for lat, lon in wy[1][::3] + [wy[1][-1]]:
            mark(lat, lon)
    for p in c.places:
        mark(p['lat'], p['lon'])
    bits = bytearray((cols * rows + 7) // 8)
    for i, h in enumerate(hit):
        if h:
            bits[i >> 3] |= 1 << (i & 7)
    return dict(cell=CELL, west=w, south=s_, cols=cols, rows=rows,
                bits=base64.b64encode(zlib.compress(bytes(bits), 9)).decode())


def main():
    if len(sys.argv) < 4:
        print('usage: build_pack.py <extract.osm.pbf> <id> <out_dir>')
        sys.exit(1)
    pbf, pid, out = sys.argv[1], sys.argv[2], sys.argv[3]
    os.makedirs(out, exist_ok=True)
    c = Collector()
    idx = 'flex_mem'
    c.apply_file(pbf, locations=True, idx=idx)
    # coverage box from the road network, ignoring stray outliers (0.05% each side) + small margin
    lats = sorted(pt[0] for w in c.ways for pt in w[1][::4])
    lons = sorted(pt[1] for w in c.ways for pt in w[1][::4])
    if lats:
        k = int(len(lats) * 0.0005)
        bbox = [lons[k] - 0.05, lats[k] - 0.05, lons[-1 - k] + 0.05, lats[-1 - k] + 0.05]
    else:
        bbox = [0, 0, 0, 0]
    cells = coverage(c, bbox)
    g = build_graph(c, os.path.join(out, pid + '.route'))
    s = build_search(c, os.path.join(out, pid + '.search'), bbox, cells)
    info = dict(id=pid, bbox=[round(x, 5) for x in bbox], vertices=g[0], edges=g[1], points=g[2], places=s[0])
    with open(os.path.join(out, pid + '.json'), 'w') as f:
        json.dump(info, f)
    print(json.dumps(info))


if __name__ == '__main__':
    main()
