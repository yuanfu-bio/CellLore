import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    // Ship the verified control-room build with the Python package so a fresh
    // clone and an installed wheel serve the same UI as a developer checkout.
    outDir: "../src/celllore/interfaces/static/ui",
    emptyOutDir: true
  },
  server: {
    proxy: {
      "/health": "http://127.0.0.1:8000",
      "/runs": "http://127.0.0.1:8000",
      "/curation-profiles": "http://127.0.0.1:8000",
      "/checkpoints": "http://127.0.0.1:8000",
      "/catalog": "http://127.0.0.1:8000",
      "/corpus-snapshots": "http://127.0.0.1:8000"
    }
  }
});
