import { build } from 'vite';
import { rename, writeFile } from 'node:fs/promises';

async function finish(result) {
  await writeFile('/tmp/result.pending.json', JSON.stringify(result));
  await rename('/tmp/result.pending.json', '/tmp/result.json');
}

try {
  await build({
    root: '/opt/runner/site',
    configFile: false,
    clearScreen: false,
    logLevel: 'silent',
    publicDir: false,
    css: { postcss: { plugins: [] } },
    esbuild: { jsx: 'automatic' },
    build: { outDir: '/output', emptyOutDir: true, sourcemap: false },
  });
  await finish({ success: true });
} catch (error) {
  const diagnostic = String(error?.stack ?? error).slice(0, 2000);
  await finish({ success: false, diagnostic });
}
