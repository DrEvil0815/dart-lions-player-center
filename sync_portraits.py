#!/usr/bin/env python3
"""Dart Lions – SportMember portrait sync, independent from all statistic importers.

Fail closed on ambiguous player/image matching. Service key stays in GitHub Actions.
"""
import hashlib
import io
import json
import os
import re
import sys
import time
import unicodedata
from collections import defaultdict
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageOps, UnidentifiedImageError

BASE = 'https://www.dart-lions.de/klub/dart-lions-tsv-09-graefenberg/hold/'
TEAMS = {
    'Dart Lions I': BASE + 'dart-lions-i',
    'Dart Lions II': BASE + 'dart-lions-ii--2',
    'Dart Lions III': BASE + 'dart-lions-iii--2',
}
MAX_DOWNLOAD = 6 * 1024 * 1024
MAX_STORAGE = 2 * 1024 * 1024
TIMEOUT = 35
SESSION = requests.Session()
SESSION.headers.update({'User-Agent': 'DartLionsPlayerCenterPortraitSync/1.0 (+https://www.dart-lions.de)'})


def norm(text):
    text = unicodedata.normalize('NFKC', text or '').casefold()
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def api(method, path, *, payload=None, data=None, headers=None, params=None, expected=(200, 201, 204)):
    url = os.environ['SUPABASE_URL'].strip().rstrip('/').removesuffix('/rest/v1') + path
    key = os.environ['SUPABASE_SERVICE_ROLE_KEY'].strip()
    h = {'apikey': key}
    # Legacy JWT service keys require Bearer. Modern sb_secret_ keys use apikey.
    if not key.startswith('sb_secret_'):
        h['Authorization'] = 'Bearer ' + key
    if payload is not None:
        h['Content-Type'] = 'application/json'
    h.update(headers or {})
    for attempt in range(3):
        try:
            response = SESSION.request(method, url, json=payload, data=data, headers=h,
                                       params=params, timeout=TIMEOUT)
            if response.status_code in expected:
                if not response.content:
                    return None
                if 'application/json' in response.headers.get('Content-Type', ''):
                    return response.json()
                return response.text
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f'API {method} {path}: HTTP {response.status_code} – {response.text[:350]}')
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


def fetch_member_rows():
    # The service role is used ONLY in this server-side workflow.
    out = []
    for offset in range(0, 10000, 500):
        rows = api('GET', '/rest/v1/pc_members', params={
            'select': 'id,first_name,last_name,display_name,is_active,is_child,left_at,avatar_path',
            'limit': 500, 'offset': offset,
            'order': 'id.asc',
        })
        out.extend(rows)
        if len(rows) < 500:
            break
    return out


def allowed_image_url(raw):
    url = urljoin('https://www.dart-lions.de/', raw or '')
    p = urlsplit(url)
    return url if p.scheme == 'https' and p.hostname in ('www.holdsport.dk', 'holdsport.dk') and p.port in (None, 443) else None


def candidates_from_page(html, members):
    """Find images in the *smallest* DOM container carrying exactly one known player.

    Never assign by list position or proximity alone. When DOM markup does not
    provide a unique name-to-image association, report instead of guessing.
    """
    soup = BeautifulSoup(html, 'html.parser')
    names = defaultdict(set)
    for m in members:
        full = norm((m.get('first_name') or '') + ' ' + (m.get('last_name') or ''))
        disp = norm(m.get('display_name'))
        if full:
            names[full].add(m['id'])
        if disp and len(disp.split()) >= 2:
            names[disp].add(m['id'])
    result = defaultdict(set)
    warning = []
    image_elements = [im for im in soup.find_all('img') if 'profile image' in norm(im.get('alt'))]
    for im in image_elements:
        raw = im.get('src') or im.get('data-src') or im.get('data-original')
        if not raw and im.get('srcset'):
            raw = im['srcset'].split(',')[0].strip().split(' ')[0]
        url = allowed_image_url(raw)
        if not url:
            warning.append('Ein Profilbild hat keine erlaubte HoldSport-Adresse')
            continue
        found = set()
        for ancestor in list(im.parents)[:9]:
            text = norm(ancestor.get_text(' ', strip=True))
            # Avoid associating a picture with a name from a whole team roster.
            profile_count = sum('profile image' in norm(x.get('alt')) for x in ancestor.find_all('img'))
            if profile_count != 1 or len(text) > 260:
                continue
            matches = {member_id for name, ids in names.items()
                       if re.search(r'(?<!\w)' + re.escape(name) + r'(?!\w)', text)
                       for member_id in ids}
            if len(matches) == 1:
                found = matches
                break
        if found:
            result[next(iter(found))].add(url)
        else:
            warning.append('Profilbild nicht zweifelsfrei mit einem Mitglied verknuepfbar')
    return result, warning, len(image_elements)


def fetch_bytes(url):
    if not allowed_image_url(url):
        raise ValueError('Unzulaessiger Bilderhost')
    with SESSION.get(url, timeout=TIMEOUT, stream=True, allow_redirects=False) as response:
        response.raise_for_status()
        if 300 <= response.status_code < 400:
            raise ValueError('Bild-Redirect nicht erlaubt')
        buf = io.BytesIO()
        for block in response.iter_content(chunk_size=65536):
            buf.write(block)
            if buf.tell() > MAX_DOWNLOAD:
                raise ValueError('Bilddatei zu gross')
        return buf.getvalue()


