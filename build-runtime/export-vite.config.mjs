import { defineConfig } from 'vite';
import { localAppDataPlugin } from './local-app-data.mjs';

export default defineConfig({
  root: 'source',
  base: './',
  publicDir: false,
  css: { postcss: { plugins: [] } },
  esbuild: { jsx: 'automatic' },
  plugins: [localAppDataPlugin()],
  server: { host: '127.0.0.1' },
  preview: { host: '127.0.0.1' },
  build: { outDir: '../dist', emptyOutDir: true, sourcemap: false },
});
