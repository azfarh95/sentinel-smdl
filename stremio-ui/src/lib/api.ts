/** Typed wrappers around /api/miniapp/stremio/*.
 *
 *  Every call carries the Telegram WebApp.initData via the
 *  X-Telegram-Init-Data header, matching the rest of the SMDL Mini App.
 *  When loaded outside Telegram (dev browser), the APK cookie is used
 *  instead (already in the cookie jar via /auth/setup). */

const tg = (window as any).Telegram?.WebApp;

function headers(): Record<string, string> {
  const h: Record<string, string> = { "Accept": "application/json" };
  if (tg?.initData) h["X-Telegram-Init-Data"] = tg.initData;
  return h;
}

async function get<T>(path: string): Promise<T> {
  const r = await fetch(path, { headers: headers(), credentials: "include" });
  if (!r.ok) throw new Error(`${path} → HTTP ${r.status}`);
  return r.json() as Promise<T>;
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(path, {
    method: "POST",
    headers: { ...headers(), "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`${path} → HTTP ${r.status}`);
  return r.json() as Promise<T>;
}

// ── Types ────────────────────────────────────────────────────────────────────
export interface RDAccount {
  ok: boolean;
  error?: string;
  username?: string;
  email?: string;
  type?: string;
  is_premium?: boolean;
  expiration?: string;
  days_left?: number;
  points?: number;
}

export interface MetaItem {
  id: string;
  type: "movie" | "series";
  name: string;
  year: number | null;
  poster: string | null;
  description: string | null;
  imdb_rating: number | null;
  genres: string[];
}

export interface StreamEntry {
  title: string;
  infohash: string | null;
  has_magnet: boolean;
  size_bytes: number | null;
  seeders: number | null;
  quality: string | null;
  source_addon: string;
  file_index: number | null;
}

export interface GrabFile {
  filename: string;
  filesize: number;
  direct_url: string;
  mime_type: string | null;
}

export interface StremioJob {
  id: number;
  imdb_id: string;
  type: "movie" | "series";
  title: string;
  infohash: string;
  file_index: number | null;
  source_stream_title: string | null;
  quality: string | null;
  expected_size: number | null;
  status: "queued" | "resolving" | "streaming" | "caching" | "cached" | "error";
  progress: number;
  direct_url: string | null;
  filename: string | null;
  filesize: number | null;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export interface CacheEntry {
  imdb_id: string;
  infohash: string;
  title: string;
  filename: string;
  filesize: number;
  mime: string | null;
  grabbed_at: string;
  last_played: string;
}

// ── Endpoint wrappers ────────────────────────────────────────────────────────
export const api = {
  account: () => get<RDAccount>("/api/miniapp/stremio/account"),

  search: (q: string, type: "movie" | "series" = "movie") =>
    get<{ results: MetaItem[] }>(
      `/api/miniapp/stremio/search?q=${encodeURIComponent(q)}&type=${type}`,
    ),

  streams: (imdb_id: string, type: "movie" | "series" = "movie",
            quality: string = "1080p") =>
    get<{ streams: StreamEntry[] }>(
      `/api/miniapp/stremio/streams?imdb_id=${encodeURIComponent(imdb_id)}&type=${type}&quality=${quality}`,
    ),

  grab: (params: { infohash?: string; magnet?: string; title?: string;
                    file_index?: number | null }) =>
    post<{ ok: boolean; error?: string; files?: GrabFile[] }>(
      "/api/miniapp/stremio/grab", params,
    ),

  // ── P4 queue + cache ──────────────────────────────────────────────────
  enqueue: (params: {
    imdb_id: string; type?: "movie" | "series"; title?: string;
    infohash: string; magnet?: string; file_index?: number | null;
    source_stream_title?: string | null; quality?: string | null;
    expected_size?: number | null;
  }) =>
    post<{ ok: boolean; job_id: number; job: StremioJob }>(
      "/api/miniapp/stremio/queue", params,
    ),

  jobs: () => get<{ jobs: StremioJob[] }>("/api/miniapp/stremio/jobs"),

  job:  (id: number) =>
    get<{ job: StremioJob }>(`/api/miniapp/stremio/jobs/${id}`),

  cache: () =>
    get<{
      entries: CacheEntry[];
      disk: { total: number; used: number; free: number; pct_used: number };
    }>("/api/miniapp/stremio/cache"),

  /** URL of a cached file (served range-aware by SMDL). */
  cachedFileUrl: (infohash: string) =>
    `/api/miniapp/stremio/file/${infohash}`,
};
