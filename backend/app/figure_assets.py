"""Immutable image-asset metadata and Markdown-reference integrity checks."""
from __future__ import annotations
import hashlib
import re
from app.errors import ProcessingError

FILENAME = re.compile(r'p[0-9]{4}-figure-[A-Za-z0-9_-]{1,70}\.png')
REFERENCE = re.compile(r'!\[[^\]\n]*\]\(images/(p[0-9]{4}-figure-[A-Za-z0-9_-]{1,70}\.png)\)')


def invalid(message='Archived figure metadata is invalid.'):
    return ProcessingError('RESULT_CORRUPT', message, 503)


def validate_metadata(data, pages):
    if type(data) is not dict or set(data) != {'version', 'figures', 'warnings'} or data['version'] != 1 or type(data['figures']) is not list or type(data['warnings']) is not list:
        raise invalid()
    files = set()
    if len(data['figures']) > 500 or any(type(w) is not str or len(w) > 400 for w in data['warnings']):
        raise invalid()
    for entry in data['figures']:
        if type(entry) is not dict or set(entry) != {'filename', 'page', 'sha256', 'bytes', 'width', 'height', 'kind', 'caption'}:
            raise invalid()
        name = entry['filename']
        if (type(name) is not str or not FILENAME.fullmatch(name) or name in files or type(entry['page']) is not int
            or not 1 <= entry['page'] <= len(pages) or not name.startswith(f'p{entry["page"]:04d}-')
            or type(entry['sha256']) is not str or not re.fullmatch('[0-9a-f]{64}', entry['sha256'])
            or type(entry['bytes']) is not int or not 0 < entry['bytes'] <= 20_000_000
            or any(type(entry[n]) is not int or not 0 < entry[n] <= 20000 for n in ('width','height'))
            or any(type(entry[n]) is not str or len(entry[n]) > 1024 for n in ('kind','caption'))):
            raise invalid()
        files.add(name)
    per_page_links = [REFERENCE.findall(page.markdown) for page in pages]
    if any(len(p) != len(set(p)) for p in per_page_links):
        raise invalid('Duplicate figure image reference in a page.')
    listed = [f for link in per_page_links for f in link]
    if len(listed) != len(set(listed)) or set(listed) != files:
        raise invalid('Figure assets differ from Markdown references.')
    if any(re.search(r'!\[[^\]\n]*\]\((?:images/|https?://|data:)', page.markdown) and
           len(REFERENCE.findall(page.markdown)) != len(re.findall(r'!\[[^\]\n]*\]\(', page.markdown)) for page in pages):
        raise invalid('Untrusted Markdown image reference.')
    if any(e['filename'] not in per_page_links[e['page']-1] for e in data['figures']):
        raise invalid('Figure reference belongs to a different page.')
    return data


def metadata_from_result(result):
    figures=[]
    for asset in result.assets:
        figures.append(dict(filename=asset.path.removeprefix('images/'), page=asset.page,
            sha256=asset.sha256, bytes=len(asset.data), width=asset.width, height=asset.height,
            kind=asset.kind, caption=asset.caption))
    data={'version': 1, 'figures': figures, 'warnings': list(result.warnings)}
    return validate_metadata(data, result.pages)


def verify_png(data, entry):
    if len(data) != entry['bytes'] or hashlib.sha256(data).hexdigest() != entry['sha256'] or not data.startswith(b'\x89PNG\r\n\x1a\n'):
        raise invalid('Archived figure checksum, size or PNG header is inconsistent.')
    return data
