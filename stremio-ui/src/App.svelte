<script lang="ts">
  /** Sentinel Media — Stremio sub-app.
   *  Three views:
   *    1. Search  — input + poster grid
   *    2. Detail  — selected title + stream picker
   *    3. Grabbed — playable URL or "still resolving" spinner
   *
   *  No router — single-component view state machine. The TG BackButton
   *  is wired to navigate Detail → Search; on Search it closes the app. */
  import { onMount } from "svelte";
  import { Search, ArrowLeft, Play, Download, Loader2, Film, ListVideo, HardDrive, Tv, Settings, Home, Puzzle, Plus, Trash2, Check, Shield, KeyRound } from "@lucide/svelte";
  import { api, type MetaItem, type StreamEntry, type GrabFile, type RDAccount,
            type StremioJob, type CacheEntry, type EpisodeMeta,
            type DiscoverData, type DiscoverItem, type AddonSummary } from "$lib/api";
  import { fmtSize } from "$lib/utils";
  import { Button } from "$lib/components/ui/button";
  import { Input } from "$lib/components/ui/input";
  import { Card, CardHeader, CardTitle, CardDescription, CardContent } from "$lib/components/ui/card";
  import { Badge } from "$lib/components/ui/badge";

  type View = "discover" | "detail" | "grab" | "queue" | "library" | "addons" | "settings";

  // ── State (Svelte 5 runes) ─────────────────────────────────────────────
  let view = $state<View>("discover");
  // Where Detail returns to (the grid we opened it from).
  let returnTo = $state<View>("discover");
  let query = $state("");
  let searching = $state(false);
  let results = $state<MetaItem[]>([]);
  let selected = $state<MetaItem | null>(null);
  let streamsLoading = $state(false);
  let streams = $state<StreamEntry[]>([]);
  let account = $state<RDAccount | null>(null);
  let lastError = $state<string | null>(null);

  // ── P4 queue + library state ──────────────────────────────────────────
  let activeJob = $state<StremioJob | null>(null);   // the job we're watching in the player
  let jobs      = $state<StremioJob[]>([]);          // queue view list
  let cache     = $state<CacheEntry[]>([]);
  let cacheDisk = $state<{ total: number; used: number; free: number; pct_used: number } | null>(null);
  let cacheRoot = $state<string>("");
  let purging   = $state(false);
  let purgeMsg  = $state<string | null>(null);
  let pollHandle: ReturnType<typeof setInterval> | null = null;
  // ── RD-blocked auto-advance ────────────────────────────────────────────
  // RD only serves pre-cached releases; uncached ones 451 (infringing_file).
  // We can't probe availability cheaply (instantAvailability is disabled), so
  // on a 451 we mark the source dead, grey it out, and walk to the next one.
  let grabbedIndex = $state<number>(-1);     // index in `streams` of the active grab
  let autoAdvancing = $state(false);          // true while hopping past dead sources
  let autoAdvanceTries = 0;                    // bounded so we don't cycle 40 sources
  const MAX_AUTO_ADVANCE = 5;

  // ── P5 series state ───────────────────────────────────────────────────
  let episodes    = $state<EpisodeMeta[]>([]);   // for the selected series
  let episodesLoading = $state(false);
  let activeSeason = $state<number>(1);
  let pickedEpisode = $state<EpisodeMeta | null>(null);   // episode whose streams are shown

  // ── Discovery home ─────────────────────────────────────────────────────
  let discover = $state<DiscoverData | null>(null);
  let discoverLoading = $state(false);
  async function loadDiscover() {
    discoverLoading = true; lastError = null;
    try { discover = await api.discover(); }
    catch (e) { lastError = String(e); }
    finally { discoverLoading = false; }
  }
  $effect(() => { if (view === "discover" && !discover && !discoverLoading) loadDiscover(); });

  // ── Addons tab (#66) ───────────────────────────────────────────────────
  let installedAddons = $state<AddonSummary[]>([]);
  let catalogAddons = $state<AddonSummary[]>([]);
  let usingDefaults = $state(true);
  let addonsLoading = $state(false);
  let addonUrl = $state("");
  let addonBusy = $state<string | null>(null);   // url currently mutating
  let addonError = $state<string | null>(null);

  async function loadAddons() {
    addonsLoading = true; addonError = null;
    try {
      const r = await api.addons.list();
      installedAddons = r.installed;
      catalogAddons = r.catalog;
      usingDefaults = r.using_defaults;
    } catch (e) { addonError = String(e); }
    finally { addonsLoading = false; }
  }
  async function addAddon(url: string) {
    const u = url.trim();
    if (!u) return;
    addonBusy = u; addonError = null;
    try {
      const r = await api.addons.add(u);
      if (!r.ok) { addonError = r.error ?? "could not add addon"; return; }
      addonUrl = "";
      await loadAddons();
    } catch (e) { addonError = String(e); }
    finally { addonBusy = null; }
  }
  async function removeAddon(url: string) {
    addonBusy = url; addonError = null;
    try {
      await api.addons.remove(url);
      await loadAddons();
    } catch (e) { addonError = String(e); }
    finally { addonBusy = null; }
  }
  $effect(() => { if (view === "addons" && !installedAddons.length && !addonsLoading) loadAddons(); });

  // ── Boot: fetch RD account state (gives us premium badge) ──────────────
  onMount(async () => {
    try { account = await api.account(); } catch (e) { /* non-fatal */ }
    const tg = (window as any).Telegram?.WebApp;
    if (tg?.BackButton) {
      // Inside Telegram: the Mini App back chevron drives navigation.
      tg.BackButton.onClick(() => { if (!goBackOneStep()) tg.close?.(); });
    } else {
      // Standalone (the Android WebView app / a plain browser): trap the
      // hardware/browser Back button so it steps back through Theater views
      // instead of leaving straight to the SMDL home. We keep one buffer
      // history entry; each Back press pops it, we unwind one view level and
      // re-push the buffer. Only when there's nothing left to unwind do we
      // actually leave to /app.
      history.pushState({ theater: true }, "");
      window.addEventListener("popstate", onPopState);
    }
    updateBack();
  });

  // Unwind one level of Theater navigation. Returns false when already at the
  // root (discover) — i.e. there is nowhere left to go inside the SPA.
  function goBackOneStep(): boolean {
    if (view === "grab") { view = "detail"; return true; }
    if (view === "detail") { view = returnTo; selected = null; streams = []; return true; }
    if (view !== "discover") { view = "discover"; return true; }
    return false;
  }

  function onPopState() {
    if (goBackOneStep()) history.pushState({ theater: true }, "");
    else exitToHome();
  }

  function updateBack() {
    const tg = (window as any).Telegram?.WebApp;
    if (!tg?.BackButton) return;
    if (view === "discover") tg.BackButton.hide();
    else tg.BackButton.show();
  }
  $effect(() => updateBack());

  // Navigate back to the Sentinel Media home (#69) — the Theater is a
  // full-page nav target, so leaving it means leaving the SPA.
  function exitToHome() { window.location.href = "/app"; }

  // ── Search ─────────────────────────────────────────────────────────────
  // Cinemeta returns both movies and series for "top" catalog searches;
  // we run BOTH queries and merge results so a series like Breaking Bad
  // and a movie like Inception coexist in the result grid.
  async function doSearch() {
    const q = query.trim();
    if (!q) return;
    searching = true; lastError = null;
    try {
      const [m, s] = await Promise.all([
        api.search(q, "movie").catch(() => ({ results: [] as MetaItem[] })),
        api.search(q, "series").catch(() => ({ results: [] as MetaItem[] })),
      ]);
      results = [...m.results, ...s.results].slice(0, 24);
    } catch (e) { lastError = String(e); }
    finally { searching = false; }
  }

  // ── Open detail ────────────────────────────────────────────────────────
  // Movies: fetch streams immediately.
  // Series: fetch the episode list first; streams come AFTER user picks
  // a specific episode.
  // Follow-a-show: auto-download new episodes of a followed series.
  let following = $state(false);
  async function toggleFollow() {
    if (!selected || selected.type !== "series") return;
    try {
      const r = await api.follow.set(selected.id, !following, selected.name, selected.poster ?? "");
      following = r.following;
    } catch (e) { lastError = String(e); }
  }

  async function openDetail(m: MetaItem) {
    returnTo = "discover";
    selected = m; streams = []; episodes = []; pickedEpisode = null;
    view = "detail"; lastError = null;
    following = false;
    if (m.type === "series") {
      api.follow.status(m.id).then(r => { following = r.following; }).catch(() => {});
    }
    if (m.type === "series") {
      episodesLoading = true;
      try {
        const r = await api.episodes(m.id);
        episodes = r.episodes;
        // Default to season 1, or the lowest available
        activeSeason = episodes.length ? Math.min(...episodes.map(e => e.season)) : 1;
      } catch (e) { lastError = String(e); }
      finally { episodesLoading = false; }
      return;
    }
    streamsLoading = true;
    try {
      const r = await api.streams(m.id, m.type, "1080p");
      streams = r.streams;
    } catch (e) { lastError = String(e); }
    finally { streamsLoading = false; }
  }

  // ── Pick a specific episode (series) ───────────────────────────────────
  async function pickEpisode(ep: EpisodeMeta) {
    if (!selected) return;
    pickedEpisode = ep; streams = []; streamsLoading = true; lastError = null;
    try {
      // ep.id is "tt0903747:1:1" — the Stremio addon stream content_id
      const r = await api.streams(ep.id, "series", "1080p");
      streams = r.streams;
    } catch (e) { lastError = String(e); }
    finally { streamsLoading = false; }
  }

  // ── Derived: seasons available in the current series ──────────────────
  const seasons = $derived(
    Array.from(new Set(episodes.map(e => e.season))).sort((a, b) => a - b),
  );
  const visibleEpisodes = $derived(
    episodes.filter(e => e.season === activeSeason),
  );

  // ── Grab → enqueue + poll until streamable or cached ──────────────────
  // The queue endpoint returns immediately with a job_id. We poll
  // /jobs/{id} every 2s — `streaming` means RD has handed us a direct
  // URL we can play right now while caching continues in the background.
  // `cached` means the file is fully on G:\ and we should switch to the
  // local file URL (better for re-watch, instant seek).
  async function grab(s: StreamEntry, index: number = -1) {
    if (!selected || !s.infohash) return;
    if (index < 0) index = streams.findIndex((x) => x === s);
    grabbedIndex = index;
    if (!autoAdvancing) autoAdvanceTries = 0;   // fresh user-initiated grab
    view = "grab"; lastError = null; activeJob = null;
    // For series, the cache key is the episode-specific id (tt0903747:1:1)
    // so re-grabs of a single episode hit the cache, not the series root.
    // The display title gets the SxxExx suffix.
    const isSeriesEpisode = selected.type === "series" && pickedEpisode;
    const cacheId    = isSeriesEpisode ? pickedEpisode!.id     : selected.id;
    const grabTitle  = isSeriesEpisode
      ? `${selected.name} · S${String(pickedEpisode!.season).padStart(2, "0")}E${String(pickedEpisode!.episode).padStart(2, "0")} — ${pickedEpisode!.title}`
      : selected.name;
    try {
      const r = await api.enqueue({
        imdb_id: cacheId, type: selected.type,
        title: grabTitle, infohash: s.infohash,
        file_index: s.file_index ?? undefined,
        source_stream_title: s.title,
        quality: s.quality ?? undefined,
        expected_size: s.size_bytes ?? undefined,
      });
      activeJob = r.job;
      startPolling(r.job_id);
    } catch (e) { lastError = String(e); }
  }

  function startPolling(jobId: number) {
    stopPolling();
    pollHandle = setInterval(async () => {
      try {
        const r = await api.job(jobId);
        activeJob = r.job;
        if (r.job.status === "cached" || r.job.status === "error") {
          stopPolling();
          if (r.job.status === "error" && r.job.error_kind === "rd_infringing") {
            handleInfringing();
          } else {
            autoAdvancing = false;
          }
        }
      } catch (e) { /* keep polling — transient */ }
    }, 2000);
  }

  // RD refused this release (not cached). Mark the source dead so it greys out,
  // then hop to the next still-live source — bounded so we don't grind through
  // every dead mirror of an uncached title.
  function handleInfringing() {
    if (grabbedIndex >= 0 && streams[grabbedIndex]) {
      streams[grabbedIndex] = { ...streams[grabbedIndex], rd_blocked: true };
    }
    if (autoAdvanceTries >= MAX_AUTO_ADVANCE) { autoAdvancing = false; return; }
    const next = streams.findIndex(
      (s, i) => i > grabbedIndex && s.has_magnet && !s.rd_blocked,
    );
    if (next < 0) { autoAdvancing = false; return; }   // nothing left to try
    autoAdvancing = true;
    autoAdvanceTries += 1;
    grab(streams[next], next);
  }
  function stopPolling() {
    if (pollHandle) { clearInterval(pollHandle); pollHandle = null; }
  }

  // ── Queue view: poll every 2s while view == "queue" ────────────────────
  async function refreshJobs() {
    try { jobs = (await api.jobs()).jobs; } catch (e) { /* ignore */ }
  }
  async function refreshCache() {
    try {
      const r = await api.cache();
      cache = r.entries;
      cacheDisk = r.disk;
      cacheRoot = r.root;
    } catch (e) { /* ignore */ }
  }
  async function purgeCache() {
    if (!confirm(`Delete all ${cache.length} cached file(s)? This cannot be undone.`)) return;
    purging = true; purgeMsg = null;
    try {
      const r = await api.purgeCache();
      purgeMsg = `Purged ${r.deleted} file(s) · ${fmtSize(r.bytes_freed)} freed.`;
      await refreshCache();
    } catch (e) { purgeMsg = String(e); }
    finally { purging = false; }
  }
  $effect(() => {
    // Auto-refresh jobs list while on the queue view
    if (view !== "queue") return;
    refreshJobs();
    const id = setInterval(refreshJobs, 2000);
    return () => clearInterval(id);
  });
  $effect(() => {
    if (view !== "library") return;
    refreshCache();
  });

  // Resolve the playable URL for the current job:
  //   • cached  → local /file/<infohash> (range-served by SMDL)
  //   • else    → direct_url from RD (CDN)
  function playUrl(job: StremioJob | null): string | null {
    if (!job) return null;
    if (job.status === "cached") return api.cachedFileUrl(job.infohash);
    return job.direct_url;
  }

  function fmtPct(p: number) { return Math.max(0, Math.min(100, p)).toFixed(0); }

  // ── P7 settings + resume position ────────────────────────────────────
  type SettingsCat = "accounts" | "playback" | "storage" | "addons";
  let settingsCat = $state<SettingsCat>("accounts");
  let settings = $state<any>(null);
  let settingsLoading = $state(false);
  let settingsSaving = $state(false);
  let resumeOffer = $state<{ position_seconds: number; duration_seconds: number | null } | null>(null);

  async function loadSettings() {
    settingsLoading = true;
    try { settings = (await api.settings.get()).settings; }
    catch (e) { lastError = String(e); }
    finally { settingsLoading = false; }
  }
  async function saveSettings(patch: Record<string, any>) {
    settingsSaving = true;
    try { settings = (await api.settings.set(patch)).settings; }
    catch (e) { lastError = String(e); }
    finally { settingsSaving = false; }
  }
  $effect(() => { if (view === "settings" && !settings) loadSettings(); });

  // ── Real-Debrid token (owner pastes it when missing/rotated) ──────────
  let rdStatus = $state<{ set: boolean; masked: string | null;
                          source: string | null; editable: boolean } | null>(null);
  let rdTokenInput = $state("");
  let rdSaving = $state(false);
  let rdSaveMsg = $state<string | null>(null);

  async function loadRdStatus() {
    try { rdStatus = await api.rdToken.status(); } catch (e) { lastError = String(e); }
  }
  async function saveRdToken() {
    if (!rdTokenInput.trim()) return;
    rdSaving = true; rdSaveMsg = null;
    try {
      const r = await api.rdToken.set(rdTokenInput.trim());
      rdStatus = r.status;
      rdTokenInput = "";
      account = r.account;
      rdSaveMsg = r.account?.ok
        ? `Saved — RD account ${r.account.username ?? ""} active.`
        : `Saved, but RD rejected it: ${r.account?.error ?? "unknown error"}`;
    } catch (e) { rdSaveMsg = String(e); }
    finally { rdSaving = false; }
  }
  $effect(() => { if (view === "settings" && !rdStatus) loadRdStatus(); });

  // ── PIA VPN status (read-only; powers the direct-torrent fallback) ────
  let piaStatus = $state<{ configured: boolean; username: string | null;
                           region: string; source: string } | null>(null);
  async function loadPiaStatus() {
    try { piaStatus = await api.pia.status(); } catch (e) { lastError = String(e); }
  }
  $effect(() => { if (view === "settings" && !piaStatus) loadPiaStatus(); });

  // Resume offer: when entering player, check for saved position.
  async function checkResume(imdb: string, vid: HTMLVideoElement) {
    try {
      const r = await api.position.get(imdb);
      if (r.position && r.position.position_seconds > 30) {
        resumeOffer = r.position;
      }
    } catch (_) {}
  }
  function acceptResume(vid: HTMLVideoElement) {
    if (resumeOffer) {
      vid.currentTime = resumeOffer.position_seconds;
      vid.play().catch(() => {});
    }
    resumeOffer = null;
  }
  function declineResume() { resumeOffer = null; }

  // Persist position on timeupdate (throttled to once per ~5s)
  let lastSaveAt = 0;
  function persistPosition(imdb: string, vid: HTMLVideoElement) {
    const now = Date.now();
    if (now - lastSaveAt < 5000) return;
    lastSaveAt = now;
    if (vid.currentTime < 5 || !isFinite(vid.duration)) return;
    api.position.save({
      imdb_id: imdb,
      position_seconds: vid.currentTime,
      duration_seconds: isFinite(vid.duration) ? vid.duration : null,
    }).catch(() => {});
  }

  // ── P6 Trakt scrobble bridge ─────────────────────────────────────────
  // Wire <video> play/pause/ended → /scrobble. Errors are intentionally
  // silenced (Trakt outage shouldn't break playback).
  let traktConnected = $state(false);
  $effect(() => {
    api.trakt.status().then(r => { traktConnected = !!r.connected; }).catch(() => {});
  });

  // ── Hardened Trakt connect (device-code) ────────────────────────────────
  // The WebView suspends our JS when you leave for trakt.tv, which paused the
  // old setInterval poll (so the token never got exchanged). Keep the
  // device-code in component state, and ALSO poll on visibility/focus so
  // "authorize → come back" connects immediately, plus a manual "check now".
  let traktConnecting = $state(false);
  let traktDevice = $state<{ device_code: string; user_code: string;
                             verification_url: string; interval: number } | null>(null);
  let traktPollTimer: ReturnType<typeof setInterval> | null = null;

  function traktStopPoll() {
    if (traktPollTimer != null) { clearInterval(traktPollTimer); traktPollTimer = null; }
  }
  async function traktPollOnce(): Promise<void> {
    if (!traktDevice) return;
    try {
      const pr = await api.trakt.connectPoll(traktDevice.device_code);
      if (pr.status === "connected") {
        traktConnected = true; traktConnecting = false; traktDevice = null; traktStopPoll();
      } else if (pr.status === "error") {
        lastError = pr.error ?? "trakt poll error";
        traktConnecting = false; traktDevice = null; traktStopPoll();
      }
    } catch (_) { /* keep waiting */ }
  }
  async function traktStartConnect() {
    lastError = null;
    const r = await api.trakt.connectStart();
    if (!r.ok) { lastError = r.error ?? "trakt connect failed"; return; }
    traktDevice = { device_code: r.device_code, user_code: r.user_code,
                    verification_url: r.verification_url, interval: r.interval || 5 };
    traktConnecting = true;
    traktStopPoll();
    traktPollTimer = setInterval(traktPollOnce, traktDevice.interval * 1000);
    try { window.open(r.verification_url, "_blank"); } catch (_) { /* WebView may block */ }
  }
  function traktCancelConnect() { traktConnecting = false; traktDevice = null; traktStopPoll(); }

  // Resume the poll the instant the page is visible/focused again — WebView
  // suspends background timers while you're over on trakt.tv.
  $effect(() => {
    const onWake = () => { if (traktDevice && document.visibilityState === "visible") traktPollOnce(); };
    document.addEventListener("visibilitychange", onWake);
    window.addEventListener("focus", onWake);
    return () => { document.removeEventListener("visibilitychange", onWake);
                   window.removeEventListener("focus", onWake); };
  });

  // ── Trakt watchlist (rendered in the Library tab) ───────────────────────
  let watchlist = $state<MetaItem[]>([]);
  let watchlistType = $state<"movies" | "shows">("movies");
  let watchlistLoading = $state(false);
  async function loadWatchlist() {
    watchlistLoading = true;
    try { const r = await api.trakt.watchlist(watchlistType); watchlist = r.ok ? r.items : []; }
    catch (_) { watchlist = []; }
    finally { watchlistLoading = false; }
  }
  function setWatchlistType(t: "movies" | "shows") {
    if (watchlistType === t) return;
    watchlistType = t; watchlist = []; loadWatchlist();
  }
  $effect(() => {
    if (view === "library" && traktConnected && !watchlist.length && !watchlistLoading) loadWatchlist();
  });

  async function scrobble(event: "start" | "pause" | "stop", vid: HTMLVideoElement) {
    if (!traktConnected || !activeJob || !selected) return;
    const pct = vid.duration > 0 ? (vid.currentTime / vid.duration) * 100 : 0;
    const ep = pickedEpisode;
    try {
      await api.trakt.scrobble({
        imdb_id: ep?.id ?? selected.id,
        type: selected.type,
        season: ep?.season ?? null,
        episode: ep?.episode ?? null,
        progress_pct: pct,
        event,
      });
    } catch (_) { /* silent */ }
  }
