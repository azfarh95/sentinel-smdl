import { vitePreprocess } from "@sveltejs/vite-plugin-svelte";

export default {
  preprocess: vitePreprocess(),
  compilerOptions: {
    // Svelte 5 runes mode — required for shadcn-svelte components.
    runes: true,
  },
};
