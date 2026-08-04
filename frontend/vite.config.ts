import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/health": "http://127.0.0.1:8000",
      "/runs": "http://127.0.0.1:8000",
      "/checkpoints": "http://127.0.0.1:8000",
      "/catalog": "http://127.0.0.1:8000",
      "/corpus-snapshots": "http://127.0.0.1:8000"
    }
  }
});
