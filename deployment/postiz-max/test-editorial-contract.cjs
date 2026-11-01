const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const vm = require('node:vm');
const tooling = process.env.EDITORIAL_TOOLING || (fs.existsSync('/app/node_modules/typescript') ? '/app/node_modules' : path.resolve(__dirname, '../../data/postiz-editorial-tooling/node_modules'));
const ts = require(path.join(tooling, 'typescript'));
const { planPatch, compilePlans, hash } = require('./patch-editorial.cjs');
const options = { target: ts.ScriptTarget.ES2017, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
  experimentalDecorators: true, emitDecoratorMetadata: true, esModuleInterop: true };
function loadModule(filename, dependencies = {}, globals = {}) {
  const result = ts.transpileModule(fs.readFileSync(path.join(__dirname, filename), 'utf8'), { fileName: filename, compilerOptions: options, reportDiagnostics: true });
  assert.deepEqual((result.diagnostics || []).filter(d => d.category === ts.DiagnosticCategory.Error), []);
  const module = { exports: {} };
  const context = vm.createContext({ module, exports: module.exports, URL, AbortController, TextEncoder, setTimeout, clearTimeout,
    require(name) { if (name in dependencies) return dependencies[name]; throw new Error(`Unexpected dependency ${name}`); }, ...globals });
  vm.runInContext(result.outputText, context, { filename });
  return module.exports;
}
class HttpException extends Error { constructor(body, status) { super(body.code); this.body = body; this.status = status; } }
const decorator = () => () => undefined;
const nest = { HttpException, Controller: decorator, Get: decorator, HttpCode: decorator, Put: decorator, Post: decorator, Req: decorator, Body: decorator };
const env = {
  EDITORIAL_OWNER_USER_ID: 'owner-id', EDITORIAL_OWNER_ORGANIZATION_ID: 'org-id',
  EDITORIAL_INTERNAL_URL: 'http://host.docker.internal:8092/internal/editorial', EDITORIAL_INTERNAL_KEY: 'private-test-key-not-real-00000000',
  FRONTEND_URL: 'http://postiz.test:4007',
};
const request = () => ({ headers: { origin: env.FRONTEND_URL, 'content-type': 'application/json' }, cookies: { auth: 'session' },
  user: { id: 'owner-id', activated: true }, org: { id: 'org-id', users: [{ role: 'SUPERADMIN', disabled: false }] } });
function controller(fetchImpl, overrides = {}) {
  const calls = [];
  const Controller = loadModule('editorial.controller.ts', { '@nestjs/common': nest }, {
    process: { env: { ...env, ...overrides } }, fetch: async (...args) => { calls.push(args); return fetchImpl ? fetchImpl(...args) : response({ revision: 2, paused: true }); },
  }).EditorialController;
  return { instance: new Controller(), calls };
}
const response = (body, status = 200) => ({ ok: status < 300, status, text: async () => JSON.stringify(body) });
const rejected = (promise, code, status) => assert.rejects(promise, error => error.message === code && (!status || error.status === status));

test('cookie owner request forwards fixed path and trusted identity, never browser credentials', async () => {
  const { instance, calls } = controller();
  const req = request(); req.headers['x-editorial-owner'] = 'attacker'; req.headers.cookie = 'secret browser cookie';
  const result = await instance.state(req);
  assert.equal(result.paused, true); assert.equal(calls.length, 1);
  assert.equal(calls[0][0], `${env.EDITORIAL_INTERNAL_URL}/state`);
  assert.equal(calls[0][1].headers['X-Editorial-Owner'], 'owner-id');
  assert.equal(calls[0][1].headers['X-Editorial-Organization'], 'org-id');
  assert.equal(calls[0][1].headers['X-Editorial-Key'], env.EDITORIAL_INTERNAL_KEY);
  assert.equal(JSON.stringify(calls).includes('secret browser cookie'), false);
  assert.equal(calls[0][1].redirect, 'error');
});

test('monitor BFF only forwards fixed read-only owner paths and rejects action query parameters', async () => {
  const { instance, calls } = controller(() => response({ available: false, read_only: true }));
  await instance.monitor(request());
  await instance.monitorPreview(request());
  assert.deepEqual(calls.map(call => call[0]), [`${env.EDITORIAL_INTERNAL_URL}/monitor`, `${env.EDITORIAL_INTERNAL_URL}/monitor-preview`]);
  assert.ok(calls.every(call => call[1].method === 'GET' && call[1].body === undefined));
  const query = request(); query.query = { action: 'send' };
  await rejected(Promise.resolve().then(() => instance.monitor(query)), 'INPUT_INVALID', 422);
  const other = request(); other.user.id = 'other';
  await rejected(Promise.resolve().then(() => instance.monitorPreview(other)), 'EDITORIAL_OWNER_REQUIRED');
  assert.equal(calls.length, 2);
});
for (const [name, mutate, code] of [
  ['no cookie', req => { req.cookies = {}; }, 'EDITORIAL_COOKIE_SESSION_REQUIRED'],
  ['auth header', req => { req.headers.auth = 'jwt'; }, 'EDITORIAL_COOKIE_SESSION_REQUIRED'],
  ['bearer with cookie', req => { req.headers.authorization = 'Bearer x'; }, 'EDITORIAL_COOKIE_SESSION_REQUIRED'],
  ['impersonation', req => { req.cookies.impersonate = 'other'; }, 'EDITORIAL_COOKIE_SESSION_REQUIRED'],
  ['other owner', req => { req.user.id = 'other'; }, 'EDITORIAL_OWNER_REQUIRED'],
  ['inactive user', req => { req.user.activated = false; }, 'EDITORIAL_OWNER_REQUIRED'],
  ['other organization', req => { req.org.id = 'other'; }, 'EDITORIAL_OWNER_REQUIRED'],
  ['nonowner role', req => { req.org.users[0].role = 'ADMIN'; }, 'EDITORIAL_OWNER_REQUIRED'],
  ['disabled membership', req => { req.org.users[0].disabled = true; }, 'EDITORIAL_OWNER_REQUIRED'],
  ['ambiguous membership', req => { req.org.users.push(req.org.users[0]); }, 'EDITORIAL_OWNER_REQUIRED'],
  ['foreign origin', req => { req.headers.origin = 'https://evil.test'; }, 'EDITORIAL_ORIGIN_REQUIRED'],
]) test(`BFF blocks ${name} before internal request`, async () => {
  const { instance, calls } = controller(); const req = request(); mutate(req);
  await rejected(instance.state(req), code); assert.equal(calls.length, 0);
});
test('same-origin browser GET without Origin works, mutations without Origin do not', async () => {
  const { instance, calls } = controller(); const req = request(); delete req.headers.origin; req.headers['sec-fetch-site'] = 'same-origin';
  await instance.settings(req);
  await rejected(Promise.resolve().then(() => instance.control(req, { action: 'pause' })), 'EDITORIAL_ORIGIN_REQUIRED');
  assert.equal(calls.length, 1);
});
test('HTTP LAN GET accepts same-origin Referer without fetch metadata and rejects foreign/missing Referer', async () => {
  const { instance, calls } = controller(); const req = request(); delete req.headers.origin;
  req.headers.referer = `${env.FRONTEND_URL}/editorial`;
  await instance.state(req);
  req.headers.referer = 'http://evil.test/editorial';
  await rejected(instance.state(req), 'EDITORIAL_ORIGIN_REQUIRED');
  delete req.headers.referer;
  await rejected(instance.state(req), 'EDITORIAL_ORIGIN_REQUIRED');
  assert.equal(calls.length, 1);
});
test('MAX browser authorization checks owner session only and makes zero Python calls', async () => {
  const { instance, calls } = controller();
  assert.equal(instance.browserAuthorize(request()), undefined);
  const req = request(); req.user.id = 'other';
  await rejected(Promise.resolve().then(() => instance.browserAuthorize(req)), 'EDITORIAL_OWNER_REQUIRED');
  assert.equal(calls.length, 0);
  const source = fs.readFileSync(path.join(__dirname, 'editorial.controller.ts'), 'utf8').replace(/\r\n/g, '\n');
  assert.ok(source.includes("@Get('/browser-authorize')\n  @HttpCode(204)"));
});
test('settings/control enforce envelopes and revision, no publish endpoint exists', async () => {
  const { instance, calls } = controller(); const req = request();
  await instance.updateSettings(req, { revision: 3, settings: { source: 'nasa_svs' } });
  assert.equal(calls[0][1].method, 'PUT'); assert.deepEqual(JSON.parse(calls[0][1].body), { revision: 3, settings: { source: 'nasa_svs' } });
  await instance.control(req, { action: 'pause', revision: 3 });
  assert.deepEqual(JSON.parse(calls[1][1].body), { action: 'pause', revision: 3 });
  for (const bad of [{ revision: true, settings: {} }, { revision: 2, settings: [], approval: true }, { revision: -1, settings: {} }]) {
    await rejected(Promise.resolve().then(() => instance.updateSettings(req, bad)), 'EDITORIAL_SETTINGS_INVALID', 422);
  }
  await rejected(Promise.resolve().then(() => instance.control(req, { action: 'publish' })), 'EDITORIAL_CONTROL_INVALID', 422);
  for (const revision of [undefined, 0, true, -1, 1.2]) {
    await rejected(Promise.resolve().then(() => instance.control(req, { action: 'pause', revision })), 'EDITORIAL_CONTROL_INVALID', 422);
  }
  assert.equal(calls.length, 2); assert.equal(instance.publish, undefined);
});

test('four-route BFF uses fixed paths and exact action envelopes without forwarding browser credentials', async () => {
  const { instance, calls } = controller(() => response({ targets: [], variants: [], dispatches: [] }));
  const req = request(); req.headers.cookie = 'private-browser-cookie';
  await instance.routes(req);
  assert.equal(calls[0][0], `${env.EDITORIAL_INTERNAL_URL}/routes`);
  assert.equal(calls[0][1].method, 'GET');
  for (const action of ['inspect', 'details', 'preflight', 'preview', 'pause', 'resume', 'reconcile']) {
    await instance.routeAction(req, { action, id: 'saved-object_123' });
    assert.equal(calls.at(-1)[0], `${env.EDITORIAL_INTERNAL_URL}/route-action`);
    assert.deepEqual(JSON.parse(calls.at(-1)[1].body), { action, id: 'saved-object_123' });
  }
  const approval = { action: 'approve', id: 'variant-123', revision: 4,
    content_hash: 'a'.repeat(64), due_at: Math.floor(Date.now() / 1000) + 3600 };
  await instance.routeAction(req, approval);
  assert.deepEqual(JSON.parse(calls.at(-1)[1].body), approval);
  assert.equal(calls.at(-1)[1].headers['X-Editorial-Owner'], env.EDITORIAL_OWNER_USER_ID);
  assert.ok(!JSON.stringify(calls).includes('private-browser-cookie'));
  assert.equal(instance.publish, undefined);
});

test('four-route mutations preserve cookie owner/origin/JSON authorization', async () => {
  for (const mutate of [req => { delete req.headers.origin; }, req => { req.cookies = {}; },
    req => { req.user.id = 'someone-else'; }, req => { req.headers['content-type'] = 'text/plain'; }]) {
    const { instance, calls } = controller(); const req = request(); mutate(req);
    await assert.rejects(Promise.resolve().then(() => instance.routeAction(req, { action: 'inspect', id: 'max_test' })));
    assert.equal(calls.length, 0);
  }
});

test('explicit isolated port18092 forwards to copy API with unchanged trusted identity', async () => {
  const internalUrl = 'http://host.docker.internal:18092/internal/editorial';
  const { instance, calls } = controller(undefined, { EDITORIAL_INTERNAL_PORT: '18092', EDITORIAL_INTERNAL_URL: internalUrl });
  await instance.routes(request());
  assert.equal(calls.length, 1); assert.equal(calls[0][0], `${internalUrl}/routes`);
  assert.equal(calls[0][1].headers['X-Editorial-Owner'], env.EDITORIAL_OWNER_USER_ID);
  assert.equal(calls[0][1].redirect, 'error');
});

test('production default8092 never accepts isolated18092 implicitly', async () => {
  for (const port of [undefined, '', '8092']) {
    const { instance, calls } = controller(undefined, { EDITORIAL_INTERNAL_PORT: port,
      EDITORIAL_INTERNAL_URL: 'http://localhost:18092/internal/editorial' });
    await rejected(instance.state(request()), 'EDITORIAL_NOT_CONFIGURED', 503);
    assert.equal(calls.length, 0);
  }
});

test('port selection remains a two-value allowlist and does not relax URL or owner guards', async () => {
  for (const port of ['8093', '443', '80', '08092', ' 18092', '18092 ', '18092/other', '*']) {
    const { instance, calls } = controller(undefined, { EDITORIAL_INTERNAL_PORT: port });
    await rejected(instance.state(request()), 'EDITORIAL_NOT_CONFIGURED', 503);
    assert.equal(calls.length, 0);
  }
  for (const url of ['https://localhost:18092/internal/editorial', 'http://evil.test:18092/internal/editorial',
    'http://localhost:18092/other', 'http://user:pass@localhost:18092/internal/editorial',
    'http://localhost:18092/internal/editorial?next=evil', 'http://localhost:18092/internal/editorial#fragment']) {
    const { instance, calls } = controller(undefined, { EDITORIAL_INTERNAL_PORT: '18092', EDITORIAL_INTERNAL_URL: url });
    await rejected(instance.state(request()), 'EDITORIAL_NOT_CONFIGURED', 503);
    assert.equal(calls.length, 0);
  }
  const { instance, calls } = controller(undefined, { EDITORIAL_INTERNAL_PORT: '18092',
    EDITORIAL_INTERNAL_URL: 'http://localhost:18092/internal/editorial' });
  const req = request(); req.user.id = 'someone-else';
  await rejected(instance.state(req), 'EDITORIAL_OWNER_REQUIRED');
  assert.equal(calls.length, 0);
});

