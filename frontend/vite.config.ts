import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => ({
  base: mode === "pages" ? "./" : "/",
  publicDir: mode === "pages" ? "public" : false,
  plugins: [react()],
  server: { host: "127.0.0.1", port: 5555, strictPort: true },
  preview: { host: "127.0.0.1", port: 5555, strictPort: true },
}));