</script>

<!-- ── Layout ─────────────────────────────────────────────────────────── -->
<div class="min-h-screen bg-background text-foreground">
  <!-- Header -->
  <header class="sticky top-0 z-10 border-b border-border bg-background/95 backdrop-blur
                 px-4 py-3 flex items-center gap-3">
    {#if view === "detail"}
      <button onclick={() => { view = returnTo; selected = null; streams = []; }}
              class="text-muted-foreground hover:text-foreground" aria-label="back">
        <ArrowLeft class="size-5" />
      </button>
    {:else if view !== "discover"}
      <button onclick={() => { view = "discover"; }}
              class="text-muted-foreground hover:text-foreground" aria-label="back">
        <ArrowLeft class="size-5" />
      </button>
    {:else}
      <button onclick={exitToHome}
              class="text-muted-foreground hover:text-foreground" aria-label="back to Sentinel Media" title="Back to Sentinel Media">
        <ArrowLeft class="size-5" />
      </button>
    {/if}
    <h1 class="text-base font-semibold flex-1">
      {view === "detail" ? (selected?.name ?? "…") : "Theater"}
    </h1>
    {#if account?.ok && account.is_premium}
      <Badge variant="secondary" class="text-[10px]">
        RD · {account.days_left?.toFixed(0)}d
      </Badge>
    {:else if account?.ok === false}
      <Badge variant="destructive" class="text-[10px]">no RD</Badge>
    {/if}
  </header>

  <main class="px-4 py-4 max-w-4xl mx-auto">
    {#if lastError}
      <div class="mb-3 rounded-md border border-destructive/40 bg-destructive/10
                   px-3 py-2 text-sm text-destructive-foreground">{lastError}</div>
    {/if}

    {#snippet posterRow(title: string, items: DiscoverItem[], showProgress: boolean)}
      <section class="mb-5">
        <h2 class="text-sm font-semibold mb-2 text-foreground">{title}</h2>
        <div class="flex gap-3 overflow-x-auto pb-2 -mx-4 px-4 snap-x">
          {#each items as m (m.id)}
            <button onclick={() => openDetail(m)}
                    class="group shrink-0 w-36 sm:w-44 text-left snap-start">
              <div class="aspect-[2/3] rounded-lg overflow-hidden bg-muted border border-border
                          group-hover:border-primary group-hover:scale-105 group-hover:shadow-xl
                          group-hover:shadow-primary/20 transition-all duration-200 relative">
                {#if m.poster}
                  <img src={m.poster} alt={m.name} loading="lazy"
                       class="w-full h-full object-cover" />
                {:else}
                  <div class="w-full h-full flex items-center justify-center">
                    <Film class="size-8 text-muted-foreground" />
                  </div>
                {/if}
                {#if showProgress && m.progress_pct}
                  <div class="absolute bottom-0 inset-x-0 h-1 bg-black/50 z-10">
                    <div class="h-full bg-primary" style="width: {m.progress_pct}%"></div>
                  </div>
                {/if}
                <!-- #70 hover preview overlay -->
                <div class="absolute inset-0 flex flex-col justify-end p-2
                            bg-gradient-to-t from-black/90 via-black/40 to-transparent
                            opacity-0 group-hover:opacity-100 transition-opacity duration-200">
                  <div class="flex items-center gap-1 text-[10px] text-white/90 mb-0.5">
                    {#if m.year}<span>{m.year}</span>{/if}
                    {#if m.imdb_rating}<span>★ {m.imdb_rating.toFixed(1)}</span>{/if}
                  </div>
                  {#if m.description}
                    <p class="text-[9px] leading-tight text-white/80 line-clamp-4">{m.description}</p>
                  {/if}
                  <div class="mt-1.5 inline-flex items-center gap-1 text-[10px] font-semibold text-primary-foreground
                              bg-primary rounded px-2 py-0.5 self-start">
                    <Play class="size-3" /> Open
                  </div>
                </div>
              </div>
              <div class="mt-1 text-xs font-medium truncate">{m.name}</div>
              <div class="text-[10px] text-muted-foreground truncate">
                {#if m.year}{m.year}{/if}{#if m.imdb_rating} · ★ {m.imdb_rating.toFixed(1)}{/if}
              </div>
            </button>
          {/each}
        </div>
      </section>
    {/snippet}

    <!-- ── DISCOVER VIEW (search + Stremio-style home rows) ────────────── -->
    {#if view === "discover"}
      <!-- Search bar lives on Home now (#merge-search-into-home) -->
      <form onsubmit={(e) => { e.preventDefault(); doSearch(); }}
            class="flex gap-2 mb-4">
        <Input type="search" placeholder="Search movies & series…  e.g. Inception"
                bind:value={query} class="flex-1" />
        <Button type="submit" disabled={searching || !query.trim()}>
          {#if searching}
            <Loader2 class="animate-spin" />
          {:else}
            <Search />
          {/if}
          Search
        </Button>
        {#if results.length || query.trim()}
          <Button variant="outline" type="button"
                  onclick={() => { query = ""; results = []; }}>Clear</Button>
        {/if}
      </form>

      {#if searching}
        <Card><CardContent class="py-12 text-center">
          <Loader2 class="animate-spin size-6 mx-auto text-muted-foreground" />
          <CardDescription class="mt-2">Searching…</CardDescription>
        </CardContent></Card>
      {:else if results.length}
        <!-- Search results replace the home rows while active -->
        <div class="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-3">
          {#each results as m (m.id)}
            <button onclick={() => openDetail(m)}
                    class="group text-left rounded-lg overflow-hidden border border-border
                           hover:border-primary transition-colors">
              <div class="aspect-[2/3] bg-muted overflow-hidden">
                {#if m.poster}
                  <img src={m.poster} alt={m.name} loading="lazy"
                       class="w-full h-full object-cover group-hover:scale-105 transition-transform" />
                {:else}
                  <div class="w-full h-full flex items-center justify-center">
                    <Film class="size-8 text-muted-foreground" />
                  </div>
                {/if}
              </div>
              <div class="p-2">
                <div class="text-sm font-medium truncate">{m.name}</div>
                <div class="flex items-center gap-2 text-xs text-muted-foreground">
                  {#if m.year}<span>{m.year}</span>{/if}
                  {#if m.imdb_rating}<span>· ★ {m.imdb_rating.toFixed(1)}</span>{/if}
                </div>
              </div>
            </button>
          {/each}
        </div>
      {:else if discoverLoading && !discover}
        <Card><CardContent class="py-12 text-center">
          <Loader2 class="animate-spin size-6 mx-auto text-muted-foreground" />
          <CardDescription class="mt-2">Loading discovery…</CardDescription>
        </CardContent></Card>
      {:else if discover}
        {#if discover.continue_watching.length}
          {@render posterRow("Continue Watching", discover.continue_watching, true)}
        {/if}
        {#if discover.popular_movies.length}
          {@render posterRow("Popular Movies", discover.popular_movies, false)}
        {/if}
        {#if discover.popular_series.length}
          {@render posterRow("Popular Series", discover.popular_series, false)}
        {/if}
        {#if !discover.continue_watching.length && !discover.popular_movies.length && !discover.popular_series.length}
          <Card class="text-center py-12"><CardContent>
            <Film class="size-12 text-muted-foreground mx-auto mb-3" />
            <CardDescription>Nothing to show yet — search above to get started.</CardDescription>
          </CardContent></Card>
        {/if}
      {/if}
    {/if}

    <!-- ── DETAIL VIEW ──────────────────────────────────────────────── -->
    {#if view === "detail" && selected}
      <div class="grid grid-cols-[100px_1fr] sm:grid-cols-[160px_1fr] gap-4 mb-4">
        {#if selected.poster}
          <img src={selected.poster} alt={selected.name}
               class="rounded-md w-full aspect-[2/3] object-cover" />
        {:else}
          <div class="rounded-md w-full aspect-[2/3] bg-muted flex items-center justify-center">
            <Film class="size-8 text-muted-foreground" />
          </div>
        {/if}
        <div class="min-w-0">
          <h2 class="text-lg font-semibold">{selected.name}</h2>
          <div class="text-sm text-muted-foreground mb-2 flex flex-wrap gap-2">
            {#if selected.year}<span>{selected.year}</span>{/if}
            {#if selected.imdb_rating}<span>· IMDB ★ {selected.imdb_rating.toFixed(1)}</span>{/if}
          </div>
          <div class="flex flex-wrap gap-1 mb-2">
            {#each selected.genres ?? [] as g}
              <Badge variant="outline" class="text-[10px]">{g}</Badge>
            {/each}
          </div>
          {#if selected.description}
            <p class="text-xs text-muted-foreground line-clamp-5">{selected.description}</p>
          {/if}
        </div>
      </div>

      <!-- Follow: auto-download new episodes (series only) -->
      {#if selected.type === "series"}
        <div class="mb-4">
          <Button variant={following ? "secondary" : "default"} size="sm" onclick={toggleFollow}>
            {#if following}
              <Check class="size-3" /> Following — auto-downloading new episodes
            {:else}
              <Plus class="size-3" /> Follow — auto-download new episodes
            {/if}
          </Button>
        </div>
      {/if}

      <!-- Episode picker — series only. Tabs by season, list per season -->
      {#if selected.type === "series"}
        {#if episodesLoading}
          <div class="text-sm text-muted-foreground py-4 flex items-center gap-2">
            <Loader2 class="size-4 animate-spin" /> Loading episodes…
          </div>
        {:else if !episodes.length}
          <div class="text-sm text-muted-foreground">No episodes found for this series.</div>
        {:else}
          <!-- Season tabs -->
          <div class="flex gap-1 mb-3 overflow-x-auto pb-1">
            {#each seasons as s}
              <button onclick={() => { activeSeason = s; pickedEpisode = null; streams = []; }}
                      class="px-3 py-1.5 rounded-md text-xs font-medium whitespace-nowrap
                             {activeSeason === s
                              ? 'bg-primary text-primary-foreground'
                              : 'bg-secondary text-secondary-foreground hover:bg-secondary/70'}">
                Season {s}
              </button>
            {/each}
          </div>

          <!-- Episode list -->
          <div class="space-y-1 mb-4">
            {#each visibleEpisodes as ep (ep.id)}
              <button onclick={() => pickEpisode(ep)}
                      class="w-full text-left p-2 rounded-md flex items-start gap-3
                             {pickedEpisode?.id === ep.id
                              ? 'bg-secondary border border-primary'
                              : 'hover:bg-secondary/50 border border-transparent'}">
                <div class="text-xs font-mono text-muted-foreground min-w-[40px]">
                  S{String(ep.season).padStart(2, "0")}E{String(ep.episode).padStart(2, "0")}
                </div>
                <div class="flex-1 min-w-0">
                  <div class="text-sm font-medium truncate">{ep.title}</div>
                  {#if ep.released || ep.runtime}
                    <div class="text-[10px] text-muted-foreground">
                      {ep.released ? new Date(ep.released).toLocaleDateString() : ""}
                      {#if ep.runtime}· {ep.runtime}m{/if}
                    </div>
                  {/if}
                </div>
                {#if pickedEpisode?.id === ep.id}
                  <Tv class="size-4 text-primary mt-0.5" />
                {/if}
              </button>
            {/each}
          </div>
        {/if}
      {/if}

      <h3 class="text-sm font-semibold mb-2 text-muted-foreground uppercase tracking-wide">
        {#if selected.type === "series" && pickedEpisode}
          Streams · S{String(pickedEpisode.season).padStart(2, "0")}E{String(pickedEpisode.episode).padStart(2, "0")} ({streams.length})
        {:else if selected.type === "series"}
          Pick an episode to see streams
        {:else}
          Streams ({streams.length})
        {/if}
      </h3>

      {#if streamsLoading}
        <Card><CardContent class="py-8 text-center">
          <Loader2 class="animate-spin size-6 mx-auto text-muted-foreground" />
          <CardDescription class="mt-2">Fanning out across addons…</CardDescription>
        </CardContent></Card>
      {/if}

      <div class="space-y-2">
        {#each streams as s, i (i)}
          <Card class={s.rd_blocked ? "opacity-50" : ""}>
            <CardContent class="p-3 flex items-center gap-3">
              <div class="flex flex-col items-center min-w-[64px]">
                {#if s.quality}<Badge>{s.quality}</Badge>{/if}
                {#if s.seeders != null}
                  <span class="text-[10px] text-muted-foreground mt-1">{s.seeders}↑</span>
                {/if}
              </div>
              <div class="flex-1 min-w-0">
                <div class="text-xs text-muted-foreground mb-1">{s.source_addon}</div>
                <div class="text-sm truncate" title={s.title}>{s.title.split("\n")[0]}</div>
                <div class="text-[10px] text-muted-foreground">
                  {fmtSize(s.size_bytes)}
                  {#if s.rd_blocked}· <span class="text-amber-500">Not cached on RD</span>{/if}
                </div>
              </div>
              {#if s.rd_blocked}
                <Button size="sm" variant="outline" disabled>Not cached</Button>
              {:else}
                <Button size="sm" disabled={!s.has_magnet} onclick={() => grab(s, i)}>
                  <Play class="size-3" /> Grab
                </Button>
              {/if}
            </CardContent>
          </Card>
        {/each}
      </div>
    {/if}

    <!-- ── GRAB VIEW (live job state) ──────────────────────────────── -->
    {#if view === "grab" && activeJob}
      {@const url = playUrl(activeJob)}
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2">
            {activeJob.title}
            {#if activeJob.status === "cached"}
              <Badge variant="secondary"><HardDrive class="size-3 mr-1" /> Cached</Badge>
            {:else if activeJob.status === "streaming"}
              <Badge>Streaming</Badge>
            {:else if activeJob.status === "downloading"}
              <Badge variant="outline"><Shield class="size-3 mr-1" /> Downloading via VPN…</Badge>
            {:else if activeJob.status === "resolving"}
              <Badge variant="outline">Resolving…</Badge>
            {:else if activeJob.status === "error" && autoAdvancing}
              <Badge variant="outline">Trying next source…</Badge>
            {:else if activeJob.status === "error"}
              <Badge variant="destructive">Error</Badge>
            {/if}
          </CardTitle>
          <CardDescription>{activeJob.source_stream_title?.split("\n")[0] ?? activeJob.filename}</CardDescription>
        </CardHeader>
        <CardContent>
          {#if activeJob.status === "resolving" || activeJob.status === "queued"}
            <div class="flex items-center gap-3 py-6 justify-center text-muted-foreground">
              <Loader2 class="animate-spin size-5" />
              <span class="text-sm">Asking Real-Debrid… (can take up to 5 min on uncached torrents)</span>
            </div>
          {:else if activeJob.status === "downloading"}
            <div class="py-4">
              <div class="flex items-center gap-3 mb-3 text-muted-foreground">
                <Loader2 class="animate-spin size-5" />
                <span class="text-sm">
                  Not on Real-Debrid — pulling it directly over your PIA VPN.
                  Finding peers… playback starts as soon as the first pieces land.
                </span>
              </div>
              <div class="flex justify-between items-center text-xs text-muted-foreground mb-1">
                <span class="flex items-center gap-1"><Shield class="size-3" /> Downloading via VPN</span>
                <span>{fmtPct(activeJob.progress)}%</span>
              </div>
              <div class="w-full h-1.5 bg-secondary rounded-full overflow-hidden">
                <div class="h-full bg-primary transition-all" style="width: {activeJob.progress}%"></div>
              </div>
            </div>
          {:else if activeJob.status === "error" && autoAdvancing}
            <div class="flex items-center gap-3 py-6 justify-center text-muted-foreground">
              <Loader2 class="animate-spin size-5" />
              <span class="text-sm">Real-Debrid couldn't serve that release — trying the next source…</span>
            </div>
          {:else if activeJob.status === "error"}
            <div class="text-sm text-destructive">{activeJob.error ?? "Unknown error"}</div>
            {#if activeJob.error_kind === "rd_infringing"}
              <div class="text-xs text-muted-foreground mt-2">
                Real-Debrid only streams releases that are already cached on its servers.
                This one isn't — brand-new releases often aren't cached by anyone yet.
                Tried sources are greyed out below; pick another, or try a more popular title.
              </div>
              <Button size="sm" variant="outline" class="mt-3"
                       onclick={() => { autoAdvancing = false; view = "detail"; }}>
                Back to sources
              </Button>
            {/if}
          {:else if url}
            {@const playId = pickedEpisode?.id ?? selected?.id ?? activeJob.imdb_id}
            <!-- svelte-ignore a11y_media_has_caption -->
            <video src={url} controls playsinline class="w-full rounded-md bg-black aspect-video"
                    onloadedmetadata={(e) => checkResume(playId, e.currentTarget as HTMLVideoElement)}
                    onplay={(e) => scrobble("start", e.currentTarget as HTMLVideoElement)}
                    onpause={(e) => { scrobble("pause", e.currentTarget as HTMLVideoElement);
                                       persistPosition(playId, e.currentTarget as HTMLVideoElement); }}
                    ontimeupdate={(e) => persistPosition(playId, e.currentTarget as HTMLVideoElement)}
                    onended={(e) => scrobble("stop", e.currentTarget as HTMLVideoElement)}></video>

            {#if resumeOffer}
              <div class="mt-2 p-3 rounded-md bg-secondary flex items-center gap-3">
                <div class="flex-1 text-sm">
                  Resume from {Math.floor(resumeOffer.position_seconds / 60)}m{Math.floor(resumeOffer.position_seconds % 60).toString().padStart(2,"0")}s?
                </div>
                <Button size="sm"
                         onclick={() => {
                           const v = document.querySelector("video") as HTMLVideoElement | null;
                           if (v) acceptResume(v);
                         }}>Resume</Button>
                <Button variant="secondary" size="sm" onclick={declineResume}>Start over</Button>
              </div>
            {/if}

            <!-- Live cache progress bar (visible while streaming, dimmed when cached) -->
            <div class="mt-3">
              <div class="flex justify-between items-center text-xs text-muted-foreground mb-1">
                <span>
                  {#if activeJob.status === "cached"}
                    Cached locally · {fmtSize(activeJob.filesize)}
                  {:else if activeJob.status === "streaming"}
                    Streaming via VPN · downloading {fmtSize(activeJob.filesize)}
                  {:else}
                    Caching to G:\ · {fmtSize(activeJob.filesize)}
                  {/if}
                </span>
                <span>{fmtPct(activeJob.progress)}%</span>
              </div>
              <div class="w-full h-1.5 bg-secondary rounded-full overflow-hidden">
                <div class="h-full bg-primary transition-all"
                     style="width: {activeJob.progress}%"></div>
              </div>
            </div>

            <div class="flex gap-2 mt-3">
              {#if activeJob.direct_url}
                <Button variant="secondary" size="sm"
                         onclick={() => activeJob?.direct_url && navigator.clipboard.writeText(activeJob.direct_url)}>
                  Copy URL
                </Button>
              {/if}
              {#if activeJob.filename}
                <a href={url} download={activeJob.filename}
                   class="inline-flex items-center gap-1 text-sm text-primary hover:underline">
                  <Download class="size-3" /> Download
                </a>
              {/if}
            </div>
          {/if}
        </CardContent>
      </Card>
    {/if}

    <!-- ── QUEUE VIEW (live job list) ──────────────────────────────── -->
    {#if view === "queue"}
      {#if !jobs.length}
        <Card class="text-center py-12"><CardContent>
          <ListVideo class="size-12 text-muted-foreground mx-auto mb-3" />
          <CardDescription>No jobs yet. Grab a movie to populate the queue.</CardDescription>
        </CardContent></Card>
      {/if}
      <div class="space-y-2">
        {#each jobs as j (j.id)}
          <Card>
            <CardContent class="p-3">
              <div class="flex items-center gap-3 mb-2">
                <div class="flex-1 min-w-0">
                  <div class="text-sm font-medium truncate">{j.title}</div>
                  <div class="text-[10px] text-muted-foreground truncate">
                    {j.source_stream_title?.split("\n")[0] ?? j.filename ?? j.infohash}
                  </div>
                </div>
                {#if j.status === "cached"}
                  <Badge variant="secondary"><HardDrive class="size-3 mr-1" /> Cached</Badge>
                {:else if j.status === "streaming"}<Badge>Streaming · {fmtPct(j.progress)}%</Badge>
                {:else if j.status === "downloading"}<Badge variant="outline">VPN · {fmtPct(j.progress)}%</Badge>
                {:else if j.status === "caching"}<Badge variant="outline">Caching · {fmtPct(j.progress)}%</Badge>
                {:else if j.status === "resolving"}<Badge variant="outline">Resolving</Badge>
                {:else if j.status === "queued"}<Badge variant="outline">Queued</Badge>
                {:else if j.status === "error"}<Badge variant="destructive">Error</Badge>
                {/if}
              </div>
              {#if j.status !== "queued" && j.status !== "error"}
                <div class="w-full h-1 bg-secondary rounded-full overflow-hidden">
                  <div class="h-full bg-primary transition-all" style="width: {j.progress}%"></div>
                </div>
              {/if}
              {#if j.error}
                <div class="text-xs text-destructive mt-2">{j.error}</div>
              {/if}
            </CardContent>
          </Card>
        {/each}
      </div>
    {/if}

    <!-- ── SETTINGS VIEW (RD / Trakt / cache / addons) ─────────────── -->
    {#if view === "settings"}
      {#if settingsLoading || !settings}
        <Card><CardContent class="py-8 text-center">
          <Loader2 class="animate-spin size-5 mx-auto text-muted-foreground" />
        </CardContent></Card>
      {:else}
        <!-- Category top nav — jump between Settings sections -->
        <div class="sticky top-0 z-10 -mx-4 mb-3 px-4 py-2 bg-background/95 backdrop-blur
                    border-b border-border flex gap-1 overflow-x-auto">
          {#each [
            { id: "accounts", label: "Accounts", icon: KeyRound },
            { id: "playback", label: "Playback", icon: Play },
            { id: "storage",  label: "Storage",  icon: HardDrive },
            { id: "addons",   label: "Addons",   icon: Puzzle },
          ] as cat (cat.id)}
            <button onclick={() => { settingsCat = cat.id as SettingsCat; }}
                    class="shrink-0 inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-xs font-medium
                           {settingsCat === cat.id
                             ? 'bg-primary text-primary-foreground'
                             : 'bg-secondary text-muted-foreground hover:text-foreground'}">
              <cat.icon class="size-3.5" />{cat.label}
            </button>
          {/each}
        </div>

        {#if settingsCat === "accounts"}
        <Card class="mb-3"><CardHeader>
          <CardTitle class="flex items-center gap-2">
            Real-Debrid
            {#if rdStatus?.set}<Badge variant="secondary">Configured {rdStatus.masked}</Badge>
            {:else}<Badge variant="destructive">No token</Badge>{/if}
          </CardTitle>
          <CardDescription>
            Personal API token from
            <a href="https://real-debrid.com/apitoken" target="_blank"
               class="text-primary underline">real-debrid.com/apitoken</a>.
            Used to turn torrents into direct streams.
          </CardDescription>
        </CardHeader><CardContent class="space-y-2">
          {#if rdStatus?.editable === false}
            <p class="text-xs text-muted-foreground">
              Token is set via the <code>RD_API_TOKEN</code> environment variable —
              edit it there to change it.
            </p>
          {:else}
            <div class="flex gap-2">
              <Input type="password" placeholder="Paste RD token…"
                     bind:value={rdTokenInput} class="text-sm" />
              <Button size="sm" disabled={rdSaving || !rdTokenInput.trim()}
                      onclick={saveRdToken}>
                {#if rdSaving}<Loader2 class="animate-spin size-4" />{:else}Save{/if}
              </Button>
            </div>
            {#if rdSaveMsg}
              <p class="text-xs text-muted-foreground">{rdSaveMsg}</p>
            {/if}
          {/if}
        </CardContent></Card>

        <Card class="mb-3"><CardHeader>
          <CardTitle class="flex items-center gap-2">
            PIA VPN
            {#if piaStatus?.configured}<Badge variant="secondary">Configured</Badge>
            {:else if piaStatus}<Badge variant="destructive">Not set</Badge>{/if}
          </CardTitle>
          <CardDescription>
            Private Internet Access powers the direct-torrent fallback — when
            Real-Debrid can't serve an uncached release, the magnet is pulled
            through PIA so your home IP never joins the swarm.
          </CardDescription>
        </CardHeader><CardContent class="space-y-1 text-sm">
          {#if piaStatus?.configured}
            <div class="flex items-center justify-between">
              <span class="text-muted-foreground">Account</span>
              <span>{piaStatus.username ?? "set"}</span>
            </div>
            <div class="flex items-center justify-between">
              <span class="text-muted-foreground">Region</span>
              <span>{piaStatus.region}</span>
            </div>
          {:else if piaStatus}
            <p class="text-xs text-destructive">No PIA credentials found.</p>
          {/if}
          <p class="text-xs text-muted-foreground pt-1">
            Managed in Windows Credential Manager — rotate with
            <code>scripts/rotate_pia_creds.ps1</code>.
          </p>
        </CardContent></Card>
        {/if}

        {#if settingsCat === "playback"}
        <Card class="mb-3"><CardHeader>
          <CardTitle>Playback</CardTitle>
          <CardDescription>Default stream quality + auto-grab behaviour.</CardDescription>
        </CardHeader><CardContent class="space-y-3">
          <div class="flex items-center justify-between">
            <span class="text-sm">Default quality</span>
            <select class="bg-secondary text-sm rounded px-3 py-1.5"
                    value={settings.default_quality}
                    onchange={(e) => saveSettings({ default_quality: (e.currentTarget as HTMLSelectElement).value })}>
              <option value="any">Any</option>
              <option value="720p">720p</option>
              <option value="1080p">1080p</option>
              <option value="2160p">2160p (4K)</option>
            </select>
          </div>
          <div class="flex items-center justify-between">
            <span class="text-sm">Auto-grab top-seeded (series)</span>
            <input type="checkbox" checked={settings.auto_grab_top_seeded}
                   onchange={(e) => saveSettings({ auto_grab_top_seeded: (e.currentTarget as HTMLInputElement).checked })} />
          </div>
        </CardContent></Card>
        {/if}

        {#if settingsCat === "storage"}
        <Card class="mb-3"><CardHeader>
          <CardTitle>Cache</CardTitle>
          <CardDescription>Where grabbed files live + how big the cache can grow.</CardDescription>
        </CardHeader><CardContent class="space-y-3">
          <div class="flex items-center justify-between gap-3">
            <div class="min-w-0">
              <div class="text-sm">Hard cap (GB)</div>
              <div class="text-[10px] text-muted-foreground">
                {#if settings.cache_max_gb}
                  Capped at {settings.cache_max_gb} GB.
                {:else}
                  Default: no fixed cap — LRU evicts at 90% partition full.
                {/if}
              </div>
            </div>
            <input type="number" min="1" placeholder="—"
                   class="bg-secondary text-sm rounded px-3 py-1.5 w-24 text-right shrink-0"
                   value={settings.cache_max_gb ?? ""}
                   onchange={(e) => {
                     const raw = (e.currentTarget as HTMLInputElement).value;
                     saveSettings({ cache_max_gb: raw ? Number(raw) : null });
                   }} />
          </div>
          <div>
            <div class="text-sm mb-1">Cache folder</div>
            <div class="text-[10px] text-muted-foreground mb-1">
              Current: <span class="font-mono">{settings.cache_root ?? "—"}</span>
              {#if settings.cache_path}<Badge variant="secondary" class="ml-1 text-[9px]">custom</Badge>
              {:else}<Badge variant="outline" class="ml-1 text-[9px]">default</Badge>{/if}
            </div>
            <div class="flex gap-2">
              <Input type="text" placeholder="/downloads/Stremio"
                     value={settings.cache_path ?? ""}
                     onchange={(e) => saveSettings({ cache_path: (e.currentTarget as HTMLInputElement).value.trim() })}
                     class="text-xs font-mono" />
            </div>
            <p class="text-[10px] text-muted-foreground mt-1">
              Must be under a mounted volume (e.g. <code>/downloads/…</code>). Empty = default. Existing files aren't moved.
            </p>
          </div>
        </CardContent></Card>
        {/if}

        {#if settingsCat === "accounts"}
        <Card class="mb-3"><CardHeader>
          <CardTitle class="flex items-center gap-2">
            Trakt
            {#if traktConnected}<Badge variant="secondary">Connected</Badge>
            {:else}<Badge variant="outline">Not connected</Badge>{/if}
          </CardTitle>
          <CardDescription>Scrobble Theater playback to your Trakt timeline + import your watchlist.</CardDescription>
        </CardHeader><CardContent>
          {#if traktConnected}
            <Button variant="secondary" size="sm"
                     onclick={async () => { await api.trakt.disconnect(); traktConnected = false; }}>
              Disconnect
            </Button>
          {:else if traktConnecting && traktDevice}
            <div class="space-y-2">
              <p class="text-sm">Open
                <a href={traktDevice.verification_url} target="_blank"
                   class="text-primary underline">{traktDevice.verification_url}</a>
                and enter this code:</p>
              <div class="text-2xl font-mono font-bold tracking-widest text-center
                          bg-muted rounded-md py-2 select-all">{traktDevice.user_code}</div>
              <p class="text-xs text-muted-foreground flex items-center gap-1">
                <Loader2 class="animate-spin size-3" /> Waiting for you to authorize — auto-detects when you return.
              </p>
              <div class="flex gap-2">
                <Button size="sm" onclick={traktPollOnce}>I've authorized — check now</Button>
                <Button variant="ghost" size="sm" onclick={traktCancelConnect}>Cancel</Button>
              </div>
            </div>
          {:else}
            <Button size="sm" onclick={traktStartConnect}>Connect Trakt</Button>
          {/if}
        </CardContent></Card>
        {/if}

        {#if settingsCat === "addons"}
        <Card><CardHeader>
          <CardTitle class="flex items-center gap-2"><Puzzle class="size-4" /> Addons</CardTitle>
          <CardDescription>Manage Stremio-protocol addons in the dedicated Addons tab.</CardDescription>
        </CardHeader><CardContent>
          <Button variant="secondary" size="sm" onclick={() => { view = "addons"; }}>
            <Puzzle class="size-3" /> Open Addons
          </Button>
        </CardContent></Card>
        {/if}
      {/if}
    {/if}

    <!-- ── LIBRARY VIEW (cached files on G:\) ──────────────────────── -->
    {#if view === "library"}
      {#if traktConnected}
        <section class="mb-5">
          <div class="flex items-center justify-between mb-2">
            <h2 class="text-sm font-semibold text-foreground">Trakt Watchlist</h2>
            <div class="inline-flex rounded-md border border-border overflow-hidden text-xs">
              <button class="px-2 py-1 {watchlistType === 'movies' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground'}"
                      onclick={() => setWatchlistType('movies')}>Movies</button>
              <button class="px-2 py-1 {watchlistType === 'shows' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground'}"
                      onclick={() => setWatchlistType('shows')}>Shows</button>
            </div>
          </div>
          {#if watchlistLoading}
            <div class="text-xs text-muted-foreground"><Loader2 class="inline animate-spin size-3" /> Loading watchlist…</div>
          {:else if watchlist.length}
            <div class="flex gap-3 overflow-x-auto pb-2 -mx-4 px-4 snap-x">
              {#each watchlist as m (m.id)}
                <button onclick={() => openDetail(m)} class="group shrink-0 w-28 sm:w-32 text-left snap-start">
                  <div class="aspect-[2/3] rounded-lg overflow-hidden bg-muted border border-border
                              group-hover:border-primary group-hover:scale-105 transition-all duration-200">
                    {#if m.poster}
                      <img src={m.poster} alt={m.name} loading="lazy" class="w-full h-full object-cover" />
                    {:else}
                      <div class="w-full h-full flex items-center justify-center"><Film class="size-6 text-muted-foreground" /></div>
                    {/if}
                  </div>
                  <div class="mt-1 text-[11px] truncate text-foreground">{m.name}</div>
                </button>
              {/each}
            </div>
          {:else}
            <p class="text-xs text-muted-foreground">Your {watchlistType} watchlist is empty — add titles on Trakt and they'll show here.</p>
          {/if}
        </section>
      {/if}
      {#if cacheDisk}
        <div class="mb-2 text-xs text-muted-foreground flex justify-between">
          <span>
            {cache.length} cached · {fmtSize(cache.reduce((a, e) => a + e.filesize, 0))}
          </span>
          <span>
            disk · {fmtSize(cacheDisk.free)} free of {fmtSize(cacheDisk.total)}
            ({cacheDisk.pct_used.toFixed(0)}% used)
          </span>
        </div>
      {/if}
      <div class="mb-4 flex items-center justify-between gap-2">
        {#if cacheRoot}
          <span class="text-[10px] font-mono text-muted-foreground truncate" title={cacheRoot}>{cacheRoot}</span>
        {/if}
        {#if cache.length}
          <Button variant="destructive" size="sm" disabled={purging} onclick={purgeCache}>
            {#if purging}<Loader2 class="animate-spin size-3" />{:else}<Trash2 class="size-3" />{/if}
            Purge cache
          </Button>
        {/if}
      </div>
      {#if purgeMsg}
        <p class="mb-3 text-xs text-muted-foreground">{purgeMsg}</p>
      {/if}
      {#if !cache.length}
        <Card class="text-center py-12"><CardContent>
          <HardDrive class="size-12 text-muted-foreground mx-auto mb-3" />
          <CardDescription>No cached files yet.</CardDescription>
        </CardContent></Card>
      {/if}
      <div class="space-y-2">
        {#each cache as e (e.infohash)}
          <Card><CardContent class="p-3 flex items-center gap-3">
            <div class="flex-1 min-w-0">
              <div class="text-sm font-medium truncate">{e.title}</div>
              <div class="text-[10px] text-muted-foreground truncate">{e.filename}</div>
              <div class="text-[10px] text-muted-foreground">
                {fmtSize(e.filesize)} · played {new Date(e.last_played).toLocaleDateString()}
              </div>
            </div>
            <a href={api.cachedFileUrl(e.infohash)} target="_blank"
               class="inline-flex items-center gap-1 text-sm text-primary hover:underline">
              <Play class="size-3" /> Play
            </a>
          </CardContent></Card>
        {/each}
      </div>
    {/if}

    <!-- ── ADDONS VIEW (#66) ───────────────────────────────────────── -->
    {#if view === "addons"}
      {#if addonError}
        <div class="mb-3 rounded-md border border-destructive/40 bg-destructive/10
                     px-3 py-2 text-sm text-destructive-foreground">{addonError}</div>
      {/if}

      <!-- Add via link -->
      <form onsubmit={(e) => { e.preventDefault(); addAddon(addonUrl); }}
            class="flex gap-2 mb-4">
        <Input type="url" placeholder="https://addon.example/manifest.json"
               bind:value={addonUrl} class="flex-1 text-sm font-mono" />
        <Button type="submit" disabled={!!addonBusy || !addonUrl.trim()}>
          {#if addonBusy === addonUrl.trim()}<Loader2 class="animate-spin size-4" />
          {:else}<Plus class="size-4" />{/if}
          Add
        </Button>
      </form>

      {#if usingDefaults}
        <p class="text-[11px] text-muted-foreground mb-3">
          Using the built-in default addons. Adding one keeps the defaults and appends yours.
        </p>
      {/if}

      <!-- Installed addons — one tile per addon -->
      <h2 class="text-sm font-semibold mb-2">Installed</h2>
      {#if addonsLoading && !installedAddons.length}
        <Card><CardContent class="py-8 text-center">
          <Loader2 class="animate-spin size-5 mx-auto text-muted-foreground" />
        </CardContent></Card>
      {:else}
        <div class="grid grid-cols-1 sm:grid-cols-2 gap-2 mb-6">
          {#each installedAddons as a (a.url)}
            <Card><CardContent class="p-3 flex items-center gap-3">
              <div class="size-10 rounded-md bg-secondary overflow-hidden shrink-0 flex items-center justify-center">
                {#if a.logo}<img src={a.logo} alt={a.name} class="w-full h-full object-cover" />
                {:else}<Puzzle class="size-5 text-muted-foreground" />{/if}
              </div>
              <div class="flex-1 min-w-0">
                <div class="text-sm font-medium truncate">{a.name}</div>
                <div class="text-[10px] text-muted-foreground truncate">
                  {a.resources.join(", ")}{#if a.version} · v{a.version}{/if}
                </div>
              </div>
              <Button variant="ghost" size="sm" disabled={addonBusy === a.url}
                      onclick={() => removeAddon(a.url)} aria-label="remove addon">
                {#if addonBusy === a.url}<Loader2 class="animate-spin size-4" />
                {:else}<Trash2 class="size-4 text-destructive" />{/if}
              </Button>
            </CardContent></Card>
          {/each}
        </div>
      {/if}

      <!-- Discover: curated catalog to add from -->
      {#if catalogAddons.length}
        <h2 class="text-sm font-semibold mb-2">Discover</h2>
        <div class="grid grid-cols-1 sm:grid-cols-2 gap-2">
          {#each catalogAddons as a (a.url)}
            {@const isInstalled = installedAddons.some(x => x.url === a.url)}
            <Card><CardContent class="p-3 flex items-center gap-3">
              <div class="size-10 rounded-md bg-secondary overflow-hidden shrink-0 flex items-center justify-center">
                {#if a.logo}<img src={a.logo} alt={a.name} class="w-full h-full object-cover" />
                {:else}<Puzzle class="size-5 text-muted-foreground" />{/if}
              </div>
              <div class="flex-1 min-w-0">
                <div class="text-sm font-medium truncate">{a.name}</div>
                {#if a.description}
                  <div class="text-[10px] text-muted-foreground line-clamp-2">{a.description}</div>
                {/if}
              </div>
              {#if isInstalled}
                <Badge variant="secondary" class="shrink-0"><Check class="size-3 mr-1" /> Added</Badge>
              {:else}
                <Button variant="secondary" size="sm" disabled={addonBusy === a.url}
                        onclick={() => addAddon(a.url)}>
                  {#if addonBusy === a.url}<Loader2 class="animate-spin size-4" />
                  {:else}<Plus class="size-4" />{/if}
                </Button>
              {/if}
            </CardContent></Card>
          {/each}
        </div>
      {/if}
    {/if}
  </main>

  <!-- Sticky bottom nav — quick switch between search / queue / library -->
  <nav class="fixed bottom-0 inset-x-0 border-t border-border bg-background/95 backdrop-blur
              flex items-stretch text-xs">
    <button onclick={() => { view = "discover"; }}
            class="flex-1 py-3 flex flex-col items-center gap-0.5
                   {view === 'discover' ? 'text-primary' : 'text-muted-foreground hover:text-foreground'}">
      <Home class="size-4" /><span>Home</span>
    </button>
    <button onclick={() => { view = "queue"; }}
            class="flex-1 py-3 flex flex-col items-center gap-0.5
                   {view === 'queue' ? 'text-primary' : 'text-muted-foreground hover:text-foreground'}">
      <ListVideo class="size-4" /><span>Queue</span>
    </button>
    <button onclick={() => { view = "library"; }}
            class="flex-1 py-3 flex flex-col items-center gap-0.5
                   {view === 'library' ? 'text-primary' : 'text-muted-foreground hover:text-foreground'}">
      <HardDrive class="size-4" /><span>Library</span>
    </button>
    <button onclick={() => { view = "addons"; }}
            class="flex-1 py-3 flex flex-col items-center gap-0.5
                   {view === 'addons' ? 'text-primary' : 'text-muted-foreground hover:text-foreground'}">
      <Puzzle class="size-4" /><span>Addons</span>
    </button>
    <button onclick={() => { view = "settings"; }}
            class="flex-1 py-3 flex flex-col items-center gap-0.5
                   {view === 'settings' ? 'text-primary' : 'text-muted-foreground hover:text-foreground'}">
      <Settings class="size-4" /><span>Settings</span>
    </button>
  </nav>
  <!-- Spacer so content isn't hidden behind the fixed nav -->
  <div class="h-16"></div>
</div>