test('route pages forward only bounded fixed query fields and reject arbitrary or ambiguous input', async () => {
  const { instance, calls } = controller();
  const req = request(); req.query = { variant_page: '12', dispatch_page: '3', dispatch_filter: 'unresolved' };
  await instance.routes(req);
  assert.equal(calls[0][0], `${env.EDITORIAL_INTERNAL_URL}/routes?variant_page=12&dispatch_page=3&dispatch_filter=unresolved`);
  for (const query of [{ url: 'http://other' }, { limit: '1000' }, { variant_page: '-1' },
    { dispatch_page: '1000001' }, { variant_page: '01' }, { variant_page: ['0', '1'] },
    { variant_page: true }, { dispatch_filter: 'unknown' }]) {
    await rejected(Promise.resolve().then(() => instance.routes({ ...request(), query })), 'EDITORIAL_ROUTE_LIST_INVALID', 422);
  }
  assert.equal(calls.length, 1);
});

test('four-route actions reject arbitrary publish, URL/credential keys, malformed IDs and stale schedules', async () => {
  const { instance, calls } = controller();
  const due = Math.floor(Date.now() / 1000) + 3600;
  const valid = { action: 'approve', id: 'variant-123', revision: 1, content_hash: 'b'.repeat(64), due_at: due };
  const invalid = [null, [], 'publish', {}, { action: 'publish', id: 'max_test' },
    { action: 'inspect', id: 'https://evil.test/publish' }, { action: 'inspect', id: '../max_test' },
    { action: 'inspect', id: 'x'.repeat(129) }, { action: 'inspect', id: 'max_test', url: 'http://internal/' },
    { action: 'resume', id: 'max_test', token: 'private' }, { action: 'inspect', id: 'max_test', due_at: due },
    { ...valid, revision: true }, { ...valid, revision: 0 }, { ...valid, content_hash: 'A'.repeat(64) },
    { ...valid, content_hash: 'short' }, { ...valid, due_at: String(due) }, { ...valid, due_at: 1 },
    { ...valid, due_at: due + 0.5 }, { ...valid, body: 'bypass exact saved body' },
    { ...valid, human_checked: true }, { ...valid, api_url: 'https://evil.test' }];
  for (const body of invalid) await rejected(Promise.resolve().then(() => instance.routeAction(request(), body)),
    'EDITORIAL_ROUTE_ACTION_INVALID', 422);
  assert.equal(calls.length, 0);
});
for (const internalUrl of ['https://evil.test/internal/editorial', 'http://host.docker.internal:8092/other',
  'http://user:pass@localhost:8092/internal/editorial', 'http://127.0.0.1:8092/internal/editorial?next=evil', 'http://localhost:8090/internal/editorial']) {
  test(`internal destination rejected: ${new URL(internalUrl).origin}`, async () => {
    const { instance, calls } = controller(undefined, { EDITORIAL_INTERNAL_URL: internalUrl });
    await rejected(instance.state(request()), 'EDITORIAL_NOT_CONFIGURED', 503); assert.equal(calls.length, 0);
  });
}
test('upstream conflicts are sanitized; network and nonJSON errors fail closed', async () => {
  const conflicting = controller(() => response({ code: 'EDITORIAL_REVISION_CONFLICT', message: 'private-stack' }, 409));
  await rejected(conflicting.instance.state(request()), 'EDITORIAL_REVISION_CONFLICT', 409);
  const network = controller(() => { throw new Error('internal-secret-host'); });
  await rejected(network.instance.state(request()), 'EDITORIAL_SERVICE_UNAVAILABLE', 503);
  const html = controller(() => ({ ok: false, status: 500, text: async () => '<html>private stack</html>' }));
  await rejected(html.instance.state(request()), 'EDITORIAL_RESPONSE_INVALID', 502);
});
test('mutations require JSON and missing owner configuration makes zero calls', async () => {
  const first = controller(); const req = request(); req.headers['content-type'] = 'text/plain';
  await rejected(Promise.resolve().then(() => first.instance.control(req, { action: 'pause' })), 'EDITORIAL_JSON_REQUIRED', 415);
  const second = controller(undefined, { EDITORIAL_OWNER_USER_ID: '' });
  await rejected(second.instance.state(request()), 'EDITORIAL_NOT_CONFIGURED', 503);
  assert.equal(first.calls.length + second.calls.length, 0);
});
test('short internal key is rejected before any internal request', async () => {
  const { instance, calls } = controller(undefined, { EDITORIAL_INTERNAL_KEY: 'short' });
  await rejected(instance.state(request()), 'EDITORIAL_NOT_CONFIGURED', 503); assert.equal(calls.length, 0);
});
const formApi = loadModule('editorial-form.ts');
test('new UI/controller modules pass strict semantic TypeScript with installed React types', () => {
  const stubPath = path.join(__dirname, 'editorial-type-stubs.d.ts');
  const stubs = `declare module '@nestjs/common' {
    export class HttpException extends Error { constructor(body: any, status: number); }
    export function Body(...args: any[]): any; export function Controller(...args: any[]): any;
    export function Get(...args: any[]): any; export function HttpCode(...args: any[]): any; export function Post(...args: any[]): any;
    export function Put(...args: any[]): any; export function Req(...args: any[]): any;
  }
  declare module '*.module.css' { const styles: Record<string, string>; export default styles; }
  declare module 'next' { export interface Metadata { title?: string; } }`;
  const compilerOptions = { ...options, strict: true, noEmit: true, skipLibCheck: true,
    moduleResolution: ts.ModuleResolutionKind.Node10, lib: ['lib.es2022.d.ts', 'lib.dom.d.ts'],
    typeRoots: [path.join(tooling, '@types')], types: ['node', 'react'], baseUrl: __dirname,
    paths: { react: [path.join(tooling, '@types/react/index.d.ts')], 'react/jsx-runtime': [path.join(tooling, '@types/react/jsx-runtime.d.ts')],
      '@gitroom/frontend/components/editorial/editorial-panel': ['./editorial-panel.tsx'] } };
  const host = ts.createCompilerHost(compilerOptions), originalRead = host.readFile, originalExists = host.fileExists, originalSource = host.getSourceFile;
  const isStub = filename => path.resolve(filename) === path.resolve(stubPath);
  host.readFile = filename => isStub(filename) ? stubs : originalRead(filename);
  host.fileExists = filename => isStub(filename) || originalExists(filename);
  host.getSourceFile = (filename, languageVersion, onError, shouldCreateNewSourceFile) => isStub(filename)
    ? ts.createSourceFile(filename, stubs, languageVersion) : originalSource(filename, languageVersion, onError, shouldCreateNewSourceFile);
  const program = ts.createProgram(['editorial.controller.ts', 'editorial-form.ts', 'editorial-panel.tsx', 'editorial.page.tsx'].map(name => path.join(__dirname, name)).concat(stubPath), compilerOptions, host);
  const diagnostics = ts.getPreEmitDiagnostics(program).filter(item => item.category === ts.DiagnosticCategory.Error);
  assert.deepEqual(diagnostics.map(item => ts.flattenDiagnosticMessageText(item.messageText, '\n')), []);
});
const initial = { source: 'nasa_svs', interval_seconds: 3600, opaque_server_option: 'keep',
  nasa: { authors: [], keywords: [], min_rating: 0, max_words: 500 },
  reddit: { subreddits: [], authors: [], keywords: [], min_score: 0, min_comments: 0, max_age_hours: 72, limit: 25, sort: 'top', time_filter: 'week', max_pages: 3 } };

test('reserve controls preserve explicit bounds and reject unsafe budgets', () => {
  const settings = { ...initial, reserve: { enabled: true, target: 6, low: 3, maximum: 12, batch: 3 } };
  const form = formApi.formFromSettings(settings);
  assert.deepEqual(JSON.parse(JSON.stringify(formApi.settingsFromForm(settings, form))), settings);
  form.reserveTarget = '8'; form.reserveLow = '2'; form.reserveBatch = '2';
  assert.deepEqual(JSON.parse(JSON.stringify(formApi.settingsFromForm(settings, form).reserve)),
    { enabled: true, target: 8, low: 2, maximum: 12, batch: 2 });
  for (const [field, value] of [['reserveBatch', '4'], ['reserveMaximum', '25'], ['reserveLow', '8'], ['reserveTarget', '1.5']]) {
    const invalid = { ...form, [field]: value };
    assert.throws(() => formApi.settingsFromForm(settings, invalid), /EDITORIAL_FORM_INVALID/);
  }
});
test('structured form roundtrip preserves server-only bounds and maps Russian rating to min_score', () => {
  const form = formApi.formFromSettings(initial); form.source = 'reddit'; form.subreddits = 'science, space\nscience'; form.redditRating = '150';
  const result = formApi.settingsFromForm(initial, form);
  assert.deepEqual(JSON.parse(JSON.stringify(result.reddit.subreddits)), ['science', 'space']);
  assert.equal(result.reddit.min_score, 150); assert.equal(result.reddit.min_rating, undefined);
  assert.equal(result.opaque_server_option, 'keep'); assert.equal(result.reddit.max_pages, 3); assert.equal(result.nasa.max_words, 500);
  assert.equal(result.nasa.min_rating, 0);
});
test('open science roundtrip keeps shared filters in legacy nasa settings and preserves Reddit exactly', () => {
  const settings = { ...initial, source: 'open_science', nasa: { ...initial.nasa, max_words: 400, authors: ['ESA/Hubble'], keywords: ['galaxy'] } };
  const form = formApi.formFromSettings(settings);
  assert.equal(form.source, 'open_science'); assert.equal(form.nasaAuthors, 'ESA/Hubble'); assert.equal(form.nasaKeywords, 'galaxy');
  assert.deepEqual(JSON.parse(JSON.stringify(formApi.settingsFromForm(settings, form))), settings);
  form.nasaAuthors = 'NOIRLab, ESA/Hubble\nNOIRLab'; form.nasaKeywords = 'galaxy\nstar';
  const result = JSON.parse(JSON.stringify(formApi.settingsFromForm(settings, form)));
  assert.deepEqual(result.nasa, { authors: ['NOIRLab', 'ESA/Hubble'], keywords: ['galaxy', 'star'], min_rating: 0, max_words: 400 });
  assert.deepEqual(result.reddit, settings.reddit); assert.deepEqual(Object.keys(result).sort(), Object.keys(settings).sort());
  assert.equal(result.opaque_server_option, 'keep'); assert.equal(result.open_science, undefined);
  form.nasaRating = '1'; assert.throws(() => formApi.settingsFromForm(settings, form), /EDITORIAL_FORM_INVALID/);
});
test('legacy NASA roundtrip and transitions to open science preserve the existing settings envelope', () => {
  const form = formApi.formFromSettings(initial);
  assert.deepEqual(JSON.parse(JSON.stringify(formApi.settingsFromForm(initial, form))), initial);
  form.source = 'open_science';
  assert.deepEqual(JSON.parse(JSON.stringify(formApi.settingsFromForm(initial, form))), { ...initial, source: 'open_science' });
  form.source = 'untrusted_source'; assert.throws(() => formApi.settingsFromForm(initial, form), /EDITORIAL_FORM_INVALID/);
});
for (const [field, value] of [['interval', '59'], ['interval', '60.5'], ['redditRating', '-1'], ['redditRating', '1000001'], ['redditAge', '0'], ['redditLimit', '101'], ['redditSort', 'unknown']]) {
  test(`form rejects invalid ${field}=${value}`, () => { const form = formApi.formFromSettings(initial); form[field] = value; assert.throws(() => formApi.settingsFromForm(initial, form), /EDITORIAL_FORM_INVALID/); });
}
test('Reddit requires authors or subreddit, never invents access authorization', () => {
  const form = formApi.formFromSettings(initial); form.source = 'reddit';
  assert.throws(() => formApi.settingsFromForm(initial, form), /EDITORIAL_FORM_INVALID/);
  form.redditAuthors = 'example'; assert.equal(formApi.settingsFromForm(initial, form).source, 'reddit');
});

