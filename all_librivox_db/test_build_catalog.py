import io
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from backend.build_catalog import (
    ARCHIVE, LIBRIVOX, BuildError, JsonClient, archive_id, build, parse_book, collect_downloads,
)


def book(project, identifier, title, genres, author='Writer', language='English'):
    return {'id': str(project), 'title': title,
            'url_iarchive': f'https://www.archive.org/details/{identifier}',
            'authors': [{'first_name': '', 'last_name': author}], 'language': language,
            'genres': [{'id': str(g), 'name': 'Genre'} for g in genres]}


class Feed:
    def __init__(self, pages, docs=None):
        self.pages = pages
        self.docs = docs or []
        self.calls = []

    def get(self, endpoint, params, **kwargs):
        self.calls.append((endpoint, params))
        if endpoint == LIBRIVOX:
            return {'books': self.pages.get(params['offset'], [])}
        return {'response': {'numFound': len(self.docs), 'docs': self.docs}}


class CatalogBuildTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / 'catalog.sqlite3'
        self.config = {'source_genre_ids': [1, 2, 3], 'display_groups': {'Children': [1, 2]}}

    def test_memberships_popularity_deduplication_unicode_and_indexes(self):
        feed = Feed({
            0: [book(1, 'alice', 'Alice &amp; friends', [1, 2]), book(2, 'cafe', 'Café', [1], 'Émile Zola')],
            2: [book(3, 'alice', 'Duplicate edition credit', [3]), book(4, 'unknown', 'Unknown downloads', [1])],
        }, [{'identifier': 'alice', 'downloads': 5}, {'identifier': 'cafe', 'downloads': 50}])
        report = build(self.output, feed, config=self.config, page_size=2,
                       profiles=[(1, 'Émile Zola', '["Emile Zola"]')])
        self.assertFalse(report['partial'])
        self.assertEqual(report['books'], 3)
        self.assertEqual(report['duplicate_archive_projects'], 1)
        self.assertEqual(report['missing_download_counts'], ['unknown'])
        with closing(sqlite3.connect(self.output)) as db:
            rows = db.execute('''SELECT b.archive_id FROM genre_books g JOIN books b ON b.id=g.book_id
              WHERE genre_id=1 ORDER BY popularity_rank''').fetchall()
            self.assertEqual(rows, [('cafe',), ('alice',), ('unknown',)])
            self.assertEqual(db.execute("SELECT title FROM books WHERE archive_id='alice'").fetchone()[0], 'Alice & friends')
            self.assertEqual(db.execute("SELECT title FROM book_search WHERE book_search MATCH '\"cafe*\"'").fetchall(), [('Café',)])
            self.assertEqual(db.execute("SELECT name FROM catalog_author_search WHERE catalog_author_search MATCH '\"emile*\"'").fetchall(), [('Émile Zola',)])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM genres').fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'source_%'").fetchall(), [])
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_failure_does_not_replace_old_output_or_leave_lock(self):
        self.output.write_bytes(b'previous published database')
        feed = Feed({0: [book(1, 'one', 'One', [1])], 1: [book(1, 'one', 'One', [1])]})
        with self.assertRaisesRegex(BuildError, 'repeated'):
            build(self.output, feed, config=self.config, force=True, page_size=1, popularity='librivox-id')
        self.assertEqual(self.output.read_bytes(), b'previous published database')
        self.assertFalse(Path(str(self.output) + '.lock').exists())
        self.assertFalse(list(self.output.parent.glob('*.tmp')))

    def test_partial_no_popularity_mode_and_new_genre(self):
        feed = Feed({0: [book(1, 'one', 'One', [150])]})
        report = build(self.output, feed, config=self.config, max_pages=1, popularity='librivox-id')
        self.assertTrue(report['partial'])
        self.assertEqual(report['new_genre_ids'], [150])
        self.assertTrue(all(url != ARCHIVE for url, _ in feed.calls))
        with closing(sqlite3.connect(self.output)) as db:
            self.assertIn('NOT popularity', db.execute("SELECT value FROM catalog_metadata WHERE key='popularity'").fetchone()[0])

    def test_popularity_sample_requests_only_its_identifiers(self):
        feed = Feed({0: [book(1, 'one', 'One', [1])]}, [{'identifier': 'one', 'downloads': 10}])
        report = build(self.output, feed, config=self.config, max_pages=1)
        self.assertEqual(report['archive_documents'], 1)
        self.assertEqual(report['missing_download_counts'], [])
        archive_requests = [params for url, params in feed.calls if url == ARCHIVE]
        self.assertEqual(archive_requests[0]['q'], 'identifier:("one")')

    def test_download_failure_does_not_publish_unsorted_catalog(self):
        feed = Feed({0: [book(1, 'one', 'One', [1])]}, [{'identifier': 'one', 'downloads': -1}])
        with self.assertRaisesRegex(BuildError, 'download count'):
            build(self.output, feed, config=self.config)
        self.assertFalse(self.output.exists())

    def test_archive_mixed_case_order_is_valid_but_exact_duplicates_fail(self):
        # Actual failing sequence from Archive: Python orders uppercase J before j.
        first = '1john_asv_ss_librivox'
        second = '1John_kjv_librivox'
        self.assertLess(second, first)

        class Pages:
            def __init__(self, duplicate=False):
                self.duplicate = duplicate

            def get(self, endpoint, params):
                ids = [first, second] if params['page'] == 1 else [first if self.duplicate else 'another_book']
                return {'response': {'numFound': 3, 'docs': [{'identifier': value, 'downloads': 10} for value in ids]}}

        for duplicate in (False, True):
            with closing(sqlite3.connect(':memory:')) as db:
                db.execute('CREATE TABLE source_downloads(archive_id TEXT PRIMARY KEY,downloads INTEGER,avg_rating REAL,num_reviews INTEGER,languages_json TEXT)')
                if duplicate:
                    with self.assertRaisesRegex(BuildError, 'repeated identifier'):
                        collect_downloads(db, Pages(duplicate=True))
                else:
                    self.assertEqual(collect_downloads(db, Pages()), 3)
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM source_downloads').fetchone()[0], 3)

    def test_large_archive_collection_uses_complete_bulk_export(self):
        class BulkFeed:
            def __init__(self, *, truncated=False, changed=False):
                self.calls = []
                self.truncated, self.changed = truncated, changed

            def get(self, endpoint, params):
                self.calls.append(params)
                if 'page' in params:
                    return {'response': {'numFound': 10001, 'docs': [{'identifier': 'book0'}]}}
                size = 10000 if self.truncated else 10001
                return {'response': {
                    'numFound': 10002 if self.changed else 10001,
                    'docs': [{'identifier': f'book{i}'} for i in reversed(range(size))],
                }}

        for options in ({}, {'truncated': True}, {'changed': True}):
            feed = BulkFeed(**options)
            with closing(sqlite3.connect(':memory:')) as db:
                db.execute('CREATE TABLE source_downloads(archive_id TEXT PRIMARY KEY,downloads INTEGER,avg_rating REAL,num_reviews INTEGER,languages_json TEXT)')
                if options:
                    with self.assertRaisesRegex(BuildError, 'incomplete|changed'):
                        collect_downloads(db, feed)
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM source_downloads').fetchone()[0], 0)
                else:
                    self.assertEqual(collect_downloads(db, feed), 10001)
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM source_downloads').fetchone()[0], 10001)
                self.assertEqual(len(feed.calls), 2)
                self.assertEqual(feed.calls[1]['rows'], 10001)
                self.assertNotIn('page', feed.calls[1])
                self.assertNotIn('sort[]', feed.calls[1])

    def test_metadata_is_persisted_with_nulls_and_language_fallback(self):
        feed = Feed({0: [book(1, 'rated', 'Rated', [1], language=['English', 'French']),
                         book(2, 'unrated', 'Unrated', [1], language=''),
                         book(3, 'missing', 'Missing', [1])]}, [
            {'identifier': 'rated', 'downloads': 42, 'avg_rating': '4.5', 'num_reviews': 8, 'language': 'eng'},
            {'identifier': 'unrated', 'language': ['Spanish'], 'num_reviews': 0},
        ])
        build(self.output, feed, config=self.config, max_pages=1)
        with closing(sqlite3.connect(self.output)) as db:
            rows = db.execute('SELECT archive_id,languages_json,avg_rating,num_reviews,downloads FROM books ORDER BY id').fetchall()
            self.assertEqual(json.loads(rows[0][1]), ['English', 'French'])
            self.assertEqual(rows[0][2:], (4.5, 8, 42))
            self.assertEqual(json.loads(rows[1][1]), ['Spanish'])
            self.assertEqual(rows[1][2:], (None, 0, None))
            self.assertEqual(rows[2][2:], (None, None, None))

    def test_librivox_only_build_keeps_language_without_fabricating_ratings(self):
        feed = Feed({0: [book(1, 'one', 'One', [1], language='Nepali')]})
        build(self.output, feed, config=self.config, popularity='librivox-id')
        with closing(sqlite3.connect(self.output)) as db:
            row = db.execute('SELECT languages_json,avg_rating,num_reviews,downloads FROM books').fetchone()
            self.assertEqual(json.loads(row[0]), ['Nepali'])
            self.assertEqual(row[1:], (None, None, None))

    def test_empty_or_malformed_feed_cannot_publish(self):
        for feed in [Feed({}), Feed({0: [{'id': '1', 'title': 'Broken'}]})]:
            with self.assertRaises(BuildError):
                build(self.output, feed, config=self.config)
            self.assertFalse(self.output.exists())

    def test_archive_links_and_missing_identifiers(self):
        self.assertEqual(archive_id(r'https:\/\/archive.org\/details\/alice?x=1'), 'alice')
        self.assertIsNone(archive_id('https://example.org/details/alice'))
        self.assertIsNone(archive_id('https://archive.org/details/../file'))
        raw = book(1, 'unused', 'T', [])
        raw['url_iarchive'] = ''
        self.assertIsNone(parse_book(raw)[1])
        raw['genres'] = [{'name': 'Missing numeric ID'}]
        with self.assertRaises(BuildError):
            parse_book(raw)

    def test_http_retry_cache_and_exact_end_of_feed_response(self):
        client = JsonClient(Path(self.temp.name) / 'cache', delay=0, retries=1)
        failure = HTTPError(LIBRIVOX, 429, 'slow down', {'Retry-After': '0'}, io.BytesIO(b'busy'))
        good = io.BytesIO(b'{"books":[]}')
        with patch('backend.build_catalog.urlopen', side_effect=[failure, good]) as request, patch('backend.build_catalog.time.sleep'):
            self.assertEqual(client.get(LIBRIVOX, {'offset': 0}, librivox=True), {'books': []})
            self.assertEqual(client.get(LIBRIVOX, {'offset': 0}, librivox=True), {'books': []})
            self.assertEqual(request.call_count, 2)
        end = HTTPError(LIBRIVOX, 404, 'end', {}, io.BytesIO(b'{"error":"Audiobooks could not be found"}'))
        with patch('backend.build_catalog.urlopen', side_effect=end):
            self.assertEqual(client.get(LIBRIVOX, {'offset': 1}, librivox=True), {'books': []})
        bad = HTTPError(LIBRIVOX, 404, 'bad path', {}, io.BytesIO(b'<html>Not found</html>'))
        with patch('backend.build_catalog.urlopen', side_effect=bad), self.assertRaises(BuildError):
            client.get(LIBRIVOX, {'offset': 2}, librivox=True)

    def test_schema_extends_the_shipped_catalog_without_changing_indexes(self):
        feed = Feed({0: [book(1, 'one', 'One', [1])]})
        build(self.output, feed, config=self.config, popularity='librivox-id')
        reference = Path(__file__).resolve().parents[1] / 'assets/database/genre_catalog.sqlite3'
        if not reference.exists():
            self.skipTest('Reference asset is optional on a standalone server')
        with closing(sqlite3.connect(self.output)) as built, closing(sqlite3.connect(reference)) as original:
            schema = "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"
            current = dict(built.execute(schema).fetchall())
            previous = dict(original.execute(schema).fetchall())
            current.pop('books')
            previous.pop('books')
            self.assertEqual(current, previous)
            fields = [row[1] for row in built.execute('PRAGMA table_info(books)')]
            self.assertEqual(fields, ['id', 'archive_id', 'title', 'author', 'languages_json', 'avg_rating', 'num_reviews', 'downloads'])
            self.assertEqual(built.execute('PRAGMA user_version').fetchone()[0], 2)


if __name__ == '__main__':
    unittest.main()
