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
  import { Search, ArrowLeft, Play, Download, Loader2, Film, ListVideo, HardDrive } from "@lucide/svelte";
  import { api, type MetaItem, type StreamEntry, type GrabFile, type RDAccount,
            type StremioJob, type CacheEntry } from "$lib/api";
  import { fmtSize } from "$lib/utils";
  import { Button } from "$lib/components/ui/button";
  import { Input } from "$lib/components/ui/input";
  import { Card, CardHeader, CardTitle, CardDescription, CardContent } from "$lib/components/ui/card";
  import { Badge } from "$lib/components/ui/badge";

  type View = "search" | "detail" | "grab" | "queue" | "library";

  // ── State (Svelte 5 runes) ─────────────────────────────────────────────
  let view = $state<View>("search");
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
  let pollHandle: ReturnType<typeof setInterval> | null = null;

  // ── Boot: fetch RD account state (gives us premium badge) ──────────────
  onMount(async () => {
    try { account = await api.account(); } catch (e) { /* non-fatal */ }
    const tg = (window as any).Telegram?.WebApp;
    tg?.BackButton?.onClick(() => {
      if (view === "grab") { view = "detail"; }
      else if (view === "detail") { view = "search"; selected = null; streams = []; }
      else { tg.close?.(); }
    });
    updateBack();
  });

  function updateBack() {
    const tg = (window as any).Telegram?.WebApp;
    if (!tg?.BackButton) return;
    if (view === "search") tg.BackButton.hide();
    else tg.BackButton.show();
  }
  $effect(() => updateBack());

  // ── Search ─────────────────────────────────────────────────────────────
  async function doSearch() {
    const q = query.trim();
    if (!q) return;
    searching = true; lastError = null;
    try {
      const r = await api.search(q, "movie");
      results = r.results;
    } catch (e) { lastError = String(e); }
    finally { searching = false; }
  }

  // ── Open detail ────────────────────────────────────────────────────────
  async function openDetail(m: MetaItem) {
    selected = m; streams = []; streamsLoading = true; view = "detail"; lastError = null;
    try {
      const r = await api.streams(m.id, m.type, "1080p");
      streams = r.streams;
    } catch (e) { lastError = String(e); }
    finally { streamsLoading = false; }
  }

  // ── Grab → enqueue + poll until streamable or cached ──────────────────
  // The queue endpoint returns immediately with a job_id. We poll
  // /jobs/{id} every 2s — `streaming` means RD has handed us a direct
  // URL we can play right now while caching continues in the background.
  // `cached` means the file is fully on G:\ and we should switch to the
  // local file URL (better for re-watch, instant seek).
  async function grab(s: StreamEntry) {
    if (!selected || !s.infohash) return;
    view = "grab"; lastError = null; activeJob = null;
    try {
      const r = await api.enqueue({
        imdb_id: selected.id, type: selected.type,
        title: selected.name, infohash: s.infohash,
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
        }
      } catch (e) { /* keep polling — transient */ }
    }, 2000);
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
    } catch (e) { /* ignore */ }
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
</script>

<!-- ── Layout ─────────────────────────────────────────────────────────── -->
<div class="min-h-screen bg-background text-foreground">
  <!-- Header -->
  <header class="sticky top-0 z-10 border-b border-border bg-background/95 backdrop-blur
                 px-4 py-3 flex items-center gap-3">
    {#if view !== "search"}
      <button onclick={() => { view = "search"; selected = null; streams = []; }}
              class="text-muted-foreground hover:text-foreground" aria-label="back">
        <ArrowLeft class="size-5" />
      </button>
    {:else}
      <Film class="size-5 text-primary" />
    {/if}
    <h1 class="text-base font-semibold flex-1">
      {view === "search" ? "Stremio" : selected?.name ?? "…"}
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

    <!-- ── SEARCH VIEW ──────────────────────────────────────────────── -->
    {#if view === "search"}
      <form onsubmit={(e) => { e.preventDefault(); doSearch(); }}
            class="flex gap-2 mb-4">
        <Input type="search" placeholder="Search movies…  e.g. Inception"
                bind:value={query} class="flex-1" autofocus />
        <Button type="submit" disabled={searching || !query.trim()}>
          {#if searching}
            <Loader2 class="animate-spin" />
          {:else}
            <Search />
          {/if}
          Search
        </Button>
      </form>

      {#if !results.length && !searching}
        <Card class="text-center py-12">
          <CardContent>
            <Film class="size-12 text-muted-foreground mx-auto mb-3" />
            <CardDescription>
              Type a movie name and hit Search.
              <br />Results come from Cinemeta · streams from Torrentio / Comet.
            </CardDescription>
          </CardContent>
        </Card>
      {/if}

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

      <h3 class="text-sm font-semibold mb-2 text-muted-foreground uppercase tracking-wide">
        Streams ({streams.length})
      </h3>

      {#if streamsLoading}
        <Card><CardContent class="py-8 text-center">
          <Loader2 class="animate-spin size-6 mx-auto text-muted-foreground" />
          <CardDescription class="mt-2">Fanning out across addons…</CardDescription>
        </CardContent></Card>
      {/if}

      <div class="space-y-2">
        {#each streams as s, i (i)}
          <Card>
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
                <div class="text-[10px] text-muted-foreground">{fmtSize(s.size_bytes)}</div>
              </div>
              <Button size="sm" disabled={!s.has_magnet} onclick={() => grab(s)}>
                <Play class="size-3" /> Grab
              </Button>
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
            {:else if activeJob.status === "resolving"}
              <Badge variant="outline">Resolving…</Badge>
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
          {:else if activeJob.status === "error"}
            <div class="text-sm text-destructive">{activeJob.error ?? "Unknown error"}</div>
          {:else if url}
            <!-- svelte-ignore a11y_media_has_caption -->
            <video src={url} controls playsinline class="w-full rounded-md bg-black aspect-video"></video>

            <!-- Live cache progress bar (visible while streaming, dimmed when cached) -->
            <div class="mt-3">
              <div class="flex justify-between items-center text-xs text-muted-foreground mb-1">
                <span>
                  {#if activeJob.status === "cached"}
                    Cached locally · {fmtSize(activeJob.filesize)}
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
                         onclick={() => navigator.clipboard.writeText(activeJob.direct_url!)}>
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

    <!-- ── LIBRARY VIEW (cached files on G:\) ──────────────────────── -->
    {#if view === "library"}
      {#if cacheDisk}
        <div class="mb-4 text-xs text-muted-foreground flex justify-between">
          <span>
            {cache.length} cached · {fmtSize(cache.reduce((a, e) => a + e.filesize, 0))}
          </span>
          <span>
            G:\ disk · {fmtSize(cacheDisk.free)} free of {fmtSize(cacheDisk.total)}
            ({cacheDisk.pct_used.toFixed(0)}% used)
          </span>
        </div>
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
  </main>

  <!-- Sticky bottom nav — quick switch between search / queue / library -->
  <nav class="fixed bottom-0 inset-x-0 border-t border-border bg-background/95 backdrop-blur
              flex items-stretch text-xs">
    <button onclick={() => { view = "search"; }}
            class="flex-1 py-3 flex flex-col items-center gap-0.5
                   {view === 'search' ? 'text-primary' : 'text-muted-foreground hover:text-foreground'}">
      <Search class="size-4" /><span>Search</span>
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
  </nav>
  <!-- Spacer so content isn't hidden behind the fixed nav -->
  <div class="h-16"></div>
</div>