test('loaded source controls show shared science filters, preserve legacy NASA labels and leave Reddit controls separate', () => {
  const React = require(path.join(tooling, 'react'));
  const { renderToStaticMarkup } = require(path.join(tooling, 'react-dom/server'));
  const renderSource = source => {
    const settings = { ...initial, source }, seeds = [{ revision: 1, paused: true }, settings, 1, formApi.formFromSettings(settings), false, false, ''];
    let index = 0;
    // Подставляем сохранённые настройки, не вызывая сеть или обработчики управления.
    const seededReact = { ...React, useState() { return React.useState(seeds[index++]); } };
    const ui = loadModule('editorial-panel.tsx', { react: seededReact, 'react/jsx-runtime': require(path.join(tooling, 'react/jsx-runtime')),
      './editorial-form': formApi, './editorial.module.css': new Proxy({}, { get: (_, key) => String(key) }) });
    return { ui, markup: renderToStaticMarkup(React.createElement(ui.EditorialPanel)) };
  };
  const { ui, markup: mixed } = renderSource('open_science');
  assert.ok(mixed.includes('Открытая наука: NASA, NOIRLab, ESA/Hubble'));
  for (const text of ['>Авторы<', '>Ключевые слова<', '>Рейтинг источника<', 'Источник не предоставляет оценку']) assert.ok(mixed.includes(text));
  assert.ok(!mixed.includes('Авторы NASA')); assert.ok(!mixed.includes('Сообщества Reddit')); assert.ok(!mixed.includes('Минимальный рейтинг'));
  const nasa = renderSource('nasa_svs').markup;
  for (const text of ['Авторы NASA', 'Ключевые слова NASA', 'Рейтинг NASA']) assert.ok(nasa.includes(text));
  const reddit = renderSource('reddit').markup;
  for (const text of ['Сообщества Reddit', 'Авторы Reddit', 'Минимальный рейтинг', 'Период рейтинга', 'проверки доступа к API и прав']) assert.ok(reddit.includes(text));
  assert.ok(!reddit.includes('Рейтинг источника')); assert.ok(!reddit.includes('Авторы NASA'));
  for (const [id, label] of [['nasa_svs_articles', 'NASA SVS'], ['noirlab_iotw', 'NOIRLab'], ['esahubble_potw', 'ESA/Hubble'], ['reddit', 'Reddit']]) {
    assert.equal(ui.sourceText(id), label);
    const record = renderToStaticMarkup(React.createElement(ui.EditorialRecords, { records: [{ id, source: id }] }));
    assert.ok(record.includes(`<td>${label}</td>`));
  }
  assert.equal(ui.sourceText('future_source'), 'future_source'); assert.equal(ui.sourceText(undefined), '—');
});
test('native React records escape titles, reject unsafe URLs, loading page has Russian controls', () => {
  const React = require(path.join(tooling, 'react'));
  const { renderToStaticMarkup } = require(path.join(tooling, 'react-dom/server'));
  const ui = loadModule('editorial-panel.tsx', { react: React, 'react/jsx-runtime': require(path.join(tooling, 'react/jsx-runtime')),
    './editorial-form': formApi, './editorial.module.css': new Proxy({}, { get: (_, key) => String(key) }) });
  const markup = renderToStaticMarkup(React.createElement(ui.EditorialRecords, { records: [{ id: 'id', title: '<script>bad</script>', url: 'javascript:alert(1)' }] }));
  assert.ok(markup.includes('&lt;script&gt;')); assert.ok(!markup.includes('href='));
  const page = renderToStaticMarkup(React.createElement(ui.EditorialPanel));
  for (const text of ['Редакция', 'Запустить цикл', 'Поставить на паузу', 'Последние материалы', 'Подключения и свои материалы']) assert.ok(page.includes(text));
  assert.equal(page.includes('href="/owner-browser/vnc.html'), false);
  assert.ok(!page.includes('iframe')); assert.ok(!page.includes('private-test-key'));
  const nasa = renderToStaticMarkup(React.createElement(ui.NasaRatingField));
  assert.ok(nasa.includes('Источник не предоставляет оценку')); assert.ok(nasa.includes('disabled=""')); assert.ok(nasa.includes('value="0"'));
  for (const phase of ['selecting', 'preparing', 'scheduling', 'waiting_dispatch', 'reconciling', 'rejected', 'hour_used', 'no_source', 'selection_slot_expired', 'waiting_interval']) {
    assert.notEqual(ui.phaseText(phase), phase); assert.match(ui.phaseText(phase), /[А-Яа-яЁё]/);
  }
  for (const code of ['NASA_RATING_UNAVAILABLE', 'EDITORIAL_SOURCE_POLICY_REQUIRED', 'ARTICLE_RECONCILIATION_REQUIRED', 'EDITORIAL_LEADERSHIP_LOST', 'REDDIT_COLLECTION_CONFIG_INVALID', 'REDDIT_COLLECTION_SCOPE_REQUIRED']) {
    assert.ok(!ui.errorText(code).includes(code)); assert.match(ui.errorText(code), /[А-Яа-яЁё]/);
  }
  assert.notEqual(ui.readableTime(1761080400), '—'); assert.equal(ui.readableTime(undefined), '—');
});
test('MAX legacy noVNC path stays fixed while login now requires a selected channel', () => {
  const React = require(path.join(tooling, 'react'));
  const ui = loadModule('editorial-panel.tsx', { react: React, 'react/jsx-runtime': require(path.join(tooling, 'react/jsx-runtime')),
    './editorial-form': formApi, './editorial.module.css': new Proxy({}, { get: (_, key) => String(key) }) });
  const url = new URL(ui.OWNER_BROWSER_URL, 'http://postiz.test:4007');
  assert.equal(url.pathname, '/owner-browser/vnc.html');
  assert.equal(url.searchParams.get('autoconnect'), '1'); assert.equal(url.searchParams.get('resize'), 'scale');
  assert.equal(url.searchParams.get('path'), 'owner-browser/websockify');
  // Установленный noVNC добавляет '/' перед path, а не перед каталогом vnc.html.
  assert.equal(new URL('/' + url.searchParams.get('path'), url.origin).pathname, '/owner-browser/websockify');
  const source = fs.readFileSync(path.join(__dirname, 'editorial-panel.tsx'), 'utf8');
  assert.ok(source.includes('path=owner-browser/websockify')); assert.equal(source.includes('href={OWNER_BROWSER_URL}'), false);
  assert.ok(source.includes("action: 'prepare_login'")); assert.ok(source.includes("value?.login_url !== '/owner-browser/'"));
});

function routeUi(react, globals = {}) {
  return loadModule('editorial-panel.tsx', { react: react || require(path.join(tooling, 'react')),
    'react/jsx-runtime': require(path.join(tooling, 'react/jsx-runtime')), './editorial-form': formApi,
    './editorial.module.css': new Proxy({}, { get: (_, key) => String(key) }) }, globals);
}

test('monitor UI validates owner status, separates unknown delivery and exposes no mutation controls', () => {
  const React = require(path.join(tooling, 'react'));
  const { renderToStaticMarkup } = require(path.join(tooling, 'react-dom/server'));
  const ui = routeUi();
  const value = ui.monitorStatusResult({ available: true, read_only: true, enabled: false, ledger_available: true,
    freshness: 'stale', commands: { enabled: false, paired: true, contract_verified: false },
    observer: { heartbeat_at: 1793520000, private_cookie: 'private-never-render' },
    reports: { last: { state: 'unknown', created_at: 1793519999, text: 'private-never-render' } } });
  const markup = renderToStaticMarkup(React.createElement(ui.MonitoringStatus, { value }));
  assert.ok(markup.includes('Рассылка выключена')); assert.ok(markup.includes('Команды выключены'));
  assert.ok(markup.includes('сигнал наблюдателя устарел')); assert.ok(markup.includes('исход отправки неизвестен'));
  assert.ok(!markup.includes('private-never-render')); assert.ok(!markup.includes('type="checkbox"'));
  assert.ok(!markup.includes('Запустить') && !markup.includes('Разблокировать'));
  for (const bad of [null, { available: true, read_only: false }, { available: true, read_only: true },
    { ...value, enabled: 'true' }, { ...value, commands: { enabled: true } }]) {
    assert.throws(() => ui.monitorStatusResult(bad), /MONITOR_RESPONSE_INVALID/);
  }
});

test('monitor UI separates configured command permission from actual receiver failures and freshness', () => {
  const React = require(path.join(tooling, 'react'));
  const { renderToStaticMarkup } = require(path.join(tooling, 'react-dom/server'));
  const ui = routeUi();
  const base = { available: true, read_only: true, enabled: true, ledger_available: true,
    freshness: 'fresh', commands: { enabled: true, paired: true, contract_verified: true },
    observer: {}, reports: { last: null } };
  const view = patch => renderToStaticMarkup(React.createElement(ui.MonitoringStatus,
    { value: ui.monitorStatusResult({ ...base, commands: { ...base.commands, ...patch } }) }));
  const blocked = view({ receiver_state: 'blocked', receiver_heartbeat_at: 1793520000,
    receiver_freshness: 'fresh', error: 'private-cookie-DOM-error' });
  assert.ok(blocked.includes('Команды разрешены'));
  assert.ok(blocked.includes('Приём команд заблокирован'));
  assert.ok(!blocked.includes('Приём команд отвечает'));
  assert.ok(!blocked.includes('private-cookie-DOM-error'));
  const stale = view({ receiver_state: 'ready', receiver_heartbeat_at: 1793520000, receiver_freshness: 'stale' });
  assert.ok(stale.includes('Сигнал приёма команд устарел'));
  assert.ok(!stale.includes('Приём команд отвечает'));
  assert.ok(view({}).includes('Работа приёма команд ещё не подтверждена'));
  assert.ok(view({ receiver_state: 'ready', receiver_heartbeat_at: 1793520000,
    receiver_freshness: 'fresh' }).includes('Приём команд отвечает'));
  assert.ok(view({ receiver_state: 'ready', receiver_freshness: 'fresh' }).includes('ещё не подтверждена'));
});

test('monitor command history hides private payloads and distinguishes queued, progress and final outcomes', () => {
  const React = require(path.join(tooling, 'react'));
  const { renderToStaticMarkup } = require(path.join(tooling, 'react-dom/server'));
  const ui = routeUi();
  const base = { available: true, read_only: true, enabled: true, ledger_available: true,
    freshness: 'fresh', commands: { enabled: true, paired: true, contract_verified: true,
      history_available: true }, observer: {}, reports: { last: { state: 'unknown' } } };
  const task = { command_id: '1'.repeat(32), action: 'refill', state: 'queued', phase: 'reserve_refilled',
    ready: 3, goal: 6, created_at: 1793519900, updated_at: 1793520000,
    actor: 'private-actor', chat_id: 'private-chat', payload: 'private-payload', message: 'private-result' };
  const parsed = patch => ui.monitorStatusResult({ ...base,
    commands: { ...base.commands, recent: [{ ...task, ...patch }] } });
  const markup = value => renderToStaticMarkup(React.createElement(ui.MonitoringStatus, { value }));
  const queued = markup(parsed({}));
  assert.ok(queued.includes('Принято в очередь; выполнение ещё не подтверждено'));
  assert.ok(!queued.includes('Обработка завершена')); assert.ok(!queued.includes('private-'));
  assert.ok(!JSON.stringify(parsed({})).includes('private-'));
  const partialDone = markup(parsed({ state: 'done' }));
  assert.ok(partialDone.includes('Целевой запас не достигнут'));
  const final = markup(parsed({ state: 'done', phase: 'reserve_satisfied', ready: 6, updated_at: 1793520030 }));
  assert.ok(final.includes('Обработка завершена')); assert.ok(final.includes('Проверка готового запаса'));
  assert.ok(!final.includes('Целевой запас не достигнут')); assert.ok(!final.includes('выполнение ещё не подтверждено'));
  assert.ok(final.includes('исход отправки неизвестен'));
  assert.ok(final.includes('Состояние задачи и доставка ответа учитываются отдельно'));
  const stopping = markup(parsed({ state: 'running', phase: 'cancellation_requested' }));
  assert.ok(stopping.includes('текущая операция завершается'));
  assert.ok(stopping.includes('Выполнение начато; итог ещё неизвестен'));
  const cancelled = markup(parsed({ state: 'cancelled', phase: 'cancelled' }));
  assert.ok(cancelled.includes('Будущие проходы отменены; история сохранена'));
  assert.ok(!cancelled.includes('Целевой запас не достигнут'));
  assert.ok(markup(parsed({ action: 'help', state: 'done' })).includes('Список команд'));
  assert.ok(markup(parsed({ action: 'diagnostics', state: 'done' })).includes('Диагностика'));
  const invalid = parsed({ command_id: 'private-invalid-id', action: 'private-action', state: 'private-state',
    phase: 'private-phase', ready: true, goal: 100 });
  assert.ok(!JSON.stringify(invalid).includes('private-'));
  assert.equal(invalid.commands.recent[0].state, 'unknown');
  assert.equal(invalid.commands.recent[0].ready, undefined); assert.equal(invalid.commands.recent[0].goal, undefined);
  const bounded = ui.monitorStatusResult({ ...base, commands: { ...base.commands,
    recent: Array.from({ length: 20 }, (_, i) => ({ ...task, command_id: i.toString(16).padStart(32, '0') })) } });
  assert.equal(bounded.commands.recent.length, 10);
});

