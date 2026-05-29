import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { resolve } from "node:path";

// Sentinel Media · Stremio sub-app build.
//
// Output: `../static/stremio/` so the Python miniapp.py route
//   /app/stremio  →  serves static/stremio/index.html
//   (handled by app/miniapp.py:miniapp_stremio)
//
// Base path: `/app/stremio/` because that's the URL the Mini App is
// mounted at — Vite needs this so emitted asset URLs are correct
// (otherwise CSS/JS try to load from `/assets/...` which 404s).
//
// Dev proxy: `/api/miniapp/*` → local SMDL container at 127.0.0.1:8096.
export default defineConfig({
  plugins: [svelte()],
  clearScreen: false,
  base: "/app/stremio/",
  resolve: {
    alias: {
      "$lib": resolve(__dirname, "src/lib"),
    },
  },
  server: {
    port: 5180,
    strictPort: true,
    host: "127.0.0.1",
    proxy: {
      "/api/miniapp": { target: "http://127.0.0.1:8096", changeOrigin: false },
    },
  },
  build: {
    outDir: resolve(__dirname, "../static/stremio"),
    emptyOutDir: true,
    target: "es2022",
    sourcemap: true,
  },
});
