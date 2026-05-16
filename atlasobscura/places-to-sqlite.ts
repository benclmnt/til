#!/usr/bin/env bun
/**
 * Atlas Obscura — Places to SQLite
 *
 * Reads the place data JSON (base or enriched) and writes it into a SQLite
 * database. Handles both formats:
 *   - Base:     [{ id, lat, lng }]
 *   - Enriched: [{ id, title, url, location, lat, lng }]
 *
 * Usage:
 *   bun run places-to-sqlite.ts                                        # reads atlas_obscura_all_places.json
 *   bun run places-to-sqlite.ts --input atlas_obscura_all_places_enriched.json
 *   bun run places-to-sqlite.ts --db my-places.db
 *   bun run places-to-sqlite.ts --help
 */

import { Database } from "bun:sqlite";
import { existsSync } from "fs";

// ─── CLI ─────────────────────────────────────────────────────────────────────

function parseArgs() {
  const args = process.argv.slice(2);
  return {
    input:
      args.includes("--input")
        ? args[args.indexOf("--input") + 1]
        : existsSync("atlas_obscura_all_places_enriched.json")
          ? "atlas_obscura_all_places_enriched.json"
          : "atlas_obscura_all_places.json",
    db:
      args.includes("--db")
        ? args[args.indexOf("--db") + 1]
        : "atlas_obscura.db",
    help: args.includes("--help") || args.includes("-h"),
  };
}

// ─── Main ────────────────────────────────────────────────────────────────────

async function main() {
  const opts = parseArgs();

  if (opts.help) {
    console.log(`
Usage: bun run places-to-sqlite.ts [options]

Read place data JSON and write to a SQLite database.

Options:
  --input <path>    Input JSON file (default: auto-detect _enriched or base)
  --db <path>       Output SQLite file (default: atlas_obscura.db)
  --help            Show this help
`);
    process.exit(0);
  }

  // Read input
  if (!existsSync(opts.input)) {
    console.error(`❌ File not found: ${opts.input}`);
    console.error("   Run download-places.ts first, or specify --input");
    process.exit(1);
  }

  console.log(`📖 Reading ${opts.input} ...`);
  const raw = Bun.file(opts.input);
  const places = JSON.parse(await raw.text()) as Record<string, unknown>[];

  if (!Array.isArray(places) || places.length === 0) {
    console.error("❌ Invalid or empty JSON array in input file");
    process.exit(1);
  }

  console.log(`📦 ${places.length} places loaded`);

  // Detect format
  const sample = places[0];
  const isEnriched =
    "title" in sample || "url" in sample || "location" in sample;
  console.log(`📋 Format: ${isEnriched ? "enriched (names + URLs)" : "base (IDs + coordinates)"}`);

  // Open / create DB
  console.log(`🗄️  Writing to ${opts.db} ...`);
  const db = new Database(opts.db);

  // Create table
  db.run(`
    CREATE TABLE IF NOT EXISTS places (
      id        INTEGER PRIMARY KEY,
      title     TEXT,
      url       TEXT,
      location  TEXT,
      city      TEXT,
      country   TEXT,
      description TEXT,
      lat       REAL NOT NULL,
      lng       REAL NOT NULL
    )
  `);

  // Use a transaction for bulk insert
  const insert = db.prepare(`
    INSERT OR REPLACE INTO places (id, title, url, location, city, country, description, lat, lng)
    VALUES ($id, $title, $url, $location, $city, $country, $description, $lat, $lng)
  `);

  const insertMany = db.transaction(
    (batch: Record<string, unknown>[]) => {
      for (const p of batch) {
        insert.run({
          $id: p.id as number,
          $title: isEnriched ? ((p.title as string) ?? null) : null,
          $url: isEnriched ? ((p.url as string) ?? null) : null,
          $location: isEnriched ? ((p.location as string) ?? null) : null,
          $city: isEnriched ? ((p.city as string) ?? null) : null,
          $country: isEnriched ? ((p.country as string) ?? null) : null,
          $description: isEnriched ? ((p.subtitle as string) ?? null) : null,
          $lat: (p.lat as number) ?? 0,
          $lng: (p.lng as number) ?? 0,
        });
      }
    },
  );

  // Insert in batches of 500
  const BATCH = 500;
  for (let i = 0; i < places.length; i += BATCH) {
    const batch = places.slice(i, i + BATCH);
    insertMany(batch);
    process.stdout.write(
      `  ⏳ ${Math.min(i + BATCH, places.length)} / ${places.length}   \r`,
    );
  }
  console.log("");

  // Create indexes
  console.log("🔍 Creating indexes...");
  db.run("CREATE INDEX IF NOT EXISTS idx_places_lat_lng ON places (lat, lng)");
  db.run("CREATE INDEX IF NOT EXISTS idx_places_location ON places (location)");

  // Show stats
  const count = db.query("SELECT COUNT(*) as cnt FROM places").get() as {
    cnt: number;
  };
  const hasTitles = db
    .query("SELECT COUNT(*) as cnt FROM places WHERE title IS NOT NULL")
    .get() as { cnt: number };

  db.close();

  console.log(`✅ Done — ${count.cnt} places in database`);
  if (hasTitles.cnt > 0) {
    console.log(`   ${hasTitles.cnt} have names / URLs`);
  }

  // Quick sample
  console.log("\n📋 Sample:\n");
  const sampleDb = new Database(opts.db);
  const rows = sampleDb
    .query(
      isEnriched
        ? "SELECT id, title, substr(url,1,50) as url, location, substr(description,1,30) as desc, lat, lng FROM places WHERE title IS NOT NULL LIMIT 5"
        : "SELECT id, lat, lng FROM places LIMIT 5",
    )
    .all() as Record<string, unknown>[];
  console.table(rows);
  sampleDb.close();
}

main().catch((err) => {
  console.error("❌", err);
  process.exit(1);
});
