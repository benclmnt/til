#!/usr/bin/env bun
/**
 * Atlas Obscura — Download All Places
 *
 * Extracts all place data from Atlas Obscura's "All Places on One Map" page,
 * optionally enriched with names & URLs via the JSON API.
 *
 * Uses Chrome's remote debugging protocol (CDP) to bypass Cloudflare.
 *
 * Usage:
 *   bun run download-places.ts                          # IDs + coordinates only
 *   bun run download-places.ts --enrich                 # + names & URLs
 *
 * Options:
 *   --enrich              Fetch names & URLs via the JSON API
 *   --concurrency <n>     Max concurrent requests (default 5, max 20)
 *   --output <path>       Output JSON path (default: atlas_obscura_all_places.json)
 *   --port <port>         Chrome DevTools port (default: 9222)
 *   --help                Show this help
 *
 * Notes on the API:
 *   - No rate limiting observed (tested at 50+ req/s bursts, zero 429s)
 *   - ~45% of requests return random 500 errors (server instability)
 *   - Script retries 500s up to 10 times with 500ms backoff
 *   - Throughput settles at ~3-5 successful req/s
 *
 * Prerequisites:
 *   Chrome running with --remote-debugging-port=9222
 *   e.g. /path/to/web-browser/scripts/start.js
 */

import WebSocket from "ws";

// ─── Constants ───────────────────────────────────────────────────────────────

const MAP_URL =
  "https://www.atlasobscura.com/articles/all-places-in-the-atlas-on-one-map";
const API_BASE = "https://www.atlasobscura.com/places";

// ─── CLI ─────────────────────────────────────────────────────────────────────

function parseArgs() {
  const args = process.argv.slice(2);
  return {
    enrich: args.includes("--enrich"),
    concurrency: args.includes("--concurrency")
      ? Math.min(Math.max(1, Number(args[args.indexOf("--concurrency") + 1]) || 5), 20)
      : 5,
    output: args.includes("--output")
      ? args[args.indexOf("--output") + 1]
      : "atlas_obscura_all_places.json",
    port: args.includes("--port")
      ? Number(args[args.indexOf("--port") + 1]) || 9222
      : 9222,
    help: args.includes("--help") || args.includes("-h"),
  };
}

// ─── Concurrency limiter (no rate limiting needed — API has no 429s) ─────────

class ConcurrencyLimiter {
  private running = 0;
  private queue: Array<() => void> = [];
  readonly max: number;

  constructor(max: number) {
    this.max = max;
  }

  async acquire(): Promise<void> {
    if (this.running < this.max) {
      this.running++;
      return;
    }
    await new Promise<void>((resolve) => this.queue.push(resolve));
    this.running++;
  }

  release(): void {
    this.running--;
    const next = this.queue.shift();
    if (next) next();
  }

  async run<T>(fn: () => Promise<T>): Promise<T> {
    await this.acquire();
    try {
      return await fn();
    } finally {
      this.release();
    }
  }
}

// ─── CDP Client ──────────────────────────────────────────────────────────────

class CDP {
  private ws: WebSocket;
  private id = 0;
  private callbacks = new Map<
    number,
    { resolve: (v: unknown) => void; reject: (e: Error) => void }
  >();
  private _open: Promise<void>;
  private _close: Promise<void>;

  constructor(wsUrl: string) {
    this._close = new Promise((resolve) => {
      this.ws = new WebSocket(wsUrl);
      this._open = new Promise((res) => this.ws.on("open", () => res()));
      this.ws.on("message", (data: Buffer) => {
        const msg = JSON.parse(data.toString());
        if (msg.id !== undefined && this.callbacks.has(msg.id)) {
          const cb = this.callbacks.get(msg.id)!;
          this.callbacks.delete(msg.id);
          if (msg.error) cb.reject(new Error(msg.error.message));
          else cb.resolve(msg.result);
        }
      });
      this.ws.on("close", () => resolve());
    });
  }

