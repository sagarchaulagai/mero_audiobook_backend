"""Shared app-compatible catalog schema and author-index builder."""
import json
import re
import unicodedata

SCHEMA = """
PRAGMA page_size = 4096;
PRAGMA foreign_keys = ON;
PRAGMA user_version = 2;
CREATE TABLE catalog_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE genres (
    genre_id INTEGER PRIMARY KEY,
    source_order INTEGER NOT NULL UNIQUE CHECK (source_order >= 1)
);
CREATE TABLE books (
    id INTEGER PRIMARY KEY,
    archive_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    author TEXT NOT NULL,
    languages_json TEXT NOT NULL DEFAULT '[]',
    avg_rating REAL CHECK (avg_rating IS NULL OR (avg_rating >= 0 AND avg_rating <= 5)),
    num_reviews INTEGER CHECK (num_reviews IS NULL OR num_reviews >= 0),
    downloads INTEGER CHECK (downloads IS NULL OR downloads >= 0)
);
CREATE TABLE genre_books (
    genre_id INTEGER NOT NULL REFERENCES genres(genre_id),
    popularity_rank INTEGER NOT NULL CHECK (popularity_rank >= 1),
    book_id INTEGER NOT NULL REFERENCES books(id),
    PRIMARY KEY (genre_id, popularity_rank)
) WITHOUT ROWID;
CREATE INDEX genre_books_by_book ON genre_books(book_id, genre_id);
-- External content avoids a second copy of title/author in the FTS table.
-- No prefix indexes: normal FTS prefix matching works without their disk cost.
CREATE VIRTUAL TABLE book_search USING fts4(
    title, author, content='books', tokenize=unicode61, matchinfo=fts3
);
CREATE VIRTUAL TABLE book_terms USING fts4aux(book_search);
"""

def author_key(name):
    # Conservative identity matching: preserve accents and word order. Never
    # split commas, which can be part of a name (e.g. Horatio Alger, Jr.).
    words = re.findall(r'[^\W_]+', unicodedata.normalize('NFKC', name).casefold())
    if words and words[0] in ('sir', 'lady', 'dr', 'mr', 'mrs'):
        words = words[1:]
    return ' '.join(words)


def build_catalog_authors(db, profiles):
    """One FTS entry per distinct credit, linked to an unambiguous profile."""
    db.execute('DROP TABLE IF EXISTS catalog_author_search')
    db.execute('DROP TABLE IF EXISTS catalog_author_books')
    db.execute('DROP TABLE IF EXISTS catalog_authors')
    db.execute('CREATE TABLE catalog_authors (id INTEGER PRIMARY KEY, name TEXT NOT NULL, pop_id INTEGER UNIQUE)')
    db.execute('CREATE VIRTUAL TABLE catalog_author_search USING fts4(name,aliases,tokenize=unicode61,matchinfo=fts3)')
    db.execute('CREATE TABLE catalog_author_books (author_id INTEGER NOT NULL, book_id INTEGER NOT NULL, PRIMARY KEY(author_id,book_id)) WITHOUT ROWID')
    candidates = {}
    canonical = {}
    entries = {}
    for profile in profiles:
        identifier, name, aliases = profile
        alternate = json.loads(aliases or '[]')
        if not isinstance(alternate, list) or any(not isinstance(value, str) for value in alternate):
            raise ValueError(f'Invalid aliases for author {identifier}')
        names = [name, *alternate]
        entries[identifier] = [name, identifier, set(names)]
        canonical[author_key(name)] = identifier
        for value in names:
            candidates.setdefault(author_key(value), set()).add(identifier)
    linked = {key: next(iter(ids)) for key, ids in candidates.items() if key and len(ids) == 1}
    linked.update(canonical)  # Canonical names take precedence over noisy aliases.
    catalog_ids = {}
    first_catalog_id = max(entries, default=0) + 1
    for book_id, name in db.execute('SELECT id,author FROM books ORDER BY id'):
        name = name.strip()
        key = author_key(name)
        if not key:
            continue
        identifier = linked.get(key)
        if identifier is None:
            identifier = catalog_ids.setdefault(key, first_catalog_id + book_id)
            entries.setdefault(identifier, [name, None, set()])
        entries[identifier][2].add(name)
        db.execute('INSERT INTO catalog_author_books VALUES (?,?)', (identifier, book_id))
    for identifier, (name, pop_id, aliases) in entries.items():
        db.execute('INSERT INTO catalog_authors VALUES (?,?,?)', (identifier, name, pop_id))
        db.execute('INSERT INTO catalog_author_search(docid,name,aliases) VALUES (?,?,?)',
                   (identifier, name, ' '.join(sorted(aliases))))
    db.execute("INSERT INTO catalog_author_search(catalog_author_search) VALUES('optimize')")
    db.execute("INSERT INTO catalog_author_search(catalog_author_search) VALUES('integrity-check')")

