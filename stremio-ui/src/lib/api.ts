/** Typed wrappers around /api/miniapp/stremio/*.
 *
 *  Every call carries the Telegram WebApp.initData via the
 *  X-Telegram-Init-Data header, matching the rest of the SMDL Mini App.
 *  When loaded outside Telegram (dev browser), the APK cookie is used
 *  instead (already in the cookie jar via /auth/setup). */

const tg = (window as any).Telegram?.WebApp;

function headers(): Record<string, string> {
  const h: Record<string, string> = { "Accept": "application/json" };
  // Canonical header across all SMDL surfaces is X-Init-Data (server reads
  // that). Sending X-Telegram-Init-Data silently 401'd the initData path.
  if (tg?.initData) h["X-Init-Data"] = tg.initData;
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

export interface DiscoverItem extends MetaItem {
  /** present on continue_watching rows — the raw resume content_id */
  resume_id?: string;
  progress_pct?: number;
  position_seconds?: number | null;
}

export interface DiscoverData {
  continue_watching: DiscoverItem[];
  popular_movies: DiscoverItem[];
  popular_series: DiscoverItem[];
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

export interface AddonSummary {
  url: string;
  name: string;
  description: string | null;
  types: string[];
  resources: string[];
  logo: string | null;
  version: string | null;
}

export interface EpisodeMeta {
  /** Stremio addon content_id, e.g. "tt0903747:1:1" — feed into /streams */
  id: string;
  season: number;
  episode: number;
  title: string;
  released: string | null;
  overview: string | null;
  thumbnail: string | null;
  runtime: number | null;
}

// ── Endpoint wrappers ────────────────────────────────────────────────────────
export const api = {
  account: () => get<RDAccount>("/api/miniapp/stremio/account"),

  search: (q: string, type: "movie" | "series" = "movie") =>
    get<{ results: MetaItem[] }>(
      `/api/miniapp/stremio/search?q=${encodeURIComponent(q)}&type=${type}`,
    ),

  discover: () => get<DiscoverData>("/api/miniapp/stremio/discover"),

  streams: (imdb_id: string, type: "movie" | "series" = "movie",
            quality: string = "1080p") =>
    get<{ streams: StreamEntry[] }>(
      `/api/miniapp/stremio/streams?imdb_id=${encodeURIComponent(imdb_id)}&type=${type}&quality=${quality}`,
    ),

  /** Series episodes for a tt-id. Returns S/E-sorted list. */
  episodes: (imdb_id: string) =>
    get<{ episodes: EpisodeMeta[] }>(
      `/api/miniapp/stremio/episodes?imdb_id=${encodeURIComponent(imdb_id)}`,
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
      root: string;
    }>("/api/miniapp/stremio/cache"),

  /** Purge every cached file + its metadata (#67). */
  purgeCache: () =>
    post<{ ok: boolean; deleted: number; bytes_freed: number }>(
      "/api/miniapp/stremio/cache/purge", {},
    ),

  /** URL of a cached file (served range-aware by SMDL). */
  cachedFileUrl: (infohash: string) =>
    `/api/miniapp/stremio/file/${infohash}`,

  // ── Addons (#66) ──────────────────────────────────────────────────────
  addons: {
    list: () =>
      get<{ installed: AddonSummary[]; catalog: AddonSummary[];
            using_defaults: boolean }>("/api/miniapp/stremio/addons"),
    add: (url: string) =>
      post<{ ok: boolean; error?: string; addon?: AddonSummary;
             addons: string[] }>(
        "/api/miniapp/stremio/addons/add", { url }),
    remove: (url: string) =>
      post<{ ok: boolean; addons: string[] }>(
        "/api/miniapp/stremio/addons/remove", { url }),
  },

  // ── P7 settings + resume position ────────────────────────────────────
  settings: {
    get: () => get<{ settings: any }>("/api/miniapp/stremio/settings"),
    set: (patch: Record<string, any>) =>
      post<{ settings: any }>("/api/miniapp/stremio/settings", patch),
  },

  /** Real-Debrid personal token — owner pastes it here when missing/rotated. */
  rdToken: {
    status: () =>
      get<{ set: boolean; masked: string | null; source: string | null;
            editable: boolean }>("/api/miniapp/stremio/rd-token"),
    set: (token: string) =>
      post<{ ok: boolean; error?: string;
             status: { set: boolean; masked: string | null; source: string | null;
                       editable: boolean };
             account: RDAccount }>("/api/miniapp/stremio/rd-token", { token }),
  },
  position: {
    get: (imdb_id: string) =>
      get<{ position: { position_seconds: number; duration_seconds: number | null;
                          updated_at: string } | null }>(
        `/api/miniapp/stremio/position/${encodeURIComponent(imdb_id)}`,
      ),
    save: (params: { imdb_id: string; position_seconds: number;
                      duration_seconds?: number | null }) =>
      post<{ ok: boolean }>("/api/miniapp/stremio/position", params),
  },

  // ── P6 Trakt ──────────────────────────────────────────────────────────
  trakt: {
    status: () =>
      get<{ connected: boolean; expires_in_days?: number; scope?: string }>(
        "/api/miniapp/stremio/trakt/status",
      ),
    connectStart: () =>
      post<{ ok: boolean; error?: string; device_code: string;
             user_code: string; verification_url: string;
             expires_in: number; interval: number }>(
        "/api/miniapp/stremio/trakt/connect/start", {},
      ),
    connectPoll: (device_code: string) =>
      post<{ ok: boolean; status: "pending" | "connected" | "error";
             error?: string }>(
        "/api/miniapp/stremio/trakt/connect/poll", { device_code },
      ),
    disconnect: () =>
      post<{ ok: boolean }>("/api/miniapp/stremio/trakt/disconnect", {}),
    scrobble: (params: {
      imdb_id: string; type?: "movie" | "series";
      season?: number | null; episode?: number | null;
      progress_pct?: number; event: "start" | "pause" | "stop";
    }) =>
      post<{ ok: boolean; error?: string }>(
        "/api/miniapp/stremio/trakt/scrobble", params,
      ),
    watchlist: (type: "movies" | "shows" = "movies") =>
      get<{ ok: boolean; error?: string;
            items: Array<{ id: string; type: string; name: string; year: number | null }> }>(
        `/api/miniapp/stremio/trakt/watchlist?type=${type}`,
      ),
  },
};