  async connect(timeout = 10_000): Promise<void> {
    await Promise.race([
      this._open,
      new Promise((_, reject) =>
        setTimeout(() => reject(new Error("CDP connect timeout")), timeout),
      ),
    ]);
  }

  async close(): Promise<void> {
    this.ws.close();
    await this._close;
  }

  async send(
    method: string,
    params: Record<string, unknown> = {},
    sessionId?: string,
    timeout = 15_000,
  ): Promise<unknown> {
    return new Promise((resolve, reject) => {
      const msgId = ++this.id;
      const msg: Record<string, unknown> = { id: msgId, method, params };
      if (sessionId) msg.sessionId = sessionId;
      const timer = setTimeout(() => {
        this.callbacks.delete(msgId);
        reject(new Error(`CDP timeout: ${method}`));
      }, timeout);
      this.callbacks.set(msgId, {
        resolve: (v) => {
          clearTimeout(timer);
          resolve(v);
        },
        reject: (e) => {
          clearTimeout(timer);
          reject(e);
        },
      });
      this.ws.send(JSON.stringify(msg));
    });
  }

  async attach(targetId: string): Promise<string> {
    const { sessionId } = (await this.send("Target.attachToTarget", {
      targetId,
      flatten: true,
    })) as { sessionId: string };
    return sessionId;
  }

  async evaluate(
    sessionId: string,
    expression: string,
    timeout = 30_000,
  ): Promise<unknown> {
    const result = (await this.send(
      "Runtime.evaluate",
      { expression, returnByValue: true, awaitPromise: true },
      sessionId,
      timeout,
    )) as {
      exceptionDetails?: { exception?: { description: string }; text: string };
      result?: { value: unknown };
    };
    if (result.exceptionDetails) {
      throw new Error(
        result.exceptionDetails.exception?.description ||
          result.exceptionDetails.text,
      );
    }
    return result.result?.value;
  }
}

// ─── Chrome helpers ──────────────────────────────────────────────────────────

async function getWsUrl(port: number): Promise<string> {
  const resp = await fetch(`http://localhost:${port}/json/version`);
  const { webSocketDebuggerUrl } = (await resp.json()) as {
    webSocketDebuggerUrl: string;
  };
  return webSocketDebuggerUrl;
}

// ─── Page interaction ────────────────────────────────────────────────────────

async function navigateToMap(cdp: CDP, sessionId: string): Promise<void> {
  console.log(`🔄 Loading ${MAP_URL} ...`);
  await cdp.send("Page.navigate", { url: MAP_URL }, sessionId, 60_000);

  // Wait for the all_places script to appear (handles Cloudflare)
  const deadline = Date.now() + 180_000; // 3 min max
  while (Date.now() < deadline) {
    try {
      const hasScript = (await cdp.evaluate(
        sessionId,
        `(function() {
          var scripts = Array.from(document.querySelectorAll("script:not([src])"));
          return scripts.some(function(s) { return s.textContent.includes("all_places"); });
        })()`,
        10_000,
      )) as boolean;
      if (hasScript) {
        console.log("✅ Page loaded");
        break;
      }
    } catch {
      // page still loading
    }
    process.stdout.write("⏳ Waiting for page...\r");
    await new Promise((r) => setTimeout(r, 2000));
  }

  // Dismiss cookie consent
  try {
    await cdp.evaluate(
      sessionId,
      `(function() {
        var btn = document.getElementById("onetrust-accept-btn-handler");
        if (btn) { btn.click(); return true; }
        return false;
      })()`,
    );
    await new Promise((r) => setTimeout(r, 1000));
  } catch {
    /* no dialog */
  }
}

