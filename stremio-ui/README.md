# Sentinel Media · Stremio sub-app

Svelte 5 + shadcn-svelte + Tailwind sub-app for the Sentinel Media (SMDL)
Mini App. Lives at `/app/stremio` inside the Mini App. Backend lives at
`/api/miniapp/stremio/*` (defined in `app/miniapp.py`).

## Architecture

- **Svelte 5 runes mode** (no stores, no `$:` — use `$state` / `$derived`)
- **shadcn-svelte components** are copy-paste into `src/lib/components/ui/`.
  Own the code, edit freely, no version dep.
- **Tailwind 3** with CSS-var theme tokens defined in `src/app.css`.
  Palette tuned to match SMDL's dark aesthetic.
- **TG WebApp SDK** loaded in `index.html`; `main.ts` calls `ready()`+`expand()`.
- **BackButton** is wired in `App.svelte:onMount` — pops view stack.

## Dev

```sh
pnpm install
pnpm dev          # localhost:5180/app/stremio/
                  # proxy: /api/miniapp/* → 127.0.0.1:8096 (SMDL container)
```

## Build

```sh
pnpm build        # → ../static/stremio/  (served by Python miniapp_stremio route)
```

## Add a shadcn-svelte component

shadcn-svelte components are intentionally NOT installed as a dep. To add
one, copy from the upstream registry (https://shadcn-svelte.com/docs/components)
into `src/lib/components/ui/<name>/`. Each component is small enough that
this is faster than a package upgrade dance.

Reusable utility: `cn()` in `$lib/utils` merges class names with
`tailwind-merge` (so `cn("px-4", "px-6")` resolves to `px-6`).
