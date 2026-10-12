#!/usr/bin/env python3
"""Build an app-compatible SQLite catalog from LibriVox and Archive popularity.

python backend/build_catalog.py --help
Python 3.10+, standard library only. No changes are made to Flutter assets.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, unquote
from urllib.request import Request, urlopen

if __package__:
    from .catalog_schema import SCHEMA, build_catalog_authors
else:
    from catalog_schema import SCHEMA, build_catalog_authors

HERE = Path(__file__).resolve().parent
LIBRIVOX = 'https://librivox.org/api/feed/audiobooks/'
ARCHIVE = 'https://archive.org/advancedsearch.php'
USER_AGENT = 'MeroCatalogBuilder/1.0 (LibriVox catalog synchronization)'
ARCHIVE_FIELDS = ['identifier', 'downloads', 'avg_rating', 'num_reviews', 'language']


class BuildError(Exception):
    pass


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class JsonClient:
    def __init__(self, cache, *, delay=1.0, timeout=45, retries=4):
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.delay, self.timeout, self.retries = delay, timeout, retries
        self.last_request = 0.0

    def get(self, endpoint, params, *, librivox=False):
        url = endpoint + '?' + urlencode(params, doseq=True)
        file = self.cache / (hashlib.sha256(url.encode()).hexdigest() + '.json')
        if file.exists():
            try:
                saved = json.loads(file.read_text(encoding='utf-8'))
                if saved['url'] == url and isinstance(saved['data'], dict):
                    return saved['data']
            except (ValueError, KeyError, TypeError):
                pass  # Re-fetch a corrupt/incomplete cache entry.
        for attempt in range(self.retries + 1):
            time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            retry_after = 0.0
            try:
                request = Request(url, headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'})
                with urlopen(request, timeout=self.timeout) as response:
                    data = json.load(response)
                if not isinstance(data, dict) or 'error' in data:
                    raise BuildError(f'API error from {endpoint}: {str(data)[:250]}')
            except HTTPError as error:
                # LibriVox's documented empty feed is HTTP 404 + this JSON body.
                # An HTML 404 or other error must never masquerade as completion.
                payload = error.read().decode('utf-8', errors='replace')
                try:
                    body = json.loads(payload)
                except ValueError:
                    body = None
                if librivox and error.code == 404 and body == {'error': 'Audiobooks could not be found'}:
                    data = {'books': []}
                else:
                    if error.code not in (408, 429, 500, 502, 503, 504) or attempt == self.retries:
                        raise BuildError(f'HTTP {error.code} from {endpoint}: {payload[:160]}') from error
                    header = error.headers.get('Retry-After', '0')
                    try:
                        retry_after = float(header)
                    except ValueError:
                        try:
                            retry_after = max(0, parsedate_to_datetime(header).timestamp() - time.time())
                        except (ValueError, TypeError):
                            retry_after = 0
                    data = None
            except (URLError, TimeoutError, OSError, ValueError, BuildError) as error:
                if attempt == self.retries:
                    raise BuildError(f'Could not fetch {endpoint}: {error}') from error
                data = None
            if data is not None:
                atomic_json(file, {'url': url, 'fetched_at': datetime.now(timezone.utc).isoformat(), 'data': data})
                return data
            wait = max(retry_after, min(30, 2 ** attempt))
            print(f'Retrying {endpoint} in {wait:g}s', flush=True)
            time.sleep(wait)
        raise BuildError('Retry limit exceeded')


def text(value):
    if not isinstance(value, str):
        return ''
    return html.unescape(value.replace(r'\/', '/')).strip()


def archive_id(value):
    url = urlsplit(text(value))
    if url.hostname not in ('archive.org', 'www.archive.org'):
        return None
    parts = unquote(url.path).strip('/').split('/')
    if len(parts) < 2 or parts[0] not in ('details', 'download', 'compress'):
        return None
    return parts[1] if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', parts[1]) else None


def positive_int(value):
    if isinstance(value, bool) or not re.fullmatch(r'[0-9]+', str(value)) or int(value) <= 0:
        raise BuildError(f'Expected a positive integer, got {value!r}')
    return int(value)


def languages(value):
    values = value if isinstance(value, list) else [value]
    return list(dict.fromkeys(text(item) for item in values if text(item)))


def store_archive_metadata(db, doc):
    identifier = doc.get('identifier')
    if not isinstance(identifier, str) or not identifier:
        raise BuildError('Archive returned metadata without an identifier')
    counts = []
    for key in ('downloads', 'num_reviews'):
        value = doc.get(key)
        if value is not None and (isinstance(value, bool) or not re.fullmatch(r'[0-9]+', str(value))):
            raise BuildError(f'Invalid {"download count" if key == "downloads" else "review count"} for {identifier}')
        counts.append(None if value is None else int(value))
    raw_rating = doc.get('avg_rating')
    try:
        rating = float(raw_rating) if raw_rating is not None and not isinstance(raw_rating, bool) else None
    except (ValueError, TypeError):
        rating = None
    if rating is not None and (not math.isfinite(rating) or not 0 <= rating <= 5):
        rating = None
    db.execute('INSERT OR REPLACE INTO source_downloads VALUES (?,?,?,?,?)', (
        identifier, counts[0], rating, counts[1], json.dumps(languages(doc.get('language')), ensure_ascii=False),
    ))


def parse_book(raw):
    if not isinstance(raw, dict):
        raise BuildError('LibriVox returned a non-object book')
    project_id = positive_int(raw.get('id'))
    title = text(raw.get('title'))
    if not title or not isinstance(raw.get('authors'), list) or not isinstance(raw.get('genres'), list):
        raise BuildError(f'Project {project_id}: missing title/authors/genres; check API fields')
    names = []
    for author in raw['authors']:
        if not isinstance(author, dict):
            raise BuildError(f'Project {project_id}: invalid author')
        name = ' '.join(filter(None, [text(author.get('first_name')), text(author.get('last_name'))]))
        if name and name not in names:
            names.append(name)
    genres = {positive_int(genre.get('id')) for genre in raw['genres'] if isinstance(genre, dict)}
    if len([g for g in raw['genres'] if isinstance(g, dict)]) != len(raw['genres']):
        raise BuildError(f'Project {project_id}: invalid genre')
    return project_id, archive_id(raw.get('url_iarchive')), title, ', '.join(names), genres, languages(raw.get('language')), names


def collect_librivox(db, client, *, page_size=500, max_pages=None):
    offset = 0
    previous_id = 0
    skipped = []
    pages = 0
    while True:
        data = client.get(LIBRIVOX, {
            'format': 'json', 'extended': '1', 'fields': 'id,title,authors,url_iarchive,genres,language',
            'sort_order': 'asc', 'limit': page_size, 'offset': offset,
        }, librivox=True)
        records = data.get('books')
        if not isinstance(records, list):
            raise BuildError('LibriVox response has no books list')
        if not records:
            break
        if len(records) > page_size:
            raise BuildError('LibriVox ignored the requested page limit')
        with db:
            for raw in records:
                project, identifier, title, author, genres, book_languages, author_names = parse_book(raw)
                if project <= previous_id:
                    raise BuildError('LibriVox IDs repeated or moved backwards; start a fresh cache snapshot')
                previous_id = project
                if identifier is None:
                    skipped.append(project)
                    continue
                db.execute('INSERT INTO source_projects VALUES (?,?,?,?,?)',
                           (project, identifier, title, author, json.dumps(book_languages, ensure_ascii=False)))
                db.executemany('INSERT INTO source_project_authors VALUES (?,?,?)',
                               ((project, position, name) for position, name in enumerate(author_names)))
                db.executemany('INSERT OR IGNORE INTO source_memberships VALUES (?,?)', ((project, genre) for genre in genres))
        pages += 1
        offset += len(records)
        print(f'LibriVox: {offset:,} projects, page {pages}', flush=True)
        if max_pages is not None and pages >= max_pages:
            return {'projects': offset, 'skipped_projects_without_archive_id': skipped, 'partial': True}
        # Always request the next offset, even after a short nonempty page.
    if offset == 0:
        raise BuildError('Refusing to publish an empty LibriVox catalog')
    return {'projects': offset, 'skipped_projects_without_archive_id': skipped, 'partial': False}


def collect_downloads(db, client):
    page = 1
    count = 0
    total = None
    while total is None or count < total:
        data = client.get(ARCHIVE, {
            'q': 'collection:librivoxaudio AND mediatype:audio',
            'fl[]': ARCHIVE_FIELDS, 'sort[]': 'identifier asc',
            'rows': 1000, 'page': page, 'output': 'json',
        })
        response = data.get('response', {})
        docs, reported = response.get('docs'), response.get('numFound')
        if not isinstance(docs, list) or type(reported) is not int or reported < 0:
            raise BuildError('Invalid Archive search result')
        if total is not None and total != reported:
            raise BuildError('Archive collection changed while paging; start a fresh cache snapshot')
        total = reported
        if page == 1 and total > 10000:
            # Advanced Search disallows deep paging. Its documented bulk mode
            # accepts all rows when page/sort are omitted. The initial cached
            # page gives us the required size; never publish a truncated export.
            data = client.get(ARCHIVE, {
                'q': 'collection:librivoxaudio AND mediatype:audio',
                'fl[]': ARCHIVE_FIELDS, 'rows': total, 'output': 'json',
            })
            response = data.get('response', {})
            docs, reported = response.get('docs'), response.get('numFound')
            if type(reported) is not int or reported != total:
                raise BuildError('Archive collection changed during export; start a fresh cache snapshot')
            if not isinstance(docs, list) or len(docs) != total:
                raise BuildError('Archive bulk export is incomplete; no catalog was published')
        if not docs:
            raise BuildError('Archive ended before all popularity records were fetched')
        with db:
            for doc in docs:
                if not isinstance(doc, dict):
                    raise BuildError('Archive returned a non-object metadata document')
                identifier = doc.get('identifier')
                if not isinstance(identifier, str) or not identifier:
                    raise BuildError('Archive returned metadata without an identifier')
                # Archive sorts mixed-case identifiers differently from Python's
                # Unicode string comparison. Trust server ordering, but verify
                # exact IDs remain unique across every page before publication.
                if db.execute('SELECT 1 FROM source_downloads WHERE archive_id=?', (identifier,)).fetchone():
                    raise BuildError(f'Archive repeated identifier {identifier!r} across results; start a fresh cache snapshot')
                store_archive_metadata(db, doc)
        count += len(docs)
        print(f'Archive popularity: {count:,}/{total:,}', flush=True)
        page += 1
    return count


def collect_sample_downloads(db, client):
    """A sample build only needs popularity for its handful of Archive IDs."""
    ids = [row[0] for row in db.execute('SELECT DISTINCT archive_id FROM source_projects ORDER BY archive_id')]
    batches, batch, size = [], [], 0
    for identifier in ids:
        if batch and (size + len(identifier) + 6 > 1400 or len(batch) >= 40):
            batches.append(batch)
            batch, size = [], 0
        batch.append(identifier)
        size += len(identifier) + 6
    if batch:
        batches.append(batch)
    count = 0
    for batch in batches:
        data = client.get(ARCHIVE, {
            'q': 'identifier:(' + ' OR '.join('"' + identifier + '"' for identifier in batch) + ')',
            'fl[]': ARCHIVE_FIELDS, 'rows': len(batch), 'page': 1, 'output': 'json',
        })
        docs = data.get('response', {}).get('docs')
        if not isinstance(docs, list):
            raise BuildError('Invalid Archive sample statistics')
        with db:
            for doc in docs:
                identifier = doc.get('identifier')
                if identifier not in batch:
                    raise BuildError('Archive returned an unexpected sample identifier')
                store_archive_metadata(db, doc)
                count += 1
    return count


def read_profiles(path):
    if path is None:
        return []
    path = Path(path).resolve()
    if not path.is_file():
        raise BuildError(f'Author profiles database not found: {path}')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        return db.execute('SELECT pop_id,author_name,other_names FROM authors ORDER BY pop_id').fetchall()


def materialize(db, config, profiles, popularity):
    # For multiple LibriVox projects sharing an Archive item, retain the earliest
    # project's display metadata and union the genre memberships.
    db.execute('''INSERT INTO books(archive_id,title,author,languages_json,avg_rating,num_reviews,downloads)
      SELECT p.archive_id,p.title,p.author,
        CASE WHEN p.languages_json != '[]' THEN p.languages_json ELSE COALESCE(d.languages_json,'[]') END,
        d.avg_rating,d.num_reviews,d.downloads FROM source_projects p
      JOIN (SELECT archive_id,MIN(project_id) AS first_id FROM source_projects GROUP BY archive_id) first
        ON p.project_id=first.first_id
      LEFT JOIN source_downloads d ON d.archive_id=p.archive_id ORDER BY p.project_id''')
    genre_ids = list(dict.fromkeys(positive_int(g) for g in config['source_genre_ids']))
    new_ids = sorted({r[0] for r in db.execute('SELECT DISTINCT genre_id FROM source_memberships')} - set(genre_ids))
    genre_ids.extend(new_ids)
    for order, genre in enumerate(genre_ids, 1):
        db.execute('INSERT INTO genres VALUES (?,?)', (genre, order))
        order_by = 'd.downloads IS NULL, d.downloads DESC, b.id' if popularity == 'downloads' else 'b.id'
        results = db.execute(f'''SELECT DISTINCT b.id FROM source_memberships m
          JOIN source_projects p ON p.project_id=m.project_id
          JOIN books b ON b.archive_id=p.archive_id
          LEFT JOIN source_downloads d ON d.archive_id=b.archive_id
          WHERE m.genre_id=? ORDER BY {order_by}''', (genre,))
        db.executemany('INSERT INTO genre_books VALUES (?,?,?)',
                       ((genre, rank, row[0]) for rank, row in enumerate(results, 1)))
    db.execute("INSERT INTO book_search(book_search) VALUES('rebuild')")
    db.execute("INSERT INTO book_search(book_search) VALUES('optimize')")
    credits = db.execute('''SELECT b.id,a.name FROM source_project_authors a
      JOIN source_projects p ON p.project_id=a.project_id
      JOIN books b ON b.archive_id=p.archive_id
      ORDER BY b.id,p.project_id,a.position''')
    build_catalog_authors(db, profiles, credits=credits)
    return new_ids


def validate(db):
    if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchall():
        raise BuildError('SQLite integrity/foreign-key check failed')
    for table in ('book_search', 'catalog_author_search'):
        db.execute(f"INSERT INTO {table}({table}) VALUES('integrity-check')")
    if db.execute('''SELECT genre_id FROM genre_books GROUP BY genre_id
        HAVING MIN(popularity_rank) != 1 OR MAX(popularity_rank) != COUNT(*)''').fetchall():
        raise BuildError('Noncontiguous genre ranks')
    if db.execute('''SELECT 1 FROM catalog_author_books a
      LEFT JOIN books b ON b.id=a.book_id LEFT JOIN catalog_authors c ON c.id=a.author_id
      WHERE b.id IS NULL OR c.id IS NULL LIMIT 1''').fetchone():
        raise BuildError('Invalid author membership')


def build(output, client, *, config, profiles=(), page_size=500, max_pages=None, popularity='downloads', force=False):
    output = Path(output).resolve()
    if output.exists() and not force:
        raise BuildError(f'Output exists: {output}; use --force to replace it after successful validation')
    output.parent.mkdir(parents=True, exist_ok=True)
    lock = Path(str(output) + '.lock')
    try:
        lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise BuildError(f'Another build may be running; lock: {lock}') from error
    os.close(lock_fd)
    temporary = None
    try:
        fd, name = tempfile.mkstemp(dir=output.parent, prefix=output.stem + '-', suffix='.tmp')
        os.close(fd)
        temporary = Path(name)
        with closing(sqlite3.connect(temporary)) as db:
            db.executescript(SCHEMA)
            db.executescript('''
              CREATE TABLE source_projects(project_id INTEGER PRIMARY KEY, archive_id TEXT, title TEXT, author TEXT,languages_json TEXT);
              CREATE INDEX source_project_archive ON source_projects(archive_id);
              CREATE TABLE source_project_authors(project_id INTEGER,position INTEGER,name TEXT,PRIMARY KEY(project_id,position));
              CREATE TABLE source_memberships(project_id INTEGER,genre_id INTEGER,PRIMARY KEY(project_id,genre_id));
              CREATE INDEX source_member_genre ON source_memberships(genre_id,project_id);
              CREATE TABLE source_downloads(archive_id TEXT PRIMARY KEY,downloads INTEGER,avg_rating REAL,num_reviews INTEGER,languages_json TEXT);
            ''')
            report = collect_librivox(db, client, page_size=page_size, max_pages=max_pages)
            if popularity == 'downloads':
                report['archive_documents'] = (collect_sample_downloads(db, client)
                    if report['partial'] else collect_downloads(db, client))
            with db:
                report['new_genre_ids'] = materialize(db, config, profiles, popularity)
                for key, table in [('books', 'books'), ('genres', 'genres'), ('genre_entries', 'genre_books'), ('authors', 'catalog_authors')]:
                    report[key] = db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                if report['books'] == 0:
                    raise BuildError('No usable Archive-linked books; refusing to publish')
                report['missing_download_counts'] = [r[0] for r in db.execute('''SELECT b.archive_id FROM books b
                  LEFT JOIN source_downloads d ON d.archive_id=b.archive_id WHERE d.downloads IS NULL ORDER BY b.id''')] if popularity == 'downloads' else []
                report['duplicate_archive_projects'] = db.execute('SELECT COUNT(*) FROM source_projects').fetchone()[0] - report['books']
                digest = hashlib.sha256()
                for row in db.execute('SELECT * FROM source_projects ORDER BY project_id'):
                    digest.update(json.dumps(row, ensure_ascii=False).encode('utf-8'))
                for row in db.execute('SELECT * FROM source_memberships ORDER BY project_id,genre_id'):
                    digest.update(json.dumps(row).encode())
                for row in db.execute('SELECT * FROM source_project_authors ORDER BY project_id,position'):
                    digest.update(json.dumps(row, ensure_ascii=False).encode('utf-8'))
                metadata = {
                    'schema_version': '2', 'source': LIBRIVOX, 'source_sha256': digest.hexdigest(),
                    'built_at': datetime.now(timezone.utc).isoformat(), 'partial': str(report['partial']).lower(),
                    'search_index': 'fts4/unicode61/external-content',
                    'author_index': 'individual-librivox-authors-v1',
                    'popularity': 'Archive downloads descending; missing counts last; catalog ID breaks ties' if popularity == 'downloads' else 'LibriVox project ID ascending (NOT popularity)',
                    'genre_groups': json.dumps(config['display_groups'], ensure_ascii=False),
                    **{k: str(report[k]) for k in ('books', 'genres', 'genre_entries', 'authors')},
                }
                db.executemany('INSERT INTO catalog_metadata VALUES (?,?)', metadata.items())
                for table in ('source_memberships', 'source_project_authors', 'source_projects', 'source_downloads'):
                    db.execute(f'DROP TABLE {table}')
                validate(db)
            db.execute('VACUUM')
            validate(db)
            db.commit()
        os.replace(temporary, output)
        report.update(output=str(output), database_bytes=output.stat().st_size,
                      sha256=hashlib.sha256(output.read_bytes()).hexdigest(), popularity=popularity)
        atomic_json(Path(str(output) + '.report.json'), report)
        return report
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='Default: backend/output/genre_catalog.sqlite3')
    parser.add_argument('--genre-config', type=Path, default=HERE / 'genre_config.json')
    default_profiles = HERE.parent / 'assets/database/authors_200.sqlite3'
    parser.add_argument('--authors-db', type=Path, default=default_profiles if default_profiles.exists() else None,
                        help='Optional existing profiles database; preserves pop_id/alias links')
    parser.add_argument('--no-author-profiles', action='store_true')
    parser.add_argument('--cache-dir', type=Path, default=HERE / '.cache' / datetime.now(timezone.utc).strftime('%Y-%m-%d'),
                        help='Cached responses resume interrupted runs; use a new directory for a fresh snapshot')
    parser.add_argument('--page-size', type=int, choices=range(1, 501), metavar='1..500', default=500)
    parser.add_argument('--max-pages', type=int, help='Explicit partial/sample build; defaults to a separate sample output filename')
    parser.add_argument('--popularity', choices=['downloads', 'librivox-id'], default='downloads')
    parser.add_argument('--delay', type=float, default=1.0, help='Minimum seconds between HTTP requests (default: 1)')
    parser.add_argument('--timeout', type=float, default=45)
    parser.add_argument('--retries', type=int, default=4)
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    if args.delay < 0 or args.timeout <= 0 or args.retries < 0 or (args.max_pages is not None and args.max_pages < 1):
        parser.error('Invalid delay, timeout, retries or max-pages')
    output = args.output or HERE / 'output' / ('genre_catalog.sample.sqlite3' if args.max_pages else 'genre_catalog.sqlite3')
    try:
        config = json.loads(args.genre_config.read_text(encoding='utf-8'))
        if not isinstance(config.get('source_genre_ids'), list) or not isinstance(config.get('display_groups'), dict):
            raise BuildError('Invalid genre config')
        known = set(config['source_genre_ids'])
        if any(not isinstance(ids, list) or not set(ids) <= known for ids in config['display_groups'].values()):
            raise BuildError('Display genres refer to unconfigured IDs')
        profiles_path = None if args.no_author_profiles else args.authors_db
        protected = [args.genre_config, profiles_path, HERE.parent / 'assets/database/genre_catalog.sqlite3']
        if output.resolve() in [p.resolve() for p in protected if p is not None]:
            raise BuildError('Output must not overwrite app assets or input files')
        client = JsonClient(args.cache_dir, delay=args.delay, timeout=args.timeout, retries=args.retries)
        report = build(output, client, config=config, profiles=read_profiles(profiles_path), page_size=args.page_size,
                       max_pages=args.max_pages, popularity=args.popularity, force=args.force)
        print(json.dumps({k: v for k, v in report.items() if not isinstance(v, list)}, indent=2))
        print(f"Validation passed. Full report: {output}.report.json")
    except (BuildError, sqlite3.Error, OSError, ValueError) as error:
        print(f'Build failed: {error}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Interrupted. Existing output is intact; cached responses can be reused.', file=sys.stderr)
        return 130
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