async function extractAllPlaces(
  cdp: CDP,
  sessionId: string,
): Promise<Array<{ id: number; lat: number; lng: number }>> {
  console.log("🔍 Extracting all_places data...");
  const data = (await cdp.evaluate(
    sessionId,
    `(function() {
      var scripts = Array.from(document.querySelectorAll("script:not([src])"));
      var target = scripts.find(function(s) {
        return s.textContent.includes("all_places");
      });
      if (!target) throw new Error("all_places script not found");
      var text = target.textContent;
      var start = text.indexOf("[");
      var end = text.indexOf("];") + 1;
      return JSON.parse(text.substring(start, end));
    })()`,
  )) as Array<{ id: number; lat: number; lng: number }>;
  console.log(`✅ Found ${data.length} places`);
  return data;
}

// ─── API enrichment ──────────────────────────────────────────────────────────

interface EnrichedPlace {
  id: number;
  title: string;
  subtitle?: string;
  url: string;
  location: string;
  city?: string;
  country?: string;
  lat: number;
  lng: number;
}

/**
 * Fetch one place's details from the JSON API.
 *
 * Retry strategy:
 *   429 (rate limit) → exponential backoff: 1s → 2s → 4s → 8s (up to 5 tries)
 *   500+ (server error) → max 2 retries (3 total attempts), 1s linear backoff
 *   Network error → treated like 500
 */
async function fetchOnePlace(
  cdp: CDP,
  sessionId: string,
  id: number,
  limiter: ConcurrencyLimiter,
): Promise<EnrichedPlace | null> {
  let attempts500 = 0;
  const MAX_500 = 2; // max 2 retries on 500 = 3 total attempts

  for (let attempt = 0; attempt <= 10; attempt++) {
    if (attempt > 0) {
      // Wait is set after we know the status code (below)
      await new Promise((r) => setTimeout(r, 200));
    }

    try {
      const raw = (await limiter.run(async () => {
        return (await cdp.evaluate(
          sessionId,
          `(async function() {
            try {
              var r = await fetch("${API_BASE}/${id}.json", { signal: AbortSignal.timeout(15000) });
              return { status: r.status, text: await r.text() };
            } catch(e) {
              return { status: 0, text: e.toString() };
            }
          })()`,
          20_000,
        )) as { status: number; text: string };
      })) as { status: number; text: string };

      if (raw.status === 200) {
        const d = JSON.parse(raw.text) as Record<string, unknown>;
        return {
          id: (d.id as number) ?? id,
          title: ((d.title as string) ?? `Place #${id}`).trim(),
          subtitle: ((d.subtitle as string) ?? "").trim() || undefined,
          url: (d.url as string) ?? `https://www.atlasobscura.com/places/${id}`,
          location:
            (d.location as string) ??
            (d.city as string) ??
            (d.country as string) ??
            "",
          city: (d.city as string) ?? undefined,
          country: (d.country as string) ?? undefined,
          lat: ((d.coordinates as { lat?: number })?.lat ??
            (d.lat as number) ??
            0) as number,
          lng: ((d.coordinates as { lng?: number })?.lng ??
            (d.lng as number) ??
            0) as number,
        };
      }

      if (raw.status === 429) {
        // Rate limited — exponential backoff: 1s → 2s → 4s → 8s
        const delay = Math.min(Math.pow(2, attempt) * 1000, 8_000);
        process.stdout.write(`\n  ⚠️  429 on #${id} — waiting ${delay / 1000}s\n`);
        await new Promise((r) => setTimeout(r, delay));
        continue;
      }

      if (raw.status === 500 || raw.status === 0) {
        // Server error — bounded retries
        attempts500++;
        if (attempts500 > MAX_500) return null;
        if (attempts500 >= 2) {
          // Slightly longer wait on second retry
          await new Promise((r) => setTimeout(r, 1000));
        }
        continue;
      }

      // Other status codes — give up
      return null;
    } catch {
      // Network error — count as 500
      attempts500++;
      if (attempts500 > MAX_500) return null;
      continue;
    }
  }

  return null;
}

