import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      // ws: true also forwards the per-case WebSocket (/api/cases/{id}/ws) used for live messages
      "/api": { target: process.env.ROOTIQ_API_TARGET ?? "http://127.0.0.1:8011", ws: true },
    },
  },
});