test('monitor UI loads status and preview only on explicit GET clicks; it never queues or sends', async () => {
  const React = require(path.join(tooling, 'react'));
  const { renderToStaticMarkup } = require(path.join(tooling, 'react-dom/server'));
  const values = [], calls = [];
  let index = 0;
  const hooks = { ...React, useState(initial) { const key = index++; if (!(key in values)) values[key] = initial;
    return [values[key], next => { values[key] = typeof next === 'function' ? next(values[key]) : next; }]; } };
  const ui = routeUi(hooks, { fetch: async (url, options) => { calls.push([url, options]);
    return { ok: true, json: async () => url.endsWith('monitor-preview')
      ? { available: true, read_only: true, external_mutations: 0, text: '<script>text only</script>' }
      : { available: true, read_only: true, enabled: false, ledger_available: false, freshness: 'unobserved',
        commands: { enabled: false, paired: false, contract_verified: false }, observer: {}, reports: { last: null } } };
  } });
  const render = () => { index = 0; return ui.Monitoring(); };
  const buttons = node => !node || typeof node !== 'object' ? [] : Array.isArray(node)
    ? node.flatMap(buttons) : [...(node.type === 'button' ? [node] : []), ...buttons(node.props?.children)];
  let tree = render();
  assert.equal(calls.length, 0); assert.equal(buttons(tree)[1].props.disabled, true);
  await buttons(tree)[0].props.onClick(); tree = render();
  assert.equal(calls[0][0], '/api/editorial/monitor'); assert.equal(calls[0][1].method, 'GET');
  assert.equal(buttons(tree)[1].props.disabled, false);
  await buttons(tree)[1].props.onClick(); tree = render();
  assert.equal(calls[1][0], '/api/editorial/monitor-preview');
  assert.ok(calls.every(call => call[1].method === 'GET' && call[1].body === undefined));
  const markup = renderToStaticMarkup(tree);
  assert.ok(markup.includes('&lt;script&gt;text only&lt;/script&gt;'));
  assert.ok(!markup.includes('<script>'));
});
const routeTarget = { id: 'vk_target', platform: 'vk', route: 'vk_api', destination: '-123456', enabled: true,
  paused: true, binding_hash: 'c'.repeat(64), live_verified: false, status_kind: 'configuration_only',
  credentials_configured: true, adapter_mode: 'live' };
const routeVariant = { id: 'variant-123', target_id: 'vk_target', title: 'Полный материал', body: 'Начало\n\nКонец материала',
  revision: 2, state: 'ready', content_hash: 'd'.repeat(64), media_count: 1, source_count: 2 };
const routeDetailsFixture = (variant = routeVariant, target = routeTarget) => ({
  variant_id: variant.id, target_id: variant.target_id, title: variant.title, body: variant.body,
  revision: variant.revision, content_hash: variant.content_hash, destination: target.destination,
  platform: target.platform, route: target.route, binding_hash: target.binding_hash,
  metadata: { tags: ['Точный тег'], community: '', ai_generated: true },
  media: Array.from({ length: variant.media_count }, () => ({ path: 'data/media/fixture.jpg', mime: 'image/jpeg', sha256: 'a'.repeat(64) })),
  sources: Array.from({ length: variant.source_count }, () => ({ url: 'https://example.test/source' })),
  read_only: true, manual_approval_allowed: variant.manual_approval_allowed !== false,
});

test('full details require exact version, recipient, binding and complete bounded posting metadata', () => {
  const ui = routeUi();
  assert.equal(ui.routeDetailsResult(routeDetailsFixture(), routeVariant, routeTarget).body, routeVariant.body);
  for (const patch of [{ revision: 3 }, { content_hash: '0'.repeat(64) }, { destination: 'other' },
    { binding_hash: '0'.repeat(64) }, { target_id: 'other' }, { title: 'Another title' }]) {
    assert.throws(() => ui.routeDetailsResult({ ...routeDetailsFixture(), ...patch }, routeVariant, routeTarget), /APPROVAL_STALE/);
  }
  for (const patch of [{ body: null }, { read_only: false }, { metadata: [] }, { media: [] },
    { sources: [] }, { manual_approval_allowed: 'true' }, { metadata: { huge: 'я'.repeat(450000) } }]) {
    assert.throws(() => ui.routeDetailsResult({ ...routeDetailsFixture(), ...patch }, routeVariant, routeTarget), /EDITORIAL_ROUTE_DETAILS_INVALID/);
  }
});

test('inventory body cannot open approval; exact details render complete escaped text and Pika metadata', () => {
  const React = require(path.join(tooling, 'react'));
  const { renderToStaticMarkup } = require(path.join(tooling, 'react-dom/server'));
  const ui = routeUi();
  const props = { variant: { ...routeVariant, body: 'UNTRUSTED-INVENTORY-BODY' }, target: routeTarget,
    pending: false, onAction: async () => true, onDetails: async () => undefined };
  const unloaded = renderToStaticMarkup(React.createElement(ui.RouteDraft, props));
  assert.ok(unloaded.includes('Открыть полный материал')); assert.ok(!unloaded.includes('UNTRUSTED-INVENTORY-BODY'));
  assert.ok(!unloaded.includes('type="checkbox"')); assert.ok(!unloaded.includes('Согласовать версию и дату'));
  const details = routeDetailsFixture(); details.body += '\n<script>unsafe</script>\nEND-OF-EXACT-BODY';
  details.metadata = { tags: ['Физика', 'Точный тег'], community: 'точное сообщество', ai_generated: false };
  const loaded = renderToStaticMarkup(React.createElement(ui.RouteDraft, { ...props, details }));
  for (const text of ['END-OF-EXACT-BODY', 'Физика', 'точное сообщество', 'ai_generated', 'false', 'data/media/fixture.jpg']) assert.ok(loaded.includes(text));
  assert.ok(loaded.includes('&lt;script&gt;')); assert.ok(!loaded.includes('<script>')); assert.ok(!loaded.includes('checked=""'));
  const stale = renderToStaticMarkup(React.createElement(ui.RouteDraft, { ...props, details: { ...details, revision: 1 } }));
  assert.ok(!stale.includes('type="checkbox"')); assert.ok(!stale.includes('END-OF-EXACT-BODY'));
});

test('pagination exposes totals, next and previous pages and the old unresolved filter without silent clipping', () => {
  const ui = routeUi(), calls = [];
  const pagination = { page_size: 50, variant_page: 1, variant_count: 124, variant_active_count: 6, variant_has_next: true,
    dispatch_page: 0, dispatch_count: 121, dispatch_active_count: 1, dispatch_has_next: true, dispatch_filter: 'all' };
  const tree = ui.EditorialRoutes({ inventory: { targets: [], variants: [], dispatches: [], pagination }, pending: false,
    onAction: async () => true, onPage: value => calls.push(value) });
  function walk(node) {
    if (!node || typeof node !== 'object') return [];
    if (Array.isArray(node)) return node.flatMap(walk);
    return [...(node.type === 'button' ? [node] : []), ...walk(node.props?.children)];
  }
  const buttons = walk(tree);
  buttons.find(button => button.props.children === 'Следующие материалы').props.onClick();
  buttons.find(button => button.props.children === 'Только незавершённые').props.onClick();
  assert.equal(ui.routeListPath(calls[0]), 'routes?variant_page=2&dispatch_page=0&dispatch_filter=all');
  assert.equal(ui.routeListPath(calls[1]), 'routes?variant_page=1&dispatch_page=0&dispatch_filter=unresolved');
  assert.equal(buttons.find(button => button.props.children === 'Предыдущие отправки').props.disabled, true);
  assert.equal(ui.routeListPath({ variant_page: 0, dispatch_page: 0, dispatch_filter: 'all' }), 'routes');
  assert.throws(() => ui.routeListPath({ variant_page: -1, dispatch_page: 0, dispatch_filter: 'all' }), /EDITORIAL_ROUTE_LIST_INVALID/);
});

test('real Telegram access-only inspect confirms access and permissions without claiming delivery', () => {
  const ui = routeUi();
  const access = { platform: 'telegram', fixture: false, verification: 'access_only', live_verified: false,
    publish: true, can_publish: true };
  assert.equal(ui.routeAccessText(access), 'Доступ и право публикации подтверждены. Доставка ещё не проверена.');
  assert.equal(ui.routeAccessText({ ...access, publish: false, can_publish: false }),
    'Соединение с площадкой подтверждено; право публикации не подтверждено. Доставка не проверена.');
});

test('browser access-only and legacy live access retain separate delivery wording', () => {
  const ui = routeUi();
  const expected = 'Доступ и право публикации подтверждены. Доставка ещё не проверена.';
  assert.equal(ui.routeAccessText({ fixture: false, state: 'access_only', publish: true, live_verified: false }), expected);
  assert.equal(ui.routeAccessText({ live_verified: true, publish: true }), expected);
});

test('fixture inspect has priority and incomplete or conflicting access data stays neutral', () => {
  const ui = routeUi();
  assert.equal(ui.routeAccessText({ fixture: true, live_verified: true, publish: true }),
    'Локальная проверка доступа; живая доставка не проверена.');
  const positive = { fixture: false, verification: 'access_only', live_verified: false, publish: true };
  for (const value of [undefined, null, [], 'available', {}, { publish: true, live_verified: false },
    { ...positive, can_publish: false }, { ...positive, publish: 'true' }, { ...positive, can_publish: 'true' },
    { ...positive, fixture: 'false', live_verified: true }, { ...positive, live_verified: 'false' },
    { ...positive, state: false }, { ...positive, state: [] }, { ...positive, state: 'offline' },
    { ...positive, verification: {} }, { ...positive, verification: 'fixture' },
    { live_verified: true, publish: true, verification: 'api_receipt' },
    { live_verified: true, publish: true, state: 'published' },
    { state: 'available', publish: false, live_verified: false, contract_verified: false }]) {
    assert.equal(ui.routeAccessText(value), 'Проверка доступа завершена; доставка не проверена.');
  }
});

test('actual panel inspect handler displays the real access result after its read-only refresh', async () => {
  const React = require(path.join(tooling, 'react'));
  for (const [access, expected] of [
    [{ fixture: false, verification: 'access_only', live_verified: false, can_publish: true, publish: true },
      'Доступ и право публикации подтверждены. Доставка ещё не проверена.'],
    [{ fixture: false, verification: 'access_only', live_verified: false, can_publish: false, publish: false },
      'Соединение с площадкой подтверждено; право публикации не подтверждено. Доставка не проверена.'],
    [{ fixture: true, live_verified: true, publish: true },
      'Локальная проверка доступа; живая доставка не проверена.'],
  ]) {
    const values = [], calls = [];
    let index = 0;
    const hooks = { ...React,
      useState(initial) { const key = index++; if (!(key in values)) values[key] = initial;
        return [values[key], next => { values[key] = typeof next === 'function' ? next(values[key]) : next; }]; },
      useEffect() {}, useCallback(callback) { return callback; }, useRef(value) { return { current: value }; },
    };
    const ui = routeUi(hooks, { fetch: async (url, options) => {
      calls.push([url, options]);
      const body = url === '/api/editorial/route-action' ? { access, delivery_verified: false }
        : url === '/api/editorial/state' ? { revision: 1, paused: true }
        : url === '/api/editorial/routes' ? { targets: [], variants: [], dispatches: [] }
        : assert.fail(`Unexpected panel request ${url}`);
      return { ok: true, json: async () => body };
    } });
    function elements(node, matches) {
      if (!node || typeof node !== 'object') return [];
      if (Array.isArray(node)) return node.flatMap(item => elements(item, matches));
      return [...(matches(node) ? [node] : []), ...elements(node.props?.children, matches)];
    }
    function render() { index = 0; return ui.EditorialPanel(); }
    const routes = elements(render(), node => node.type === ui.EditorialRoutes)[0];
    assert.equal(await routes.props.onAction({ action: 'inspect', id: 'telegram_test' }), true);
    const notice = elements(render(), node => node.props?.role === 'status')[0];
    assert.equal(notice.props.children, expected);
    assert.equal(calls.length, 3);
    assert.deepEqual(calls.map(([url, options]) => [url, options.method]), [
      ['/api/editorial/route-action', 'POST'], ['/api/editorial/state', 'GET'], ['/api/editorial/routes', 'GET']]);
    assert.deepEqual(JSON.parse(calls[0][1].body), { action: 'inspect', id: 'telegram_test' });
  }
});

test('actual panel loads exact details separately, never auto-preflights, and drops review when paging', async () => {
  const React = require(path.join(tooling, 'react')), values = [], refs = [], calls = [];
  let index = 0, refIndex = 0;
  const hooks = { ...React,
    useState(initial) { const key = index++; if (!(key in values)) values[key] = initial;
      return [values[key], next => { values[key] = typeof next === 'function' ? next(values[key]) : next; }]; },
    useEffect() {}, useCallback(callback) { return callback; },
    useRef(value) { const key = refIndex++; if (!(key in refs)) refs[key] = { current: value }; return refs[key]; },
  };
  const summary = { ...routeVariant }; delete summary.body;
  const ui = routeUi(hooks, { fetch: async (url, options) => {
    calls.push([url, options]);
    let body;
    if (url === '/api/editorial/route-action') {
      const action = JSON.parse(options.body);
      body = action.action === 'details' ? routeDetailsFixture() : { access: { fixture: true }, delivery_verified: false };
      assert.ok(['inspect', 'details'].includes(action.action));
    } else if (url === '/api/editorial/state') body = { revision: 1, paused: true };
    else if (url.startsWith('/api/editorial/routes')) body = { targets: [routeTarget], variants: [summary], dispatches: [] };
    else assert.fail(`Unexpected request ${url}`);
    return { ok: true, json: async () => body };
  } });
  function elements(node) {
    if (!node || typeof node !== 'object') return [];
    if (Array.isArray(node)) return node.flatMap(elements);
    return [...(node.type === ui.EditorialRoutes ? [node] : []), ...elements(node.props?.children)];
  }
  function render() { index = 0; refIndex = 0; return elements(ui.EditorialPanel())[0].props; }
  await render().onAction({ action: 'inspect', id: routeTarget.id });
  assert.deepEqual(Object.keys(render().detailsById), []);
  await render().onDetails(summary);
  assert.equal(render().detailsById[summary.id].body, routeVariant.body);
  assert.deepEqual(calls.filter(([url]) => url.endsWith('/route-action')).map(([, options]) => JSON.parse(options.body).action), ['inspect', 'details']);
  await render().onPage({ variant_page: 1, dispatch_page: 0, dispatch_filter: 'unresolved' });
  assert.deepEqual(Object.keys(render().detailsById), []);
  assert.ok(calls.some(([url]) => url === '/api/editorial/routes?variant_page=1&dispatch_page=0&dispatch_filter=unresolved'));
});

