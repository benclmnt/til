# Atlas Obscura — All Places Scraper

Download all 31,126 places from [Atlas Obscura](https://www.atlasobscura.com/articles/all-places-in-the-atlas-on-one-map) and store them in SQLite.

## Scripts

### `download-places.ts`

Extracts all place IDs + coordinates from the interactive map page, then optionally
enriches them with names, URLs, descriptions via the JSON API.

```bash
# Basic — just IDs & coordinates (takes ~10s)
bun run download-places.ts

# Enriched — names, URLs, descriptions (takes ~2-3 hours with retries)
bun run download-places.ts --enrich

# Faster enrichment (more concurrent requests)
bun run download-places.ts --enrich --concurrency 10

# Custom output
bun run download-places.ts --output my-places.json
```

**Output:**
- `atlas_obscura_all_places.json` — base format: `[{id, lat, lng}]`
- `atlas_obscura_all_places_enriched.json` — enriched format: `[{id, title, subtitle, url, location, city, country, lat, lng}]`
- `atlas_obscura_all_places_partial.json` — intermediate saves (every 500 places)

### `places-to-sqlite.ts`

Reads the JSON (base or enriched) and writes to a SQLite database.

```bash
# Auto-detects _enriched.json or falls back to base
bun run places-to-sqlite.ts

# Explicit input
bun run places-to-sqlite.ts --input my-places.json

# Custom database path
bun run places-to-sqlite.ts --db my-places.db
```

**Schema:**

```sql
CREATE TABLE places (
  id          INTEGER PRIMARY KEY,
  title       TEXT,
  url         TEXT,
  location    TEXT,
  city        TEXT,
  country     TEXT,
  description TEXT,   -- the "subtitle" from the API
  lat         REAL NOT NULL,
  lng         REAL NOT NULL
);
CREATE INDEX idx_places_lat_lng    ON places (lat, lng);
CREATE INDEX idx_places_location   ON places (location);
```

## API Behavior (determined empirically)

We tested the API extensively using Chrome CDP:

| Property | Finding |
|----------|---------|
| **Rate limiting** | ❌ None observed — but handled with exponential backoff: 1s→2s→4s→8s |
| **Server errors** | ~45% of requests return random **500 errors** (server instability) |
| **Retries** | 500 errors: max 2 retries (3 total attempts). 429: exponential backoff 1s→2s→4s→8s (up to 5 tries) |
| **Throughput** | ~3-5 successful results/second (limited by server capacity) |

**Why no rate limiting?** The API is behind Cloudflare and Heroku. The Cloudflare challenge on the main page gates access, but once through, the JSON API has no additional throttling.

## Prerequisites

- **Bun** (v1.0+)
- **Chrome** running with `--remote-debugging-port=9222`

Start Chrome via the web-browser skill:

```bash
cd /path/to/web-browser && ./scripts/start.js
```

Or start Chrome manually:

```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222 --no-first-run
```

## How It Works

1. Connects to Chrome via CDP (bypasses Cloudflare)
2. Navigates to the map page, waits for the `all_places` data to appear
3. Extracts the embedded JSON array from the `<script>` tag (~31K places)
4. **(optional)** Makes concurrent requests to `/places/{id}.json` for names/URLs
5. Retries on 500 errors (server flakiness) up to 10 times
6. Saves as JSON, then imports into SQLite
