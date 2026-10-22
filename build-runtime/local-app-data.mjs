// Local, single-user application-record adapter for exported demo source.
// It binds only through Vite's loopback server and stores data beside the export.
import { createHash } from 'node:crypto';
import { existsSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';

const dataPath = resolve(process.cwd(), '.atoms-local-data.json');
const collectionPath = /^\/app\/collections\/([a-z][a-z0-9_]*)\/records$/;
const recordPath = /^\/app\/records\/([A-Za-z0-9_-]{1,128})$/;

function load() {
  if (!existsSync(dataPath)) return { records: Object.create(null), mutations: Object.create(null) };
  const parsed = JSON.parse(readFileSync(dataPath, 'utf8'));
  if (!parsed || !parsed.records || !parsed.mutations ||
      typeof parsed.records !== 'object' || typeof parsed.mutations !== 'object' ||
      Array.isArray(parsed.records) || Array.isArray(parsed.mutations)) {
    throw new Error('Local application data file is invalid');
  }
  return { records: Object.assign(Object.create(null), parsed.records),
    mutations: Object.assign(Object.create(null), parsed.mutations) };
}

function save(state) {
  const temporary = `${dataPath}.next`;
  writeFileSync(temporary, JSON.stringify(state, null, 2));
  renameSync(temporary, dataPath);
}

function respond(response, status, body = null) {
  response.statusCode = status;
  response.setHeader('Content-Type', 'application/json; charset=utf-8');
  response.setHeader('Cache-Control', 'no-store');
  response.end(body === null ? '' : JSON.stringify(body));
}

async function bodyOf(request) {
  const parts = [];
  let length = 0;
  for await (const chunk of request) {
    parts.push(chunk);
    length += chunk.length;
    if (length > 1_048_576) throw new Error('Request body is too large');
  }
  return Buffer.concat(parts).toString('utf8');
}

async function handle(request, response, next) {
  const url = new URL(request.url || '/', 'http://127.0.0.1');
  if (!url.pathname.startsWith('/app/')) return next();
  const authority = `127.0.0.1:${request.socket.localPort}`;
  if (request.headers.host !== authority) {
    return respond(response, 403, { error: 'Local host required' });
  }
  if (request.method !== 'GET' && request.headers.origin &&
      request.headers.origin !== `http://${authority}`) {
    return respond(response, 403, { error: 'Same origin required' });
  }
  try {
    const collection = collectionPath.exec(url.pathname);
    const record = recordPath.exec(url.pathname);
    if (request.method === 'GET' && collection) {
      const limit = Number(url.searchParams.get('limit') || 100);
      const offset = Number(url.searchParams.get('cursor') || 0);
      if (!Number.isInteger(limit) || limit < 1 || limit > 100 ||
          !Number.isInteger(offset) || offset < 0) return respond(response, 422, { error: 'Invalid page' });
      const state = load();
      const rows = Object.values(state.records).filter(item =>
        !item.deleted && item.collection === collection[1]);
      rows.sort((a, b) => a.id.localeCompare(b.id));
      return respond(response, 200, {
        items: rows.slice(offset, offset + limit),
        next_cursor: offset + limit < rows.length ? String(offset + limit) : null,
      });
    }
    if ((request.method === 'PUT' || request.method === 'DELETE') && record) {
      const key = request.headers['idempotency-key'];
      if (typeof key !== 'string' || !key || key.length > 200) {
        return respond(response, 422, { error: 'Idempotency-Key required' });
      }
      const raw = request.method === 'PUT' ? await bodyOf(request) : '';
      const fingerprint = createHash('sha256').update(`${request.method}\0${url.pathname}\0${raw}`).digest('hex');
      const state = load();
      const prior = Object.hasOwn(state.mutations, key) ? state.mutations[key] : null;
      if (prior) {
        if (prior.fingerprint !== fingerprint) return respond(response, 409, { error: 'Idempotency conflict' });
        return respond(response, prior.status, prior.body);
      }
      const id = record[1];
      if (request.method === 'DELETE') {
        const current = Object.hasOwn(state.records, id) ? state.records[id] : null;
        if (!current) return respond(response, 404, { error: 'Record not found' });
        current.deleted = true;
        state.mutations[key] = { fingerprint, status: 204, body: null };
        save(state);
        return respond(response, 204);
      }
      let payload;
      try { payload = JSON.parse(raw); } catch { return respond(response, 422, { error: 'Invalid JSON' }); }
      if (!payload || typeof payload.collection !== 'string' ||
          !/^[a-z][a-z0-9_]*$/.test(payload.collection) ||
          typeof payload.json_document !== 'string' ||
          Buffer.byteLength(payload.json_document, 'utf8') > 131_072 ||
          !Number.isInteger(payload.schema_version) || payload.schema_version < 1 ||
          payload.schema_version > 1000 ||
          !Number.isInteger(payload.revision) || payload.revision < 0) {
        return respond(response, 422, { error: 'Invalid record' });
      }
      let document;
      try { document = JSON.parse(payload.json_document); }
      catch { return respond(response, 422, { error: 'Invalid document' }); }
      if (!document || typeof document !== 'object' || Array.isArray(document)) {
        return respond(response, 422, { error: 'Document must be an object' });
      }
      const current = Object.hasOwn(state.records, id) ? state.records[id] : null;
      if (current && (current.deleted || current.collection !== payload.collection)) {
        return respond(response, 403, { error: 'Record scope differs' });
      }
      if ((current && (current.revision !== payload.revision ||
                       payload.schema_version < current.schema_version)) ||
          (!current && payload.revision !== 0)) return respond(response, 409, { error: 'Revision conflict' });
      const saved = { id, collection: payload.collection,
        json_document: JSON.stringify(document), schema_version: payload.schema_version,
        revision: current ? current.revision + 1 : 1 };
      state.records[id] = saved;
      state.mutations[key] = { fingerprint, status: 200, body: saved };
      save(state);
      return respond(response, 200, saved);
    }
    return respond(response, 404, { error: 'Unknown application route' });
  } catch (error) {
    return respond(response, 500, { error: String(error.message || error) });
  }
}

export function localAppDataPlugin() {
  return {
    name: 'atoms-local-app-data',
    configureServer(server) { server.middlewares.use(handle); },
    configurePreviewServer(server) { server.middlewares.use(handle); },
  };
}