test('owner schedule is parsed as Moscow regardless of browser timezone; invalid dates fail closed', () => {
  const ui = routeUi(); const clock = Date.UTC(2026, 9, 31, 0, 0);
  assert.equal(ui.moscowSchedule('2026-10-31T06:15', clock), Date.UTC(2026, 9, 31, 3, 15) / 1000);
  for (const value of ['', '2026-10-31T03:00', '2026-02-30T12:00', '2026-13-01T12:00',
    '2026-10-31T06:15Z', '2026-10-31T06:15:00', '2026-10-31T24:00', '<script>']) {
    assert.equal(ui.moscowSchedule(value, clock), null);
  }
  assert.match(ui.readableTime(Date.UTC(2026, 9, 31, 0, 0) / 1000), /03:00/);
});

test('route cards and exact full drafts render safely without credentials, auto-consent or delivery overclaims', () => {
  const React = require(path.join(tooling, 'react'));
  const { renderToStaticMarkup } = require(path.join(tooling, 'react-dom/server'));
  const ui = routeUi();
  const targets = ['max', 'telegram', 'vk', 'pikabu'].map((platform, index) => ({ ...routeTarget,
    id: index === 2 ? 'vk_target' : `target-${index}`, platform,
    route: { max: 'max_browser', telegram: 'telegram_api', vk: 'vk_browser', pikabu: 'pikabu_browser' }[platform],
    token: 'never-render-this-token' }));
  targets.push({ ...routeTarget, id: 'disabled-reddit', platform: 'reddit', route: 'postiz', destination: 'UNMANAGED-REDDIT' });
  const lastLine = 'END-OF-COMPLETE-APPROVED-TEXT';
  const candidate = { ...routeVariant, body: `<script>alert(1)</script>\n${'Полный текст '.repeat(500)}${lastLine}` };
  const markup = renderToStaticMarkup(React.createElement(ui.EditorialRoutes, { pending: false,
    detailsById: { [candidate.id]: routeDetailsFixture(candidate, targets[2]) },
    inventory: { targets, variants: [candidate],
      dispatches: [{ id: 'dispatch-123', target_id: 'vk_target', title: 'API receipt', state: 'published',
        due_at: 1867111200, verified: false, evidence_kind: 'api_receipt', external_id: '-123456_77', attempts: 1,
        snapshot_hash: 'e'.repeat(64), permalink: 'javascript:alert(1)' },
      { id: 'dispatch-unknown', target_id: 'vk_target', title: 'Unknown send', state: 'unknown',
        due_at: 1867114800, verified: false, external_id: '-123456_78', attempts: 1, snapshot_hash: 'f'.repeat(64) }] },
    onAction: async () => true }));
  for (const label of ['MAX', 'Telegram', 'ВКонтакте', 'Пикабу', 'Проверить доступ', 'Возобновить канал',
    'API-квитанция; без независимой сверки', 'Сверить результат', 'Москва, UTC+3', lastLine]) assert.ok(markup.includes(label));
  assert.ok(markup.includes('&lt;script&gt;'));
  assert.ok(!markup.includes('<script>')); assert.ok(!markup.includes('javascript:'));
  assert.ok(!markup.includes('never-render-this-token')); assert.ok(!markup.includes('Доставка подтверждена'));
  assert.ok(!markup.includes('UNMANAGED-REDDIT'));
  assert.ok(!markup.includes('checked=""')); assert.ok(!markup.includes('Повторить отправку'));
});

test('delivery labels require persistent evidence kinds and never promote fixture or missing proof', () => {
  const ui = routeUi();
  const published = { id: 'historical', target_id: 'vk_target', title: 'History', state: 'published',
    due_at: 1867111200, verified: false, attempts: 1, snapshot_hash: 'e'.repeat(64) };
  for (const verified of [true, false, null]) {
    assert.equal(ui.routeDeliveryText({ ...published, verified, evidence_kind: 'fixture' }),
      'Локальная проверка; живая доставка не проверена');
  }
  assert.equal(ui.routeDeliveryText({ ...published, evidence_kind: 'api_receipt' }),
    'API-квитанция; без независимой сверки');
  assert.equal(ui.routeDeliveryText({ ...published, verified: true, evidence_kind: 'independent_readback' }),
    'Доставка подтверждена');
  for (const evidence_kind of [undefined, 'unverified', 'unknown', true, [], {}, 'independent_readback']) {
    assert.equal(ui.routeDeliveryText({ ...published, evidence_kind }), 'Отправка отмечена; подтверждения нет');
  }
  assert.equal(ui.routeDeliveryText({ ...published, verified: true }),
    'В истории отмечено подтверждение; новых доказательств нет');
  assert.equal(ui.routeDeliveryText({ ...published, state: 'unknown' }), 'Исход неизвестен — требуется сверка');
});

test('historical fixture badge survives a target changing to live and has no publication link', () => {
  const { renderToStaticMarkup } = require(path.join(tooling, 'react-dom/server'));
  const ui = routeUi();
  const anchors = node => !node || typeof node !== 'object' ? [] : Array.isArray(node)
    ? node.flatMap(anchors) : [...(node.type === 'a' ? [node] : []), ...anchors(node.props?.children)];
  const fixturePermalink = new URL('https://vk.ru/wall123456_51');
  const dispatch = { id: 'fixture-history', target_id: 'vk_target', title: 'Local fixture', state: 'published',
    due_at: 1867111200, verified: true, evidence_kind: 'fixture', attempts: 1,
    snapshot_hash: 'e'.repeat(64), permalink: fixturePermalink.href };
  for (const adapter_mode of ['fixture', 'browser', 'live']) {
    const tree = ui.EditorialRoutes({ pending: false,
      inventory: { targets: [{ ...routeTarget, adapter_mode, route: 'vk_browser' }], variants: [], dispatches: [dispatch] },
      onAction: async () => true });
    const markup = renderToStaticMarkup(tree);
    assert.ok(markup.includes('Локальная проверка; живая доставка не проверена'));
    assert.ok(!markup.includes('API-квитанция')); assert.ok(!markup.includes('Доставка подтверждена'));
    assert.ok(!markup.includes('Открыть публикацию'));
    assert.deepEqual(anchors(tree).map(anchor => anchor.props.href), []);
  }
  const verified = ui.EditorialRoutes({ pending: false,
    inventory: { targets: [{ ...routeTarget, adapter_mode: 'live', route: 'vk_browser' }], variants: [],
      dispatches: [{ ...dispatch, evidence_kind: 'independent_readback' }] }, onAction: async () => true });
  assert.deepEqual(anchors(verified).map(anchor => anchor.props.href), [dispatch.permalink]);
});

test('historical recipient comes only from the dispatch snapshot after target destination changes', () => {
  const React = require(path.join(tooling, 'react'));
  const { renderToStaticMarkup } = require(path.join(tooling, 'react-dom/server'));
  const ui = routeUi();
  const target = { ...routeTarget };
  const saved = { id: 'snapshot-history', target_id: target.id, title: 'Saved recipient', state: 'published',
    destination: '-111111', platform: 'vk', route: 'vk_api', evidence_kind: 'api_receipt',
    due_at: 1867111200, verified: false, attempts: 1, snapshot_hash: 'e'.repeat(64) };
  const renderRows = dispatches => {
    const markup = renderToStaticMarkup(React.createElement(ui.EditorialRoutes, { pending: false,
      inventory: { targets: [target], variants: [], dispatches }, onAction: async () => true }));
    return markup.match(/<tbody>([\s\S]*?)<\/tbody>/)[1];
  };
  const before = renderRows([saved]);
  target.destination = 'CHANGED-CURRENT-DESTINATION'; target.platform = 'telegram'; target.route = 'telegram_api';
  const after = renderRows([saved]);
  assert.equal(after, before);
  assert.ok(after.includes('ВКонтакте · -111111')); assert.ok(!after.includes('CHANGED-CURRENT-DESTINATION'));
  const legacy = renderRows([{ ...saved, destination: undefined, platform: undefined, route: undefined }]);
  assert.ok(legacy.includes('Адресат не сохранён')); assert.ok(!legacy.includes('CHANGED-CURRENT-DESTINATION'));
});

test('unknown dispatch can reconcile from its journal without DB ID, and historical variants have no approval form', () => {
  const React = require(path.join(tooling, 'react'));
  const { renderToStaticMarkup } = require(path.join(tooling, 'react-dom/server'));
  const ui = routeUi();
  const markup = renderToStaticMarkup(React.createElement(ui.EditorialRoutes, { pending: false,
    inventory: { targets: [routeTarget], variants: ['published', 'unknown', 'cancelled', 'failed'].map(state => ({
      ...routeVariant, id: `history-${state}`, title: `HISTORICAL-${state}`, state })),
      dispatches: [{ id: 'dispatch-unknown', target_id: 'vk_target', title: 'Lost DB receipt', state: 'unknown',
        due_at: 1867114800, verified: false, external_id: null, attempts: 1, snapshot_hash: 'f'.repeat(64) }] },
    onAction: async () => true }));
  const reconcile = markup.match(/<button[^>]*>Сверить результат<\/button>/);
  assert.ok(reconcile); assert.ok(!reconcile[0].includes('disabled'));
  assert.ok(markup.includes('сохранённые доказательства'));
  assert.ok(!markup.includes('HISTORICAL-')); assert.ok(!markup.includes('Согласовать версию и дату'));
});

test('hourly automatic MAX never renders a manual owner checkbox or approval action', () => {
  const React = require(path.join(tooling, 'react'));
  const { renderToStaticMarkup } = require(path.join(tooling, 'react-dom/server'));
  const ui = routeUi();
  const target = { ...routeTarget, platform: 'max', route: 'max_browser' };
  const markup = renderToStaticMarkup(React.createElement(ui.EditorialRoutes, { pending: false,
    inventory: { targets: [target], variants: [{ ...routeVariant, state: 'review_pending',
      manual_approval_allowed: false, approval_mode: 'automatic', origin: 'hourly_article_test' }], dispatches: [] },
    onAction: async () => { throw new Error('Automatic policy must not use manual route approval'); } }));
  assert.ok(markup.includes('MAX')); assert.ok(!markup.includes('type="checkbox"'));
  assert.ok(!markup.includes('Согласовать версию и дату')); assert.ok(!markup.includes('Я проверил материал'));
});

function routeDraftHarness(onAction) {
  const React = require(path.join(tooling, 'react'));
  const values = [], effects = [];
  let index = 0, effectIndex = 0, queued = [];
  const hooks = { ...React,
    useState(initial) { const key = index++; if (!(key in values)) values[key] = initial;
      return [values[key], next => { values[key] = typeof next === 'function' ? next(values[key]) : next; }]; },
    useEffect(callback, dependencies) { const key = effectIndex++;
      if (JSON.stringify(effects[key]) !== JSON.stringify(dependencies)) { effects[key] = dependencies; queued.push(callback); } },
  };
  const ui = routeUi(hooks);
  const props = { variant: { ...routeVariant, media_count: 0 }, target: { ...routeTarget, paused: false }, pending: false, onAction };
  function render() { index = 0; effectIndex = 0; queued = []; const tree = ui.RouteDraft({ ...props,
      details: props.details === undefined ? routeDetailsFixture(props.variant, props.target) : props.details });
    for (const effect of queued) effect(); return tree; }
  function elements(node, matches) {
    if (!node || typeof node !== 'object') return [];
    if (Array.isArray(node)) return node.flatMap(item => elements(item, matches));
    return [...(matches(node) ? [node] : []), ...elements(node.props?.children, matches)];
  }
  const find = (tree, type, property, value) => elements(tree, node => node.type === type && node.props?.[property] === value)[0];
  return { props, render, find, buttons: tree => elements(tree, node => node.type === 'button') };
}

