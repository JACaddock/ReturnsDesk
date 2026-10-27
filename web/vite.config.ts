import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev the API runs separately on :8000; in Docker it serves the built files itself.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/chat": "http://localhost:8000" } },
});
