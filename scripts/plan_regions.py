#!/usr/bin/env python3
"""
Plans which map packs exist: reads the Geofabrik region list and file sizes and writes regions.json.

Rules
  * every country outside Africa gets a pack
  * a country (or region) whose OSM file is bigger than LIMIT_MB is split into its Geofabrik sub-regions
    (US states, Canadian provinces, German states, Russian federal districts, ...), recursively
  * EXCLUDE: places where Google Play is not available, empty Arctic areas, tiny islands

Output: [{id, region, name, country, continent, mb}]   (region = Geofabrik path, id = unique pack id)
"""
import json, re, sys, urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = 'https://download.geofabrik.de'
LIMIT_MB = 400
CONTINENTS = {'asia': 'Asia', 'europe': 'Europe', 'north-america': 'North America', 'south-america': 'South America',
              'central-america': 'Central America', 'australia-oceania': 'Oceania'}
EXCLUDE = {
    # no Google Play
    'china', 'iran', 'north-korea', 'syria', 'cuba', 'crimean-fed-district', 'crimea',
    # empty Arctic / polar
    'greenland', 'nunavut', 'northwest-territories', 'yukon', 'antarctica',
    # tiny islands
    'tuvalu', 'nauru', 'niue', 'tokelau', 'pitcairn-islands', 'ile-de-clipperton', 'cook-islands', 'wallis-et-futuna',
    'kiribati', 'marshall-islands', 'micronesia', 'palau', 'american-oceania', 'us/us-virgin-islands',
}
# Geofabrik groups that duplicate countries listed on their own
GROUPS_OK = {'kosovo', 'azores', 'guernsey-jersey', 'isle-of-man'}
COUNTRY_NAME = {'us': 'United States', 'russia': 'Russia', 'gcc-states': 'Gulf States (GCC)',
                'malaysia-singapore-brunei': 'Malaysia, Singapore, Brunei', 'israel-and-palestine': 'Israel & Palestine',
                'haiti-and-domrep': 'Haiti & Dominican Rep.', 'ireland-and-northern-ireland': 'Ireland & N. Ireland'}

UA = {'User-Agent': 'Mozilla/5.0 (map pack planner)'}


def get(url):
    for _ in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
                return r.read().decode('utf-8', 'replace')
        except Exception as e:
            err = e
    raise err


def main(out='regions.json'):
    feats = [f['properties'] for f in json.loads(get(BASE + '/index-v1-nogeom.json'))['features']]
    by_id = {p['id']: p for p in feats}
    kids = {}
    for p in feats:
        kids.setdefault(p.get('parent') or '', []).append(p)

    def path_of(p):
        return p['urls']['pbf'].split('download.geofabrik.de/')[1].replace('-latest.osm.pbf', '')

    # file sizes are only on the html pages ("(50&nbsp;MB)" next to each .osm.pbf link)
    pages = {('/' + path_of(p)).rsplit('/', 1)[0] or '/index' for p in feats if p.get('urls', {}).get('pbf')}
    size = {}
    rx = re.compile(r'href="([^"]+)-latest\.osm\.pbf"[^<]*</a>\s*</td>\s*<td[^>]*>\s*\(([\d.,]+)&nbsp;(KB|MB|GB)\)')

    def scan(pg):
        try:
            html = get(BASE + pg + '.html')
        except Exception:
            return
        base = pg.rsplit('/', 1)[0]          # links are relative to the page's folder
        for href, v, u in rx.findall(html):
            mb = float(v.replace(',', '')) * {'KB': 1 / 1024, 'MB': 1, 'GB': 1024}[u]
            size[(href if href.startswith('/') else base + '/' + href).strip('/')] = mb
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(scan, sorted(pages)))

    packs = []

    def add(p, continent, country):
        if p['id'] in EXCLUDE:
            return
        mb = size.get(path_of(p))
        if mb and mb > LIMIT_MB and kids.get(p['id']):
            for k in kids[p['id']]:
                add(k, continent, country)
            return
        name = p['name']
        if '/' in name:                      # US states are named like 'us/new-york'
            name = name.split('/')[-1].replace('-', ' ').title().replace(' Of ', ' of ')
        packs.append(dict(id=p['id'].replace('/', '-'), region=path_of(p), name=name, country=country,
                          continent=continent, mb=round(mb or 0, 1)))

    for cid, cname in CONTINENTS.items():
        for p in kids.get(cid, []):
            pid = p['id']
            if pid.startswith('us-') or pid in ('us', 'great-britain', 'alps', 'britain-and-ireland', 'dach', 'sea'):
                continue
            is_state = pid.startswith('us/')
            if not (p.get('iso3166-1:alpha2') or is_state or pid in GROUPS_OK):
                continue
            country = 'United States' if is_state else COUNTRY_NAME.get(pid, p['name'])
            add(p, cid, country)
    for p in kids.get('russia', []):
        add(p, 'europe', 'Russia')

    # unique ids
    seen = set()
    for p in packs:
        assert p['id'] not in seen, p['id']
        seen.add(p['id'])
    packs.sort(key=lambda p: (p['continent'], p['country'], p['name']))
    json.dump(packs, open(out, 'w'), indent=0, ensure_ascii=False)
    print(len(packs), 'packs,', round(sum(p['mb'] for p in packs) / 1024, 1), 'GB of OSM data', file=sys.stderr)
    missing = [p['region'] for p in packs if not p['mb']]
    if missing:
        print('no size for', missing, file=sys.stderr)


if __name__ == '__main__':
    main(*sys.argv[1:])
