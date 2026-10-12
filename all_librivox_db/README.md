# Build Mero's catalog database

This Python program fetches **LibriVox's current catalog**, including each book's
numeric genre IDs, authors and Archive identifier. It produces the same SQLite
tables and FTS4 indexes used by `assets/database/genre_catalog.sqlite3`, with
schema-v2 columns for language and rating metadata.
The Flutter app and its bundled database are not modified by running it.

It is a compatible fresh dataset, **not a byte-for-byte copy** of the existing
snapshot: current titles, available projects, memberships and popularity can differ.
The app has not yet been changed to download this database from a server.

## Run

Requires Python **3.10+** with SQLite FTS4/unicode61 support. No pip packages or API
keys are needed. Run from the repository root:

```powershell
python backend/build_catalog.py
```

Outputs:

- `backend/output/genre_catalog.sqlite3`
- `backend/output/genre_catalog.sqlite3.report.json` — counts, SHA-256, missing
  download counts, projects without usable Archive URLs, and partial-build status.

The full crawl can take a while; progress prints after every page. LibriVox permits
at most 500 projects per request. The builder requests only the fields it needs,
uses ascending project IDs, follows offsets until the explicit empty-feed response,
and waits at least one second between HTTP requests. There are no parallel bulk requests.

For a quick live sample:

```powershell
python backend/build_catalog.py --max-pages 1 --page-size 5
```

This writes **`genre_catalog.sample.sqlite3`**, marked `partial=true` in metadata
and the report. It must not be published as the complete catalog. A sample's ranks
only compare books in that sample. A complete crawl ends only when LibriVox returns
its recognized “Audiobooks could not be found” response.

Existing output files are protected by default. Rebuild explicitly with:

```powershell
python backend/build_catalog.py --force
```

For a fresh API snapshot, choose a new cache directory as well:

```powershell
python backend/build_catalog.py --cache-dir backend/.cache/new-snapshot --force
```

The default response cache is `backend/.cache/<UTC-date>/`. Rerunning with the same
directory replays completed requests and continues the crawl, saving bandwidth
after interruption. Cache data is a crawl snapshot, not a permanently fresh feed.
Use the original `--cache-dir` to resume across dates. The API is live, so this is
not a transactionally consistent server snapshot; repeated/backwards LibriVox IDs,
duplicate Archive identifiers, or an Archive result-count change cause a safe failure rather than publishing a suspect
page sequence. Use a fresh cache if such a failure occurs.

Archive's identifier sort is not Python's case-sensitive string ordering. Mixed-case
identifiers are accepted in the server's order; exact duplicate IDs across pages
are still rejected so pagination problems cannot silently drop records.

HTTP 429, transient server errors and connection failures receive bounded retries,
including `Retry-After`. Other errors stop the build. Configure with `--timeout`,
`--retries`, `--delay`, and `--page-size`. Do not hammer either volunteer service.

## Genre membership and popularity

- **LibriVox is authoritative for genre membership.** Requests use `extended=1`
  and `fields=id,title,authors,url_iarchive,genres,language`. No guessing from Archive subjects.
- `genre_config.json` contains your 81 display-group mappings and the existing 147
  source IDs. Empty genres remain present; additional IDs returned by LibriVox are
  appended and reported. The display mappings do not collapse the source IDs.
  “Courses” was omitted as in your latest supplied mapping.
- LibriVox does **not** provide download popularity. The default build obtains
  Archive.org download counts and sorts each genre by downloads descending.
  Ties use stable catalog order. Missing counts sort last and are listed in the
  report; they are not treated as verified zero downloads.
- Full builds first request 1,000 Archive records to determine the collection size.
  Collections above Archive's 10,000-result pagination limit use its documented
  bulk export: request the full count with no `page` or `sort` parameter. The
  builder checks the export count and rejects duplicate IDs or incomplete results.
  Smaller collections use normal pagination. Requests include `identifier`,
  `downloads`, `avg_rating`, `num_reviews`, and `language`. Small sample builds
  request statistics only for their sample identifiers.