test('approval requires successful preflight, owner date and actual checkbox; date/content changes reset consent', async () => {
  const actions = []; const harness = routeDraftHarness(async action => { actions.push(action); return true; });
  let tree = harness.render();
  assert.equal(harness.find(tree, 'input', 'type', 'checkbox').props.checked, false);
  await harness.buttons(tree)[1].props.onClick();
  assert.equal(actions.length, 0);
  await harness.buttons(tree)[0].props.onClick(); tree = harness.render();
  assert.equal(actions[0].action, 'preflight');
  assert.equal(harness.find(tree, 'input', 'type', 'checkbox').props.checked, false);
  const future = new Date(Date.now() + 4 * 3600000).toISOString().slice(0, 16);
  harness.find(tree, 'input', 'type', 'datetime-local').props.onChange({ target: { value: future } });
  tree = harness.render();
  assert.equal(harness.buttons(tree)[1].props.disabled, true);
  harness.find(tree, 'input', 'type', 'checkbox').props.onChange({ target: { checked: true } }); tree = harness.render();
  assert.equal(harness.buttons(tree)[1].props.disabled, false);
  await harness.buttons(tree)[1].props.onClick();
  assert.equal(actions[1].action, 'approve'); assert.equal(actions[1].id, routeVariant.id);
  assert.equal(actions[1].revision, routeVariant.revision); assert.equal(actions[1].content_hash, routeVariant.content_hash);
  assert.equal(actions[1].due_at, Date.parse(`${future}:00+03:00`) / 1000);
  tree = harness.render(); assert.equal(harness.find(tree, 'input', 'type', 'checkbox').props.checked, false);
  harness.find(tree, 'input', 'type', 'checkbox').props.onChange({ target: { checked: true } }); tree = harness.render();
  harness.find(tree, 'input', 'type', 'datetime-local').props.onChange({ target: { value: future } });
  tree = harness.render(); assert.equal(harness.find(tree, 'input', 'type', 'checkbox').props.checked, false);
  harness.props.variant = { ...harness.props.variant, revision: 3, content_hash: '9'.repeat(64) };
  harness.render(); tree = harness.render();
  assert.equal(harness.find(tree, 'input', 'type', 'checkbox').props.disabled, true);
  assert.equal(harness.buttons(tree)[1].props.disabled, true);
});

test('failed preflight never enables consent or emits approval', async () => {
  const actions = []; const harness = routeDraftHarness(async action => { actions.push(action); return false; });
  let tree = harness.render(); await harness.buttons(tree)[0].props.onClick(); tree = harness.render();
  assert.equal(harness.find(tree, 'input', 'type', 'checkbox').props.disabled, true);
  await harness.buttons(tree)[1].props.onClick();
  assert.deepEqual(actions.map(item => item.action), ['preflight']);
});

test('paused target and changed destination binding cannot reuse owner consent', async () => {
  const actions = []; const harness = routeDraftHarness(async action => { actions.push(action); return true; });
  let tree = harness.render(); await harness.buttons(tree)[0].props.onClick(); tree = harness.render();
  const future = new Date(Date.now() + 4 * 3600000).toISOString().slice(0, 16);
  harness.find(tree, 'input', 'type', 'datetime-local').props.onChange({ target: { value: future } });
  tree = harness.render(); harness.find(tree, 'input', 'type', 'checkbox').props.onChange({ target: { checked: true } });
  tree = harness.render();
  harness.props.target = { ...harness.props.target, paused: true }; tree = harness.render();
  assert.equal(harness.buttons(tree)[1].props.disabled, true);
  await harness.buttons(tree)[1].props.onClick(); assert.equal(actions.length, 1);
  harness.props.target = { ...harness.props.target, paused: false, binding_hash: '8'.repeat(64) };
  harness.render(); tree = harness.render();
  assert.equal(harness.find(tree, 'input', 'type', 'checkbox').props.checked, false);
  assert.equal(harness.find(tree, 'input', 'type', 'checkbox').props.disabled, true);
});

// Собственная JPEG fixture: 2x2 синих пикселя, без внешних изображений/запросов.
const previewJpeg = 'data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAACAAIDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDxyiiiv3E8w//Z';
const previewPayload = variant => ({ variant_id: variant.id, revision: variant.revision,
  content_hash: variant.content_hash, preflight_only: true,
  images: [{ data_url: previewJpeg, sha256: '9'.repeat(64), mime: 'image/jpeg', source_mime: 'image/png',
    width: 2, height: 2, preview: true }] });

test('preview accepts only bounded exact-version JPEG data URLs and rejects stale or malicious payloads', () => {
  const ui = routeUi(), candidate = { ...routeVariant, media_count: 1 };
  assert.equal(ui.routePreviewImages(previewPayload(candidate), candidate)[0].data_url, previewJpeg);
  for (const data_url of ['https://external.example/image.jpg', 'javascript:alert(1)', 'blob:secret',
    'data:image/svg+xml;base64,PHN2Zy8+', 'data:image/jpeg;base64,PHN2Zy8+']) {
    const value = previewPayload(candidate); value.images[0].data_url = data_url;
    assert.throws(() => ui.routePreviewImages(value, candidate), /EDITORIAL_ROUTE_PREVIEW_INVALID/);
  }
  const stale = { ...previewPayload(candidate), revision: candidate.revision + 1 };
  assert.throws(() => ui.routePreviewImages(stale, candidate), /APPROVAL_STALE/);
  const oversized = previewPayload(candidate); oversized.images[0].data_url = 'data:image/jpeg;base64,/9j/' + 'A'.repeat(1024 * 1024);
  assert.throws(() => ui.routePreviewImages(oversized, candidate), /EDITORIAL_ROUTE_PREVIEW_INVALID/);
  assert.throws(() => ui.routePreviewImages({ ...previewPayload(candidate), images: [] }, candidate), /EDITORIAL_ROUTE_PREVIEW_INVALID/);
});

test('real fixture thumbnail renders independently and never checks consent or preflight automatically', async () => {
  const { renderToStaticMarkup } = require(path.join(tooling, 'react-dom/server'));
  const actions = [], previews = [];
  const harness = routeDraftHarness(async action => { actions.push(action); return true; });
  harness.props.variant = { ...harness.props.variant, media_count: 1 };
  harness.props.onPreview = async variant => { previews.push(variant.id); return previewPayload(variant); };
  let tree = harness.render();
  await harness.buttons(tree).find(button => button.props.children === 'Показать изображение').props.onClick();
  tree = harness.render();
  assert.equal(previews.length, 1); assert.equal(actions.length, 0);
  const markup = renderToStaticMarkup(tree);
  assert.ok(markup.includes('<img src="data:image/jpeg;base64,/9j/'));
  assert.ok(markup.includes('исходный файл этой версии'));
  assert.equal(harness.find(tree, 'input', 'type', 'checkbox').props.checked, false);
  assert.equal(harness.find(tree, 'input', 'type', 'checkbox').props.disabled, true);
  const future = new Date(Date.now() + 4 * 3600000).toISOString().slice(0, 16);
  harness.find(tree, 'input', 'type', 'datetime-local').props.onChange({ target: { value: future } }); tree = harness.render();
  assert.equal(harness.find(tree, 'input', 'type', 'checkbox').props.disabled, true);
  await harness.buttons(tree).find(button => button.props.children === 'Проверить перед согласованием').props.onClick(); tree = harness.render();
  assert.equal(harness.find(tree, 'input', 'type', 'checkbox').props.disabled, false);
  assert.equal(harness.find(tree, 'input', 'type', 'checkbox').props.checked, false);
  assert.equal(harness.buttons(tree).find(button => button.props.children === 'Согласовать версию и дату').props.disabled, true);
  harness.props.variant = { ...harness.props.variant, content_hash: '7'.repeat(64) };
  harness.render(); tree = harness.render();
  assert.ok(!renderToStaticMarkup(tree).includes('<img'));
});

test('stale preview result never renders an image or enables owner approval', async () => {
  const { renderToStaticMarkup } = require(path.join(tooling, 'react-dom/server'));
  const harness = routeDraftHarness(async () => { throw new Error('No approval expected'); });
  harness.props.variant = { ...harness.props.variant, media_count: 1 };
  harness.props.onPreview = async variant => ({ ...previewPayload(variant), content_hash: '0'.repeat(64) });
  let tree = harness.render();
  await harness.buttons(tree).find(button => button.props.children === 'Показать изображение').props.onClick(); tree = harness.render();
  const markup = renderToStaticMarkup(tree);
  assert.ok(!markup.includes('<img')); assert.ok(markup.includes('согласуйте новую версию'));
  assert.equal(harness.find(tree, 'input', 'type', 'checkbox').props.disabled, true);
});

// Новое подключение и авторские материалы: только mocks MAX/Telegram/VK.
const setupTarget = { id: 'science_telegram', profile_id: 'science', platform: 'telegram', route: 'telegram_api',
  destination: '-1001234567890', enabled: true, paused: true, binding_hash: 'a'.repeat(64), options: { media_layout: 'auto' },
  capabilities: { can_configure: true, can_set_secret: true, can_create_original: true, can_upload_media: true, can_login: false },
  diagnostics: { local_prerequisites: false, credentials_configured: false, blocked_reasons: ['TELEGRAM_TOKEN_REQUIRED'] } };
const setupFixture = () => ({ config_hash: 'b'.repeat(64), targets: [{ ...setupTarget }],
  max_channels: [{ id: '-123456', name: 'Разрешённый научный MAX' }],
  profiles: [{ id: 'science', name: 'Наука', enabled: true, publication_mode: 'human' }],
  restart: { required: false, can_request: false, blocked_reasons: [] }, external_mutations: 0 });
const uploadReference = { path: `data/media/${'c'.repeat(64)}.png`, mime: 'image/png', sha256: 'c'.repeat(64) };
const createAdmin = () => ({ action: 'create_original', id: setupTarget.id, expected_binding_hash: setupTarget.binding_hash,
  original: { title: 'Своя заметка', body: 'Полный русский авторский текст без сокращений.', media: [], metadata: {} }, attest_original: true });

test('onboarding BFF returns only managed setup fields and never secret/config internals', async () => {
  const value = setupFixture(); value.token = 'private-token';
  value.max_channels[0].name = 'К'.repeat(300);
  value.targets[0].token = 'private-token'; value.targets[0].capabilities.token = 'private-token';
  value.targets[0].diagnostics.raw = { token: 'private-token' };
  const { instance, calls } = controller(() => response(value));
  const result = await instance.routeSetup(request());
  assert.equal(result.targets[0].id, setupTarget.id);
  assert.equal(result.max_channels[0].name, 'К'.repeat(300));
  assert.equal(JSON.stringify(result).includes('private-token'), false);
  assert.equal(calls[0][0], `${env.EDITORIAL_INTERNAL_URL}/route-setup`);
  const unsafe = setupFixture(); unsafe.targets[0].options.dom_contract_verified = true;
  const invalid = controller(() => response(unsafe));
  await rejected(invalid.instance.routeSetup(request()), 'EDITORIAL_ROUTE_ADMIN_INVALID');
});

test('onboarding BFF preserves owner/origin guard and strict target CAS envelope', async () => {
  const configure = { action: 'configure_target', id: 'new_max', expected_config_hash: 'b'.repeat(64), expected_binding_hash: null,
    target: { profile_id: 'science', platform: 'max', route: 'max_browser', destination: 'approved-channel', enabled: true, options: { max_chars: 4000 } } };
  const { instance, calls } = controller(() => response({ target_id: 'new_max', binding_hash: 'c'.repeat(64), config_hash: 'd'.repeat(64), paused: true, restart_required: true, token: 'do-not-return' }));
  const result = await instance.routeAdmin(request(), configure);
  assert.equal(result.paused, true); assert.equal(result.restart_required, true); assert.equal(result.token, undefined);
  assert.deepEqual(JSON.parse(calls[0][1].body), configure);
  const foreign = request(); foreign.headers.origin = 'http://evil.test';
  await rejected(instance.routeAdmin(foreign, configure), 'EDITORIAL_ORIGIN_REQUIRED');
  for (const changed of [
    { ...configure, due_at: 123 }, { ...configure, expected_binding_hash: 'old' },
    { ...configure, target: { ...configure.target, options: { dom_contract_verified: true } } },
    { ...configure, target: { ...configure.target, options: { token_env: 'PRIVATE_TOKEN' } } },
    { ...configure, target: { ...configure.target, options: { profile_dir: 'data/other-profile' } } },
    { ...configure, target: { ...configure.target, route: 'telegram_api' } },
    { ...configure, target: { ...configure.target, platform: 'other', route: 'manual' } },
  ]) await rejected(instance.routeAdmin(request(), changed), 'EDITORIAL_ROUTE_ADMIN_INVALID');
  assert.equal(calls.length, 1);
});

test('onboarding BFF token is write-only even when upstream echoes it or fails', async () => {
  const value = '123456:owner-test-private-token';
  const body = { action: 'set_secret', id: setupTarget.id, expected_binding_hash: setupTarget.binding_hash, value };
  const { instance, calls } = controller(() => response({ target_id: setupTarget.id, configured: true, paused: true, restart_required: true, value, token: value }));
  assert.deepEqual(JSON.parse(JSON.stringify(await instance.routeAdmin(request(), body))),
    { target_id: setupTarget.id, configured: true, restart_required: true, paused: true });
  assert.equal(JSON.parse(calls[0][1].body).value, value);
  for (const extra of [{ token_env: 'OTHER' }, { password: value }, { cookie: value }, { url: 'http://other' }]) {
    await rejected(instance.routeAdmin(request(), { ...body, ...extra }), 'EDITORIAL_ROUTE_ADMIN_INVALID');
  }
  const failing = controller(() => response({ code: value, value, stack: value }, 422));
  await rejected(failing.instance.routeAdmin(request(), body), 'EDITORIAL_REQUEST_FAILED', 422);
  const upper = controller(() => response({ code: 'PRIVATEUPPERCASETOKEN' }, 422));
  await rejected(upper.instance.routeAdmin(request(), { ...body, value: 'PRIVATEUPPERCASETOKEN' }), 'EDITORIAL_REQUEST_FAILED', 422);
  assert.equal(calls.length, 1);
});

