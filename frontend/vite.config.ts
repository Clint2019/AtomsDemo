import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  const backend = loadEnv(mode, process.cwd(), "ATOMS_").ATOMS_API_PROXY_TARGET || "http://127.0.0.1:8000";
  return { server: { host: "127.0.0.1", allowedHosts: ["app.preview.localhost"],
    proxy: { "/api": backend, "/healthz": backend } } };
});