- To use only LibriVox, run `--popularity librivox-id`. The resulting ranks use
  ascending LibriVox project ID and metadata explicitly says **NOT popularity**.
- Multiple projects pointing at the same Archive identifier become one book;
  genre memberships are combined and the earliest project's display metadata wins.
  Multi-author names remain a combined credit. Projects with missing/unusable
  Archive links are reported and excluded because the app needs an Archive ID.

## Stored language and ratings (schema version 2)

The `books` table also contains:

| Column | Meaning |
| --- | --- |
| `languages_json` | JSON list of languages; `[]` when unknown. LibriVox supplies the primary value, with Archive language metadata as fallback. |
| `avg_rating` | Archive's average review rating from 0 to 5, or SQL `NULL` when unavailable/invalid. |
| `num_reviews` | Number of Archive reviews, or `NULL` when unknown. Explicit zero remains zero. |
| `downloads` | Archive download count, or `NULL` when unknown. |

These fields add little storage per book and need no extra per-book requests.
`--popularity librivox-id` skips Archive: language is still stored, while ratings,
reviews and downloads remain unknown. Ratings are snapshot data, not live values;
use a fresh cache directory and rebuild periodically to refresh them. `built_at`
records database generation time; response-cache files retain their own fetch times.

The Flutter catalog readers support both the old four-column book records and
schema v2. Genre, catalog-author and search results read the stored metadata;
existing online detail/metadata refreshes can still update it. Rebuild the catalog
to populate these fields; existing bundled assets are not automatically replaced.

## Author profiles and standalone server use

The shared schema includes `books`, `genres`, `genre_books`, `book_search`,
`book_terms`, `catalog_authors`, `catalog_author_search`, `catalog_author_books`,
and `catalog_metadata`. Internal SQLite FTS tables are created normally. Temporary
fetch tables are removed before publication. Search remains compatible with the app.

In this repository, the script automatically uses
`assets/database/authors_200.sqlite3` to retain profile IDs and searchable aliases.
Photos and biographies themselves remain in that separate profile database, just
as they do in the app today. The generated catalog links to those `pop_id` values.

You can copy the **whole `backend` directory** to a server without the Flutter
project or `tools/`. Supply the matching profile database explicitly:

```bash
python build_catalog.py --authors-db /srv/mero/authors_200.sqlite3 \
  --output /srv/mero/catalog/genre_catalog.sqlite3 \
  --cache-dir /srv/mero/catalog-cache/build-001
```

Without a profile database, or with `--no-author-profiles`, all authors are
generated from book credits and the profile-link column is null. The schema still
works, but the app cannot enrich these results from its curated profiles.

The builder writes to a temporary database, validates SQLite integrity, foreign
keys, contiguous ranks and both search indexes, then atomically replaces the
destination. An unsuccessful crawl leaves an existing destination intact. A lock
file prevents two jobs writing the same output. After a hard process kill, first
verify that no build is running, then remove the stale `<output>.lock` and its
temporary file before resuming. Normal failures/Ctrl+C clean these up automatically.

For future deployment, serve the completed file with its SHA-256/version and have
the app download and validate it before switching databases. Do not hot-swap an
open SQLite file inside the app. Internal book/catalog-author IDs are snapshot-local;
Archive identifiers remain the stable audiobook identity. Deployment and app-side
database updates are a later step.

## Tests and reference

```powershell
python -m unittest backend.test_build_catalog
python -m unittest discover -s tools
```

Tests cover backward-compatible schema extensions, stored language/ratings, popularity order, duplicate
projects, Unicode, genre membership, author/search indexes, HTTP retry/cache
behavior, partial builds, and preservation of existing output on failure.
`catalog_schema.py` is shared by the backend and existing local conversion tools
so those builders do not drift apart.

API documentation checked:

- https://librivox.org/api/info — fields, extended genre data, offsets, ascending
  project-ID ordering, and the current maximum page size of 500.
- https://archive.org/advancedsearch.php — JSON search fields, download counts,
  identifier ordering and pagination.