test('onboarding recovery reports only the fixed pause and reconciliation code without a secret', async () => {
  const secret='123456:private-owner-token', code='ROUTE_ADMIN_RECOVERY_REQUIRED';
  const {instance}=controller(()=>response({code,value:secret,trace:secret},409));
  await rejected(instance.routeAdmin(request(),{action:'set_secret',id:setupTarget.id,
    expected_binding_hash:setupTarget.binding_hash,value:secret}),code,409);
  const ui=routeUi(), message=ui.errorText(code);
  assert.ok(message.includes('сверки')); assert.ok(message.includes('на паузе'));
  assert.equal(message.includes(secret),false); assert.equal(message.includes('успешно'),false);
});

test('onboarding BFF restart accepts only the fixed owner request with configuration CAS', async () => {
  const body={action:'request_restart',id:'server',expected_binding_hash:null,expected_config_hash:'b'.repeat(64)};
  const {instance,calls}=controller(()=>response({request_id:'c'.repeat(32),state:'restart_requested',restart_required:true,unit:'private-unit'}));
  const result=await instance.routeAdmin(request(),body);
  assert.deepEqual(JSON.parse(JSON.stringify(result)),{request_id:'c'.repeat(32),state:'restart_requested',restart_required:true});
  assert.deepEqual(JSON.parse(calls[0][1].body),body);
  for(const changed of [{...body,id:'other-service'},{...body,unit:'arbitrary.service'},{...body,expected_binding_hash:'a'.repeat(64)},
    {...body,expected_config_hash:'stale'},{...body,command:['restart']}]) {
    await rejected(instance.routeAdmin(request(),changed),'EDITORIAL_ROUTE_ADMIN_INVALID');
  }
  assert.equal(calls.length,1);
  const malformed=controller(()=>response({request_id:'c'.repeat(32),state:'running',restart_required:true}));
  await rejected(malformed.instance.routeAdmin(request(),body),'EDITORIAL_RESPONSE_INVALID');
});

test('onboarding BFF upload binds MIME SHA content address and rejects excessive base64', async () => {
  const body = { action: 'upload_media', id: setupTarget.id, expected_binding_hash: setupTarget.binding_hash,
    mime: uploadReference.mime, sha256: uploadReference.sha256, data_base64: 'aA==' };
  const { instance, calls } = controller(() => response({ media: uploadReference, external_mutations: 0, raw: 'never-return' }));
  assert.deepEqual(JSON.parse(JSON.stringify(await instance.routeAdmin(request(), body))), { media: uploadReference, external_mutations: 0 });
  for (const changed of [{ ...body, path: '../../secret' }, { ...body, mime: 'image/svg+xml' }, { ...body, data_base64: 'data:image/png;base64,aA==' },
    { ...body, data_base64: 'AAAA'.repeat(3495254) }, { ...body, sha256: 'invalid' }]) {
    await rejected(instance.routeAdmin(request(), changed), 'EDITORIAL_ROUTE_ADMIN_INVALID');
  }
  assert.equal(calls.length, 1);
  const mismatch = controller(() => response({ media: { ...uploadReference, path: 'data/media/other.png' }, external_mutations: 0 }));
  await rejected(mismatch.instance.routeAdmin(request(), body), 'EDITORIAL_RESPONSE_INVALID', 502);
});

test('onboarding BFF original create and UUID revise never carry approval or dispatch dates', async () => {
  const created = { variant_id: '12345678-1234-1234-1234-123456789012', target_id: setupTarget.id,
    revision: 1, content_hash: 'd'.repeat(64), state: 'review_pending', approval_created: false, external_mutations: 0 };
  const { instance, calls } = controller(() => response(created));
  await instance.routeAdmin(request(), createAdmin());
  for (const changed of [{ ...createAdmin(), attest_original: false }, { ...createAdmin(), due_at: 9999999999 },
    { ...createAdmin(), approval_created: true }, { ...createAdmin(), original: { ...createAdmin().original, media: [{ ...uploadReference, path: 'data/../secret.png' }] } },
    { ...createAdmin(), original: { ...createAdmin().original, metadata: { origin: 'hourly_article_test' } } }]) {
    await rejected(instance.routeAdmin(request(), changed), 'EDITORIAL_ROUTE_ADMIN_INVALID');
  }
  const revise = { ...createAdmin(), action: 'revise_original', id: created.variant_id, revision: 1, content_hash: created.content_hash };
  const revised = controller(() => response({ ...created, revision: 2 }));
  await revised.instance.routeAdmin(request(), revise);
  assert.deepEqual(JSON.parse(revised.calls[0][1].body), revise);
  const staleReply = controller(() => response(created));
  await rejected(staleReply.instance.routeAdmin(request(), revise), 'EDITORIAL_RESPONSE_INVALID');
  assert.equal(calls.length, 1);
});

test('onboarding BFF login accepts only fixed owner portal and bounded future selection', async () => {
  const body = { action: 'prepare_login', id: 'own_vk', expected_binding_hash: 'c'.repeat(64) };
  const result = { target_id: body.id, login_url: '/owner-browser/', expires_at: Math.floor(Date.now()/1000) + 599, live_verified: false };
  const allowed = controller(() => response(result));
  assert.equal((await allowed.instance.routeAdmin(request(), body)).login_url, '/owner-browser/');
  for (const patch of [{ login_url: 'https://other.test' }, { live_verified: true }, { expires_at: 1 }, { expires_at: 999999999999 }]) {
    const bad = controller(() => response({ ...result, ...patch }));
    await rejected(bad.instance.routeAdmin(request(), body), 'EDITORIAL_RESPONSE_INVALID');
  }
});

test('onboarding image SHA works on LAN HTTP without WebCrypto for padding boundaries and 10MiB', () => {
  const crypto = require('node:crypto');
  for (const length of [0, 1, 55, 56, 63, 64, 65, 1024*1024, 10*1024*1024]) {
    const bytes = Buffer.alloc(length); for (let index=0; index<length; index++) bytes[index] = (index*131+17)%256;
    assert.equal(formApi.imageSha256(bytes), crypto.createHash('sha256').update(bytes).digest('hex'), `binary length ${length}`);
  }
  const bytes = Buffer.from('Русская подпись — 🌌 наука\n', 'utf8');
  assert.equal(formApi.imageSha256(bytes), crypto.createHash('sha256').update(bytes).digest('hex'));
});

test('onboarding channel form sends only editable options and keeps platforms separate', () => {
  const form = formApi.channelForm(setupTarget), target = formApi.channelTarget(form);
  assert.equal(target.route, 'telegram_api'); assert.equal(target.options.media_layout, 'auto');
  assert.equal('token_env' in target.options, false); assert.equal('dom_contract_verified' in target.options, false);
  const vk = formApi.channelForm(); Object.assign(vk, { id: 'own_vk', profile_id: 'science', platform: 'vk', destination: '123', owner: '123' });
  assert.deepEqual(JSON.parse(JSON.stringify(formApi.channelTarget(vk).options)), { owner: '123' });
  for (const [key, value] of [['owner', ''], ['operation_timeout_seconds', '29'], ['max_chars', '40001'], ['id', '../other']]) {
    const copy = { ...vk, [key]: value }; assert.throws(() => formApi.channelTarget(copy));
  }
});

function adminComponent(name, props, globals = {}) {
  const React = require(path.join(tooling, 'react')), values = [];
  let index = 0;
  const hooks = { ...React, useState(initial) { const key=index++; if (!(key in values)) values[key]=initial;
    return [values[key], next => { values[key]=typeof next==='function' ? next(values[key]) : next; }]; }, useEffect() {} };
  const ui = routeUi(hooks, globals);
  const elements = (node, predicate) => !node || typeof node !== 'object' ? [] : Array.isArray(node)
    ? node.flatMap(item => elements(item,predicate)) : [...(predicate(node) ? [node] : []), ...elements(node.props?.children,predicate)];
  const render=()=>{ index=0; return ui[name](props); };
  const find=(tree, type, predicate=()=>true)=>elements(tree,node=>node.type===type&&predicate(node))[0];
  return { ui, props, render, elements, find };
}

test('onboarding token form clears secret on both success and failure and never renders raw errors', async () => {
  const { renderToStaticMarkup } = require(path.join(tooling,'react-dom/server'));
  for (const failing of [false,true]) {
    const calls=[], secret='123456:private-owner-token';
    const harness=adminComponent('ChannelSetup',{setup:setupFixture(),pending:false,onAdmin:async body=>{
      calls.push(body); if(failing)throw new Error(`raw request ${secret}`); return {configured:true}; }});
    let tree=harness.render(); harness.find(tree,'select').props.onChange({target:{value:setupTarget.id}}); tree=harness.render();
    harness.find(tree,'input',node=>node.props.type==='password').props.onChange({target:{value:secret}}); tree=harness.render();
    await harness.find(tree,'form',node=>node.props.autoComplete==='off').props.onSubmit({preventDefault(){}}); tree=harness.render();
    assert.equal(harness.find(tree,'input',node=>node.props.type==='password').props.value,'');
    assert.equal(calls[0].action,'set_secret'); assert.equal(calls[0].value,secret);
    assert.equal(renderToStaticMarkup(tree).includes(secret),false);
  }
});

test('onboarding original composer requires author attestation and edits exact saved version without approval', async () => {
  const calls=[], editing={...routeDetailsFixture({...routeVariant,target_id:setupTarget.id,media_count:0},setupTarget),media:[],metadata:{origin:'owner_original'}};
  const harness=adminComponent('OriginalComposer',{setup:setupFixture(),pending:false,editing,onCancel(){},onAdmin:async body=>{calls.push(body); return {};}});
  let tree=harness.render();
  assert.equal(harness.find(tree,'button',node=>node.props.type==='submit').props.disabled,true);
  harness.find(tree,'input',node=>node.props.type==='checkbox').props.onChange({target:{checked:true}}); tree=harness.render();
  harness.find(tree,'textarea').props.onChange({target:{value:'Исправленный русский текст.'}}); tree=harness.render();
  assert.equal(harness.find(tree,'input',node=>node.props.type==='checkbox').props.checked,false);
  harness.find(tree,'input',node=>node.props.type==='checkbox').props.onChange({target:{checked:true}}); tree=harness.render();
  await harness.find(tree,'form').props.onSubmit({preventDefault(){}});
  assert.equal(calls.length,1); const body=calls[0];
  assert.equal(body.action,'revise_original'); assert.equal(body.id,editing.variant_id); assert.equal(body.revision,editing.revision);
  assert.equal(body.content_hash,editing.content_hash); assert.equal(body.expected_binding_hash,setupTarget.binding_hash);
  assert.equal(body.original.body,'Исправленный русский текст.'); assert.deepEqual(Object.keys(body.original.metadata),[]);
  assert.equal('due_at' in body,false); assert.equal('approved' in body,false);
});

test('onboarding renders only three managed choices and does not offer passwords or verification flags', () => {
  const React=require(path.join(tooling,'react')), {renderToStaticMarkup}=require(path.join(tooling,'react-dom/server'));
  const ui=routeUi(), markup=renderToStaticMarkup(React.createElement(ui.ChannelSetup,{setup:setupFixture(),pending:false,onAdmin:async()=>{}}));
  for(const text of ['MAX','Telegram','ВКонтакте','Сохранить подключение на паузе','Редакционный профиль']) assert.ok(markup.includes(text));
  for(const text of ['Пикабу','dom_contract_verified','media_contract_verified','profile_dir','localStorage','Пароль VK']) assert.equal(markup.includes(text),false);
  const source=fs.readFileSync(path.join(__dirname,'editorial-panel.tsx'),'utf8');
  assert.equal(source.includes('localStorage'),false); assert.equal(source.includes('sessionStorage'),false);
});

test('onboarding selected configuration CAS is not silently rebased by refresh', async () => {
  const calls=[], harness=adminComponent('ChannelSetup',{setup:setupFixture(),pending:false,onAdmin:async body=>{calls.push(body);return {};}});
  let tree=harness.render(); harness.find(tree,'select').props.onChange({target:{value:setupTarget.id}}); tree=harness.render();
  harness.props.setup={...setupFixture(),config_hash:'f'.repeat(64),targets:[{...setupTarget,binding_hash:'e'.repeat(64)}]}; tree=harness.render();
  assert.equal(harness.find(tree,'button',node=>node.props.type==='submit').props.disabled,true);
  await harness.find(tree,'form',node=>node.props.autoComplete!=='off').props.onSubmit({preventDefault(){}});
  assert.equal(calls.length,0);
  harness.find(tree,'select').props.onChange({target:{value:setupTarget.id}}); tree=harness.render();
  assert.equal(harness.find(tree,'button',node=>node.props.type==='submit').props.disabled,false);
});

test('onboarding oversized or invalid file never calls upload or grants author attestation', async () => {
  const calls=[], harness=adminComponent('OriginalComposer',{setup:setupFixture(),pending:false,editing:null,onCancel(){},onAdmin:async body=>{calls.push(body);return {};}});
  let tree=harness.render(); harness.find(tree,'select').props.onChange({target:{value:setupTarget.id}}); tree=harness.render();
  for(const file of [{type:'image/svg+xml',size:10},{type:'image/png',size:10485761}]) {
    let read=false; const target={files:[{...file,arrayBuffer:async()=>{read=true;return new ArrayBuffer(0);}}],value:'chosen'};
    harness.find(tree,'input',node=>node.props.type==='file').props.onChange({target});
    await new Promise(resolve=>setImmediate(resolve)); tree=harness.render();
    assert.equal(read,false); assert.equal(target.value,''); assert.equal(calls.length,0);
    assert.equal(harness.find(tree,'input',node=>node.props.type==='checkbox').props.checked,false);
  }
});