async function enrichPlaces(
  cdp: CDP,
  sessionId: string,
  places: Array<{ id: number; lat: number; lng: number }>,
  concurrency: number,
): Promise<EnrichedPlace[]> {
  const total = places.length;
  console.log(`🌐 Enriching ${total} places via API (concurrency: ${concurrency})...`);
  console.log(`   (~45% of requests get 500 errors — script retries up to 10x)`);

  const limiter = new ConcurrencyLimiter(concurrency);
  const results: EnrichedPlace[] = [];
  let errors = 0;
  let retries = 0;

  for (let i = 0; i < total; i++) {
    const place = places[i];
    const detail = await fetchOnePlace(cdp, sessionId, place.id, limiter);
    if (detail) {
      results.push(detail);
    } else {
      errors++;
      results.push({
        id: place.id,
        title: `Place #${place.id}`,
        url: `https://www.atlasobscura.com/places/${place.id}`,
        location: "",
        lat: place.lat,
        lng: place.lng,
      });
    }

    // Progress every 50 places
    if ((i + 1) % 50 === 0 || i === total - 1) {
      const pct = ((i + 1) / total * 100).toFixed(1);
      process.stdout.write(
        `  ⏳ ${i + 1}/${total} (${pct}%) — ${errors} errors   \r`,
      );
    }

    // Save partial progress every 500 places
    if ((i + 1) % 500 === 0) {
      await Bun.write(
        "atlas_obscura_all_places_partial.json",
        JSON.stringify(results, null, 2),
      );
    }
  }

  console.log(`\n✅ Done — ${results.length} places, ${errors} errors`);
  return results;
}

// ─── Main ────────────────────────────────────────────────────────────────────

async function main() {
  const opts = parseArgs();

  if (opts.help) {
    console.log(`
Usage: bun run download-places.ts [options]

Extract all places from Atlas Obscura's "All Places on One Map" page.

Options:
  --enrich              Fetch names & URLs via the JSON API
  --concurrency <n>     Max concurrent requests (default 5, max 20)
  --output <path>       Output JSON path (default: atlas_obscura_all_places.json)
  --port <port>         Chrome DevTools port (default: 9222)
  --help                Show this help

API behavior (determined empirically):
  - No rate limiting observed — 429 handled with exponential backoff 1s→2s→4s→8s
  - ~45% of requests get random 500 errors (server instability)
  - 500 errors: max 2 retries (3 attempts total)
  - Effective throughput: ~3-5 successful results/second

Prerequisites:
  Chrome running with --remote-debugging-port=9222
    /path/to/web-browser/scripts/start.js
`);
    process.exit(0);
  }

  console.log("🚀 Connecting to Chrome CDP...");
  const wsUrl = await getWsUrl(opts.port);
  const cdp = new CDP(wsUrl);
  await cdp.connect();
  console.log("✅ Connected");

  try {
    const { targetId } = (await cdp.send("Target.createTarget", {
      url: "about:blank",
    })) as { targetId: string };
    const sessionId = await cdp.attach(targetId);
    console.log("📌 Created new tab");

    await navigateToMap(cdp, sessionId);
    const places = await extractAllPlaces(cdp, sessionId);

    // Save base data (IDs + coords)
    const basePath = opts.output;
    await Bun.write(basePath, JSON.stringify(places));
    console.log(`💾 Saved ${places.length} places to ${basePath}`);

    // Enrich
    if (opts.enrich) {
      const enrichedPath = basePath.replace(/\.json$/, "_enriched.json");
      const enriched = await enrichPlaces(cdp, sessionId, places, opts.concurrency);
      await Bun.write(enrichedPath, JSON.stringify(enriched, null, 2));
      console.log(`💾 Saved enriched data to ${enrichedPath}`);

      // Cleanup partial
      try { Bun.spawnSync(["rm", "atlas_obscura_all_places_partial.json"]); } catch {}
    }
  } finally {
    await cdp.close();
    console.log("🔌 Disconnected");
  }

  console.log("✨ Done!");
}

main().catch((err) => {
  console.error("❌", err);
  process.exit(1);
});
