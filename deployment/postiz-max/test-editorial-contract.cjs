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
  for (const text of ['Редакция', 'Запустить цикл', 'Поставить на паузу', 'Последние материалы', 'Вход в MAX', '/owner-browser/vnc.html?autoconnect=1&amp;resize=scale']) assert.ok(page.includes(text));
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
test('MAX login link supplies the exact root-relative noVNC WebSocket proxy path', () => {
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
  assert.ok(source.includes('path=owner-browser/websockify')); assert.ok(source.includes('href={OWNER_BROWSER_URL}'));
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
  const React = require(path.join(tooling, 'react'));
  const { renderToStaticMarkup } = require(path.join(tooling, 'react-dom/server'));
  const ui = routeUi();
  for (const adapter_mode of ['fixture', 'browser', 'live']) {
    const markup = renderToStaticMarkup(React.createElement(ui.EditorialRoutes, { pending: false,
      inventory: { targets: [{ ...routeTarget, adapter_mode, route: 'vk_browser' }], variants: [], dispatches: [{
        id: 'fixture-history', target_id: 'vk_target', title: 'Local fixture', state: 'published',
        due_at: 1867111200, verified: true, evidence_kind: 'fixture', attempts: 1,
        snapshot_hash: 'e'.repeat(64), permalink: 'https://vk.ru/wall123456_51' }] }, onAction: async () => true }));
    assert.ok(markup.includes('Локальная проверка; живая доставка не проверена'));
    assert.ok(!markup.includes('API-квитанция')); assert.ok(!markup.includes('Доставка подтверждена'));
    assert.ok(!markup.includes('Открыть публикацию')); assert.ok(!markup.includes('https://vk.ru/wall123456_51'));
  }
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