test('onboarding file upload uses original frozen bytes and rejects a mismatched server reference', async () => {
  const crypto=require('node:crypto'), bytes=Buffer.from([137,80,78,71,0,1,2,3]), calls=[];
  const harness=adminComponent('OriginalComposer',{setup:setupFixture(),pending:false,editing:null,onCancel(){},onAdmin:async body=>{
    calls.push(body); return {media:uploadReference,external_mutations:0}; }},{btoa:value=>Buffer.from(value,'binary').toString('base64')});
  let tree=harness.render(); harness.find(tree,'select').props.onChange({target:{value:setupTarget.id}}); tree=harness.render();
  const target={files:[{type:'image/png',size:bytes.length,arrayBuffer:async()=>Uint8Array.from(bytes).buffer}],value:'chosen'};
  harness.find(tree,'input',node=>node.props.type==='file').props.onChange({target});
  await new Promise(resolve=>setImmediate(resolve)); tree=harness.render();
  assert.equal(calls.length,1); assert.equal(calls[0].action,'upload_media');
  assert.equal(calls[0].sha256,crypto.createHash('sha256').update(bytes).digest('hex'));
  assert.deepEqual(Buffer.from(calls[0].data_base64,'base64'),bytes);
  assert.equal(harness.elements(tree,node=>node.type==='img').length,0);
  assert.equal(harness.find(tree,'input',node=>node.props.type==='checkbox').props.checked,false);
});

test('original preview uses frozen uploaded bytes with an explicit raster Blob MIME', async () => {
  const bytes=Buffer.from([137,80,78,71,0,1,2,3]);
  const sha=hash(bytes);
  for(const mime of ['image/png','image/jpeg']) {
    const blobs=[], urls=[], uploads=[];
    class RasterBlob { constructor(parts, options) { this.parts=parts; this.type=options.type; blobs.push(this); } }
    const harness=adminComponent('OriginalComposer',{setup:setupFixture(),pending:false,editing:null,onCancel(){},onAdmin:async body=>{
      uploads.push(body); return {media:{path:`data/media/${sha}.${mime==='image/png'?'png':'jpg'}`,mime,sha256:sha},external_mutations:0};
    }},{btoa:value=>Buffer.from(value,'binary').toString('base64'),Blob:RasterBlob,
      URL:{createObjectURL:value=>{urls.push(value);return 'blob:synthetic-raster';},revokeObjectURL(){}}});
    let tree=harness.render(); harness.find(tree,'select').props.onChange({target:{value:setupTarget.id}}); tree=harness.render();
    const file={type:mime,size:bytes.length,arrayBuffer:async()=>Uint8Array.from(bytes).buffer};
    harness.find(tree,'input',node=>node.props.type==='file').props.onChange({target:{files:[file],value:'chosen'}});
    await new Promise(resolve=>setImmediate(resolve)); tree=harness.render();
    assert.equal(blobs.length,1); assert.equal(urls[0],blobs[0]); assert.notEqual(urls[0],file);
    assert.equal(blobs[0].type,mime); assert.deepEqual(Buffer.from(blobs[0].parts[0]),bytes);
    assert.deepEqual(Buffer.from(uploads[0].data_base64,'base64'),bytes);
    assert.equal(harness.find(tree,'img').props.src,'blob:synthetic-raster');
    assert.equal(harness.find(tree,'input',node=>node.props.type==='checkbox').props.checked,false);
  }
});

test('onboarding changed target binding blocks edits of an old exact version', () => {
  const editing={...routeDetailsFixture({...routeVariant,target_id:setupTarget.id,media_count:0},setupTarget),media:[],metadata:{origin:'owner_original'}};
  const setup={...setupFixture(),targets:[{...setupTarget,binding_hash:'e'.repeat(64)}]};
  const harness=adminComponent('OriginalComposer',{setup,pending:false,editing,onCancel(){},onAdmin:async()=>assert.fail('No stale save')});
  const tree=harness.render();
  assert.equal(harness.find(tree,'button',node=>node.props.type==='submit').props.disabled,true);
  assert.equal(harness.find(tree,'input',node=>node.props.type==='checkbox').props.disabled,true);
});

test('onboarding exact VK image policy is visible before consent and missing or changed policy blocks old review', async () => {
  const {renderToStaticMarkup}=require(path.join(tooling,'react-dom/server'));
  const harness=routeDraftHarness(async()=>true);
  harness.props.target={...harness.props.target,route:'vk_browser'};
  harness.props.variant={...harness.props.variant,media_count:1};
  harness.props.onPreview=async variant=>previewPayload(variant);
  harness.props.details={...routeDetailsFixture(harness.props.variant,harness.props.target),media_policy:null};
  let tree=harness.render();
  await harness.buttons(tree).find(button=>button.props.children==='Показать изображение').props.onClick(); tree=harness.render();
  await harness.buttons(tree).find(button=>button.props.children==='Проверить перед согласованием').props.onClick(); tree=harness.render();
  harness.find(tree,'input','type','datetime-local').props.onChange({target:{value:new Date(Date.now()+8*3600000).toISOString().slice(0,16)}}); tree=harness.render();
  assert.equal(harness.find(tree,'input','type','checkbox').props.disabled,true);
  harness.props.details={...harness.props.details,media_policy:{id:'native-asset-lineage-v1',label:'Исходные размеры',description:'Пережатие допускается, обрезка и изменение размеров запрещены.'}};
  harness.render(); tree=harness.render();
  assert.ok(renderToStaticMarkup(tree).includes('обрезка и изменение размеров запрещены'));
  assert.equal(harness.find(tree,'input','type','checkbox').props.checked,false);
  assert.equal(harness.find(tree,'input','type','checkbox').props.disabled,true);
  const ui=routeUi();
  assert.throws(()=>ui.routeDetailsResult({...harness.props.details,media_policy:{...harness.props.details.media_policy,id:'accepted-actual-media-v1'}},harness.props.variant,harness.props.target));
  assert.throws(()=>ui.routeDetailsResult({...harness.props.details,media_policy:{...harness.props.details.media_policy,enable_crop:true}},harness.props.variant,harness.props.target));
});

test('onboarding MAX selector lists existing allowed names and rejects an injected unlisted ID', async () => {
  const calls=[], harness=adminComponent('ChannelSetup',{setup:setupFixture(),pending:false,onAdmin:async body=>{calls.push(body);return {};}});
  let tree=harness.render();
  assert.ok(harness.elements(tree,node=>node.type==='option'&&node.props.value==='-123456')[0].props.children.includes('Разрешённый научный MAX'));
  const inputs=harness.elements(tree,node=>node.type==='input'&&node.props.required);
  inputs[0].props.onChange({target:{value:'science_max'}});
  const selects=harness.elements(tree,node=>node.type==='select');
  selects.find(node=>node.props.required&&node.props.children.flat().some(child=>child?.props?.value==='science')).props.onChange({target:{value:'science'}});
  selects.find(node=>node.props.required&&node.props.children.flat().some(child=>child?.props?.value==='-123456')).props.onChange({target:{value:'-999999'}});
  tree=harness.render(); await harness.find(tree,'form').props.onSubmit({preventDefault(){}});
  assert.equal(calls.length,0);
});

test('onboarding editing an original clears the old checked review before opening the editor', async () => {
  const edits=[], harness=routeDraftHarness(async()=>true);
  harness.props.variant={...harness.props.variant,origin:'owner_original'};
  harness.props.onEdit=details=>edits.push(details);
  let tree=harness.render();
  await harness.buttons(tree).find(button=>button.props.children==='Проверить перед согласованием').props.onClick(); tree=harness.render();
  harness.find(tree,'input','type','datetime-local').props.onChange({target:{value:new Date(Date.now()+8*3600000).toISOString().slice(0,16)}}); tree=harness.render();
  harness.find(tree,'input','type','checkbox').props.onChange({target:{checked:true}}); tree=harness.render();
  assert.equal(harness.find(tree,'input','type','checkbox').props.checked,true);
  harness.buttons(tree).find(button=>button.props.children==='Редактировать авторский материал').props.onClick(); tree=harness.render();
  assert.equal(edits.length,1); assert.equal(edits[0].revision,harness.props.variant.revision);
  assert.equal(harness.find(tree,'input','type','checkbox').props.checked,false);
  assert.equal(harness.find(tree,'input','type','checkbox').props.disabled,true);
});
function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'postiz-editorial-'));
  const api = `import { Module } from '@nestjs/common';\nimport { ExistingController } from '@gitroom/backend/api/routes/existing.controller';\nconst authenticatedController = [ExistingController];\nexport class ApiModule { configure(consumer: any) { consumer.apply('AuthMiddleware').forRoutes(...authenticatedController); } }`;
  const menu = 'export const useMenuItem = () => {\n  const firstMenu = [\n  ]; return firstMenu; };';
  const files = { 'apps/backend/src/api/api.module.ts': api, 'apps/frontend/src/components/layout/top.menu.tsx': menu,
    'apps/backend/dist/apps/backend/src/api/api.module.js': 'pinned compiled API', 'unrelated-guard.js': 'CAS sentinel unchanged' };
  for (const [relative, content] of Object.entries(files)) { fs.mkdirSync(path.dirname(path.join(root, relative)), { recursive: true }); fs.writeFileSync(path.join(root, relative), content); }
  const manifest = { typescript_version: ts.version, sources: Object.fromEntries(Object.entries(files).filter(([name]) => name.endsWith('.ts') || name.endsWith('.tsx')).map(([name, content]) => [name, hash(content)])),
    compiled: { 'apps/backend/dist/apps/backend/src/api/api.module.js': hash('pinned compiled API') } };
  return { root, manifest };
}
test('strict overlay registers controller inside authenticated routes and compiles only two backend modules', () => {
  const { root, manifest } = fixture();
  try {
    const plan = planPatch(root, __dirname, manifest); const all = compilePlans(root, plan, ts);
    const api = all.find(([name]) => name.endsWith('api.module.ts'))[1];
    assert.ok(api.includes('const authenticatedController = [\n  EditorialController,')); assert.ok(api.includes('forRoutes(...authenticatedController)'));
    const compiled = all.filter(([name]) => name.endsWith('.js')); assert.equal(compiled.length, 2);
    assert.ok(compiled[0][1].includes('require("./routes/editorial.controller")'));
    assert.ok(compiled[0][1].includes('require("./routes/existing.controller")')); assert.ok(!compiled[0][1].includes('@gitroom/'));
    assert.equal(fs.readFileSync(path.join(root, 'unrelated-guard.js'), 'utf8'), 'CAS sentinel unchanged');
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});
test('pinned mismatch and preexisting route fail before any planned source writes', () => {
  const { root, manifest } = fixture();
  try {
    fs.writeFileSync(path.join(root, 'apps/backend/src/api/api.module.ts'), 'unexpected source');
    assert.throws(() => planPatch(root, __dirname, manifest), /EDITORIAL_PINNED_BASE_MISMATCH/);
    assert.equal(fs.existsSync(path.join(root, 'apps/backend/src/api/routes/editorial.controller.ts')), false);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});
test('preexisting native route is never overwritten', () => {
  const { root, manifest } = fixture();
  try {
    const existing = path.join(root, 'apps/frontend/src/app/(app)/(site)/editorial/page.tsx');
    fs.mkdirSync(path.dirname(existing), { recursive: true }); fs.writeFileSync(existing, 'existing owner route');
    assert.throws(() => planPatch(root, __dirname, manifest), /EDITORIAL_TARGET_ALREADY_EXISTS/);
    assert.equal(fs.readFileSync(existing, 'utf8'), 'existing owner route');
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});
test('image preflight refuses wrong base and never starts or replaces containers', () => {
  const { main, buildArguments } = require('./build-editorial-image.cjs');
  const args = ['--origin', 'http://postiz.test:4007', '--tag', 'max-postiz-linux:editorial-test'];
  const calls = [], manifest = JSON.parse(fs.readFileSync(path.join(__dirname, 'editorial-manifest.json')));
  assert.throws(() => main(args, () => ({ status: 0, stdout: 'wrong-image' })), /EDITORIAL_BUILD_BASE_IMAGE_MISMATCH/);
  main(args, (binary, parameters) => { calls.push([binary, parameters]); return { status: 0, stdout: manifest.base_image_id }; });
  assert.equal(calls.length, 1); assert.deepEqual(calls[0][1].slice(0, 2), ['image', 'inspect']);
  main([...args, '--build'], (binary, parameters) => { calls.push([binary, parameters]); return { status: 0, stdout: manifest.base_image_id }; });
  assert.equal(calls.length, 3); assert.equal(calls[2][1][0], 'build');
  assert.ok(!calls.flat(2).some(value => ['run', 'up', 'restart', 'stop'].includes(value)));
  assert.throws(() => buildArguments(['--origin', 'http://postiz.test:4007', '--tag', manifest.base_image]), /EDITORIAL_BUILD_BASE_OVERWRITE_FORBIDDEN/);
});