def image_as_jpeg(raw):
    Image.MAX_IMAGE_PIXELS = 22_000_000
    try:
        with Image.open(io.BytesIO(raw)) as src:
            src.verify()
        with Image.open(io.BytesIO(raw)) as src:
            src = ImageOps.exif_transpose(src)
            if src.width < 100 or src.height < 100:
                raise ValueError('Bildaufloesung zu niedrig')
            src.thumbnail((1000, 1000), Image.Resampling.LANCZOS)
            if src.mode in ('RGBA', 'LA') or 'transparency' in src.info:
                rgba = src.convert('RGBA')
                background = Image.new('RGB', rgba.size, (24, 24, 24))
                background.paste(rgba, mask=rgba.getchannel('A'))
                src = background
            else:
                src = src.convert('RGB')
            stream = io.BytesIO()
            src.save(stream, 'JPEG', quality=85, optimize=True)
            value = stream.getvalue()
            if len(value) > MAX_STORAGE:
                raise ValueError('Optimiertes Portrait ueber 2 MB')
            return value
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError('Ungueltige Bilddaten') from exc


def main():
    members = fetch_member_rows()
    eligible = {m['id']: m for m in members if m['is_active'] and not m['is_child']
                and (not m['left_at'] or m['left_at'] > time.strftime('%Y-%m-%d'))}
    print(f'Mitglieder: {len(members)}, fuer Bildimport berechtigt: {len(eligible)}')
    chosen = defaultdict(set)
    issues = []
    total_images = 0
    for team, url in TEAMS.items():
        try:
            res = SESSION.get(url, timeout=TIMEOUT)
            res.raise_for_status()
            found, warnings, images = candidates_from_page(res.text, eligible.values())
            total_images += images
            for member_id, urls in found.items():
                chosen[member_id].update(urls)
            issues.extend(f'{team}: {w}' for w in warnings)
            print(f'{team}: {images} Portrait-Tags, {len(found)} eindeutige Zuordnungen, {len(warnings)} Hinweise')
        except requests.RequestException as exc:
            issues.append(f'{team}: Homepage nicht abrufbar ({type(exc).__name__})')
    if total_images == 0:
        raise RuntimeError('Keine Portrait-Tags gefunden. Abbruch ohne Aenderungen.')
    states = api('GET', '/rest/v1/pc_portrait_sync_state', params={
        'select': 'member_id,source_sha256,portrait_path', 'limit': 1000,
    })
    prior = {x['member_id']: x for x in states}
    stats = defaultdict(int)
    for member_id, sources in sorted(chosen.items()):
        m = eligible[member_id]
        label = (m.get('first_name') or '') + ' ' + (m.get('last_name') or '')
        expected_path = member_id + '/portrait-sync.jpg'
        if len(sources) != 1:
            issues.append(f'{label}: widerspruechliche Bildadressen – manuelle Pruefung')
            stats['ambiguous'] += 1
            continue
        if m['avatar_path'] not in (None, expected_path):
            stats['manual_preserved'] += 1
            continue
        url = next(iter(sources))
        try:
            image = image_as_jpeg(fetch_bytes(url))
            digest = hashlib.sha256(image).hexdigest()
            prev = prior.get(member_id)
            if prev and prev['source_sha256'] == digest and prev['portrait_path'] == expected_path:
                if m['avatar_path'] == expected_path:
                    stats['unchanged'] += 1
                    continue
                # If avatar was removed manually, do NOT silently reinstate it.
                stats['previously_removed'] += 1
                continue
            api('POST', '/storage/v1/object/pc-portraits/' + expected_path,
                data=image, headers={'Content-Type': 'image/jpeg', 'x-upsert': 'true',
                                     'cache-control': '3600'}, expected=(200, 201))
            result = api('POST', '/rest/v1/rpc/pc_sync_assign_portrait', payload={
                'p_member_id': member_id, 'p_sha256': digest, 'p_source_url': url,
            })
            if result == 'assigned':
                stats['assigned'] += 1
            else:
                stats[result or 'unassigned'] += 1
        except (ValueError, RuntimeError, requests.RequestException, OSError) as exc:
            issues.append(f'{label}: {str(exc)[:200]}')
            stats['errors'] += 1
    matched = set(chosen)
    print('Ergebnis:', json.dumps(dict(stats), ensure_ascii=False))
    print(f'Gesehene Bilder: {total_images}; erkannte Mitglieder: {len(matched)}')
    for message in issues[:80]:
        print('PRUEFEN:', message)
    if issues:
        print(f'Insgesamt {len(issues)} Hinweis(e). Keine unsicheren Zuordnungen geschrieben.')
    # Any partial failure should be visible in job logs, but all other sync jobs
    # are independent and still finish. In particular, no database deletes occur.
    if stats['errors'] or total_images < 10:
        sys.exit(1)


if __name__ == '__main__':
    main()
