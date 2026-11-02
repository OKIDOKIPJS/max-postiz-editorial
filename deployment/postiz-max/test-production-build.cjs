const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const { spawnSync } = require('node:child_process');
const { argumentsFor, structuralCheck, main } = require('./build-production-image.cjs');
const frontend = require('./patch-production-frontend.cjs');
const backend = require('./patch-production-backend.cjs');
const originSecurity = require('./production-origin.cjs');

const args = ['--build', '--origin', 'http://127.0.0.1:14007', '--tag', 'max-postiz-linux:0.12.1-production-test'];
function docker(options = {}) {
  const calls = [];
  const run = (command, parameters) => {
    assert.ok(['docker', 'docker.exe'].includes(command)); calls.push(parameters);
    const values = parameters[0] === '--context' ? parameters.slice(2) : parameters;
    if (values[0] === 'context' && values[1] === 'show') return { status: 0, stdout: 'default\n' };
    if (values[0] === 'context') return { status: 0, stdout: JSON.stringify(options.remote ? 'tcp://other:2375' : 'unix:///var/run/docker.sock') };
    if (values[0] === 'version') return { status: options.unavailable ? 1 : 0, stdout: 'fixture-version' };
    if (values[0] === 'image' && values[1] === 'inspect') return { status: options.exists ? 0 : 1, stdout: options.exists ? 'sha256:existing' : '' };
    if (values[0] === 'image') return { status: options.listUnknown ? 1 : 0, stdout: options.listExists ? 'sha256:existing' : '' };
    if (values[0] === 'build') return { status: options.buildFails ? 1 : 0 };
    assert.fail(`Unexpected Docker action ${values[0]}`);
  };
  return { run, calls };
}

test('fresh recipe checks exact pinned source sets and rebuilds all native targets after both overlays', () => {
  const checked = structuralCheck(__dirname);
  assert.equal(checked.upstream_source_pin_count, 11); assert.equal(checked.editorial_source_pin_count, 2);
  assert.equal(checked.frontend_config_source_pin_count, 1);
  assert.equal(checked.backend_bootstrap_source_pin_count, 1);
  assert.equal(checked.compiled_artifact_reused, false); assert.equal(checked.full_native_rebuild_required, true);
  const upstream = JSON.parse(fs.readFileSync(path.join(__dirname, 'source-manifest.json')));
  const editorial = JSON.parse(fs.readFileSync(path.join(__dirname, 'editorial-manifest.json')));
  assert.deepEqual(editorial.sources, {
    'apps/backend/src/api/api.module.ts': '7c05e351318c1b3281d1397456e8be88fc6d443e2678ac3064b9488b3788a3e5',
    'apps/frontend/src/components/layout/top.menu.tsx': '6767be5ddaac467a67f341ff9575e69114b0d8a0fa5cb0f252a7946a7f077599',
  });
  assert.ok(Object.keys(editorial.sources).every(name => !(name in upstream.sha256)));
  const recipe = fs.readFileSync(path.join(__dirname, 'Dockerfile.production'), 'utf8');
  assert.ok(recipe.includes("planPatch('/app','/adapter',{...m,compiled:{}})"));
  assert.ok(recipe.includes('max-browser.provider.ts max-browser.guard.ts'));
  assert.ok(!recipe.includes('FROM max-postiz-')); assert.ok(!recipe.includes('COPY . '));
  assert.ok(!/^RUN .*prisma (?:db push|migrate)/m.test(recipe));
  assert.ok(recipe.indexOf('pnpm --filter ./apps/orchestrator build') < recipe.indexOf('node /adapter/patch-production-frontend.cjs'));
  assert.ok(recipe.indexOf('node /adapter/patch-production-frontend.cjs') < recipe.indexOf('pnpm --filter ./apps/frontend exec next build --webpack'));
  assert.ok(recipe.indexOf('pnpm --filter ./apps/frontend exec next build --webpack') < recipe.indexOf('node /adapter/production-startup.cjs --check /app'));
  assert.ok(recipe.endsWith('ENTRYPOINT []\nCMD ["/bin/sh", "/adapter/start-production.sh"]\n'));
});

test('only the exact upstream backend gains a bounded editorial admin parser before full Nest builds', () => {
  const raw = fs.readFileSync(path.join(__dirname, 'fixtures/production-backend-main.upstream.ts'));
  assert.equal(crypto.createHash('sha256').update(raw).digest('hex'), backend.UPSTREAM_BACKEND_MAIN_SHA256);
  const planned = backend.planBackendMain(raw);
  assert.equal(planned.replace(backend.ADDITION, ''), raw.toString('utf8'));
  assert.equal(planned.split("req.method === 'POST' && req.path === '/editorial/route-admin'").length, 2);
  assert.equal(planned.split("return json({ limit: '15mb' })(req, res, next);").length, 2);
  assert.ok(!planned.includes('bodyParser: false'));
  const recipe = fs.readFileSync(path.join(__dirname, 'Dockerfile.production'), 'utf8');
  assert.ok(recipe.indexOf('node /adapter/patch-production-backend.cjs') < recipe.indexOf('pnpm --filter ./apps/backend build'));
  assert.throws(() => backend.planBackendMain(Buffer.from(planned)), /PRODUCTION_BACKEND_SOURCE_MISMATCH/);
  assert.throws(() => backend.planBackendMain(Buffer.concat([raw, Buffer.from('\n')])), /PRODUCTION_BACKEND_SOURCE_MISMATCH/);
});

test('backend parser patch changes one pinned bootstrap file and refuses reentry', () => {
  const raw = fs.readFileSync(path.join(__dirname, 'fixtures/production-backend-main.upstream.ts'));
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), 'postiz-backend-native-'));
  try {
    const target = path.join(folder, backend.UPSTREAM_BACKEND_MAIN);
    fs.mkdirSync(path.dirname(target), { recursive: true }); fs.writeFileSync(target, raw);
    assert.deepEqual(backend.main(folder), { route: '/editorial/route-admin', json_limit: '15mb',
      ordinary_route_limits_changed: false, owner_authorization_required: true });
    assert.equal(fs.readFileSync(target, 'utf8'), backend.planBackendMain(raw));
    assert.throws(() => backend.main(folder), /PRODUCTION_BACKEND_SOURCE_MISMATCH/);
  } finally { fs.rmSync(folder, { recursive: true, force: true }); }
});

test('native server sanitizer is externalized with one additive line against the exact pinned official Next config', () => {
  // Official Postiz source at 91c91f633a0175fb3914dba64c932928c514b72a.
  const raw = fs.readFileSync(path.join(__dirname, 'fixtures/production-next.config.upstream.cjs'));
  assert.equal(crypto.createHash('sha256').update(raw).digest('hex'), frontend.UPSTREAM_NEXT_CONFIG_SHA256);
  const planned = frontend.planFrontendConfig(raw);
  const addition = "  serverExternalPackages: ['isomorphic-dompurify'],\n";
  assert.equal(planned.split(addition).length, 2);
  assert.equal(planned.replace(addition, ''), raw.toString('utf8'));
  assert.ok(planned.includes('productionBrowserSourceMaps: true'));
  assert.ok(planned.includes('export default withSentryConfig(nextConfig'));
  assert.ok(!/ignoreBuildErrors|ignoreDuringBuilds|swcMinify|typescript\s*:/.test(planned));
});

test('changed, already patched, non-buffer or truncated Next source is refused without writing', () => {
  const raw = fs.readFileSync(path.join(__dirname, 'fixtures/production-next.config.upstream.cjs'));
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), 'postiz-next-pin-'));
  try {
    const target = path.join(folder, frontend.UPSTREAM_NEXT_CONFIG);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    for (const bad of [Buffer.concat([raw, Buffer.from('\n')]), Buffer.from(frontend.planFrontendConfig(raw)), raw.subarray(0, 32)]) {
      fs.writeFileSync(target, bad);
      assert.throws(() => frontend.main(folder), /PRODUCTION_FRONTEND_SOURCE_MISMATCH/);
      assert.deepEqual(fs.readFileSync(target), bad);
    }
    assert.throws(() => frontend.planFrontendConfig(raw.toString('utf8')), /PRODUCTION_FRONTEND_SOURCE_MISMATCH/);
  } finally { fs.rmSync(folder, { recursive: true, force: true }); }
});

test('frontend config patch writes only the intended pinned file and refuses reentry', () => {
  const raw = fs.readFileSync(path.join(__dirname, 'fixtures/production-next.config.upstream.cjs'));
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), 'postiz-next-native-'));
  try {
    const target = path.join(folder, frontend.UPSTREAM_NEXT_CONFIG);
    fs.mkdirSync(path.dirname(target), { recursive: true }); fs.writeFileSync(target, raw);
    const result = frontend.main(folder);
    assert.deepEqual(result.server_external_packages, ['isomorphic-dompurify']);
    assert.equal(result.sanitizer_changed, false);
    assert.equal(fs.readFileSync(target, 'utf8'), frontend.planFrontendConfig(raw));
    assert.deepEqual(fs.readdirSync(path.dirname(target)), ['next.config.js']);
    assert.throws(() => frontend.main(folder), /PRODUCTION_FRONTEND_SOURCE_MISMATCH/);
  } finally { fs.rmSync(folder, { recursive: true, force: true }); }
});

test('secure mode is empty for HTTPS and true only for canonical literal loopback HTTP', () => {
  for (const value of ['https://postiz.example', 'https://postiz.example:8443', 'https://127.0.0.1:14007']) {
    assert.equal(originSecurity.securityForOrigin(value).not_secured, '');
    assert.equal(originSecurity.validateBuildSecurity(value, '').origin, value);
  }
  for (const value of ['http://127.0.0.1:14007', 'http://[::1]:14007']) {
    assert.equal(originSecurity.securityForOrigin(value).not_secured, 'true');
    assert.equal(originSecurity.validateBuildSecurity(value, 'true').origin, value);
  }
});

test('public HTTP, ambiguous origins and truthy false or omitted cookie modes fail closed', () => {
  for (const value of ['http://postiz.example', 'http://localhost:14007', 'http://127.1:14007',
    'http://2130706433:14007', 'http://127.0.0.1.example', 'http://[::ffff:127.0.0.1]',
    'https://user:fixture-secret@postiz.example', 'https://postiz.example/path', 'https://postiz.example/',
    'https://postiz.example?x=1', 'https://postiz.example#x', ' https://postiz.example', null, {}]) {
    assert.throws(() => originSecurity.securityForOrigin(value), /PRODUCTION_BUILD_ORIGIN_INVALID/);
  }
  for (const selected of [undefined, '__UNSET__', 'true', 'false', '0', false, null]) {
    assert.throws(() => originSecurity.validateBuildSecurity('https://postiz.example', selected), /PRODUCTION_COOKIE_MODE_INVALID/);
  }
  assert.throws(() => originSecurity.validateBuildSecurity('http://127.0.0.1:14007', ''), /PRODUCTION_COOKIE_MODE_INVALID/);
});

test('legacy private LAN mode requires an explicit profile and exactly the owner retained origin', () => {
  const { LEGACY_ORIGIN_PROFILE: profile, LEGACY_PRIVATE_ORIGIN: origin } = originSecurity;
  assert.equal(origin, 'http://10.20.30.45:4007');
  assert.throws(() => originSecurity.securityForOrigin(origin), /PRODUCTION_BUILD_ORIGIN_INVALID/);
  assert.deepEqual(originSecurity.validateBuildSecurity(origin, 'true', profile), {
    origin, not_secured: 'true', origin_profile: profile,
  });
  for (const value of [origin + '/', origin + '/editorial', origin + '?a=1', origin + '#x',
    'http://10.20.30.45:4008', 'http://10.20.30.46:4007', 'https://10.20.30.45:4007',
    'http://127.0.0.1:4007', 'http://postiz.example:4007']) {
    assert.throws(() => originSecurity.securityForOrigin(value, profile), /PRODUCTION_BUILD_ORIGIN_INVALID/);
  }
  for (const selected of ['', 'false', undefined, '__UNSET__']) {
    assert.throws(() => originSecurity.validateBuildSecurity(origin, selected, profile), /PRODUCTION_COOKIE_MODE_INVALID/);
  }
  for (const profile of ['', 'legacy-private-lan-http', true, null, {}]) {
    assert.throws(() => originSecurity.securityForOrigin(origin, profile), /PRODUCTION_ORIGIN_PROFILE_INVALID/);
  }
});

test('direct Docker origin validator refuses implicit legacy HTTP and accepts only its exact explicit profile', () => {
  const helper = path.join(__dirname, 'production-origin.cjs');
  const origin = originSecurity.LEGACY_PRIVATE_ORIGIN, profile = originSecurity.LEGACY_ORIGIN_PROFILE;
  for (const [values, passed] of [
    [[origin, 'true'], false], [[origin, 'true', profile], true], [[origin, '', profile], false],
    [[origin.replace('4007', '4008'), 'true', profile], false],
    [[origin, 'true', 'unknown-profile'], false], [[origin, 'true', profile, 'extra'], false],
  ]) {
    const result = spawnSync(process.execPath, [helper, ...values], { encoding: 'utf8' });
    assert.equal(result.status === 0, passed);
    if (passed) assert.equal(JSON.parse(result.stdout).origin_profile, profile);
    else assert.match(result.stderr, /^PRODUCTION_[A-Z_]+\r?\n$/);
  }
});

test('legacy runner profile is explicit, bound to the build and rejects environment contradictions before Docker', () => {
  const profile = originSecurity.LEGACY_ORIGIN_PROFILE;
  const selected = args.map(value => value === 'http://127.0.0.1:14007' ? originSecurity.LEGACY_PRIVATE_ORIGIN : value);
  assert.throws(() => argumentsFor(selected), /PRODUCTION_BUILD_ORIGIN_INVALID/);
  const explicit = [...selected, '--origin-profile', profile];
  const { run, calls } = docker();
  const result = main(explicit, run, __dirname, Object.freeze({ PRODUCTION_ORIGIN_PROFILE: profile }));
  const built = calls.find(call => call.includes('build'));
  assert.ok(built.includes('PRODUCTION_ORIGIN_PROFILE=' + profile));
  assert.ok(built.includes('PRODUCTION_NOT_SECURED=true'));
  assert.equal(result.origin_profile, profile);
  for (const environment of [{ PRODUCTION_ORIGIN_PROFILE: 'https-or-loopback-v1' }, { production_origin_profile: 'unknown' }]) {
    assert.throws(() => main(explicit, () => assert.fail('No Docker for contradictory profile'), __dirname, environment), /PRODUCTION_ORIGIN_PROFILE_INVALID/);
  }
  assert.throws(() => argumentsFor(['--check', '--origin-profile', profile]), /PRODUCTION_BUILD_ARGUMENTS_INVALID/);
});

test('direct Docker validator accepts an explicit empty HTTPS argument and refuses omitted or contradictory modes', () => {
  const helper = path.join(__dirname, 'production-origin.cjs');
  for (const [values, passed] of [
    [['https://postiz.example', ''], true], [['http://127.0.0.1:14007', 'true'], true],
    [['https://postiz.example'], false], [['https://postiz.example', '__UNSET__'], false],
    [['https://postiz.example', 'true'], false], [['https://postiz.example', 'false'], false],
    [['http://postiz.example', 'true'], false], [['https://user:fixture-secret@postiz.example', ''], false],
  ]) {
    const result = spawnSync(process.execPath, [helper, ...values], { encoding: 'utf8' });
    assert.equal(result.status === 0, passed);
    assert.ok(!result.stdout.includes('fixture-secret') && !result.stderr.includes('fixture-secret'));
    if (passed) assert.equal(JSON.parse(result.stdout).not_secured, values[1]);
    else assert.match(result.stderr, /^PRODUCTION_[A-Z_]+\r?\n$/);
  }
});

test('runner passes the explicit derived HTTPS empty build argument and rejects conflicting caller environment before Docker', () => {
  const secureArgs = args.map(value => value === 'http://127.0.0.1:14007' ? 'https://postiz.example' : value);
  const { run, calls } = docker();
  main(secureArgs, run, __dirname, Object.freeze({ NOT_SECURED: '', PRODUCTION_NOT_SECURED: '' }));
  const built = calls.find(call => call.includes('build'));
  assert.ok(built.includes('PRODUCTION_NOT_SECURED='));
  for (const environment of [{ NOT_SECURED: 'true' }, { NOT_SECURED: 'false' }, { PRODUCTION_NOT_SECURED: 'true' }]) {
    assert.throws(() => main(secureArgs, () => assert.fail('No Docker for contradictory security'), __dirname, environment), /PRODUCTION_COOKIE_MODE_INVALID/);
  }
  assert.throws(() => main(args, () => assert.fail('No Docker for contradictory security'), __dirname, { NOT_SECURED: '' }), /PRODUCTION_COOKIE_MODE_INVALID/);
  const publicHttp = args.map(value => value === 'http://127.0.0.1:14007' ? 'http://postiz.example' : value);
  assert.throws(() => main(publicHttp, () => assert.fail('No Docker for public HTTP'), __dirname, {}), /PRODUCTION_BUILD_ORIGIN_INVALID/);
});

test('security validator and final ENV follow full Next so the native build cache remains reusable', () => {
  const recipe = fs.readFileSync(path.join(__dirname, 'Dockerfile.production'), 'utf8');
  const fullNext = recipe.indexOf('RUN pnpm --filter ./apps/frontend exec next build --webpack');
  const guard = recipe.indexOf('RUN node /adapter/production-origin.cjs "$PUBLIC_ORIGIN" "$PRODUCTION_NOT_SECURED" "$PRODUCTION_ORIGIN_PROFILE"');
  const finalEnv = recipe.indexOf('ENV NOT_SECURED=$PRODUCTION_NOT_SECURED');
  assert.ok(fullNext < guard && guard < finalEnv);
  assert.ok(recipe.includes('ARG PRODUCTION_NOT_SECURED=__UNSET__'));
  assert.ok(recipe.includes('ARG PRODUCTION_ORIGIN_PROFILE=https-or-loopback-v1'));
  assert.ok(!recipe.slice(finalEnv).includes('NOT_SECURED=true'));
  assert.equal(recipe.slice(fullNext, guard).match(/\bRUN\b/g).length, 1);
});

test('check is strictly local and never claims a built image or accesses Docker', () => {
  const result = main(['--check'], () => assert.fail('No Docker in structural check'));
  assert.equal(result.image_built, false); assert.equal(result.build_status, 'NOT_BUILT');
  assert.equal(result.runtime_verified, false); assert.equal(result.upstream_source_checked, false);
});

test('fresh build invokes only local-context probes and one Docker build, no service or volume operations', () => {
  const { run, calls } = docker(); const result = main(args, run, __dirname, {});
  assert.equal(result.image_built, true); assert.equal(result.runtime_verified, false);
  const builds = calls.filter(call => call.includes('build')); assert.equal(builds.length, 1);
  assert.equal(builds[0][builds[0].indexOf('--builder') + 1], 'default');
  assert.equal(builds[0][builds[0].indexOf('--network') + 1], 'none');
  assert.ok(builds[0].includes(path.join(__dirname, 'Dockerfile.production')));
  assert.ok(builds[0].includes('PUBLIC_ORIGIN=http://127.0.0.1:14007'));
  assert.ok(builds[0].includes('PRODUCTION_NOT_SECURED=true'));
  assert.ok(calls.every(call => !call.some(value => ['run', 'compose', 'start', 'stop', 'volume', 'push', 'tag'].includes(value))));
});

test('existing or concurrently discovered tag is refused before any build', () => {
  for (const options of [{ exists: true }, { listExists: true }]) {
    const { run, calls } = docker(options);
    assert.throws(() => main(args, run, __dirname, {}), /PRODUCTION_TAG_ALREADY_EXISTS/);
    assert.ok(!calls.some(call => call.includes('build')));
  }
});

test('remote, missing daemon and unknown absence are fail-closed without building', () => {
  for (const [options, code] of [[{ remote: true }, 'PRODUCTION_DOCKER_LOCAL_REQUIRED'],
    [{ unavailable: true }, 'PRODUCTION_DOCKER_UNAVAILABLE'], [{ listUnknown: true }, 'PRODUCTION_DOCKER_UNAVAILABLE']]) {
    const { run, calls } = docker(options);
    assert.throws(() => main(args, run, __dirname, {}), new RegExp(code));
    assert.ok(!calls.some(call => call.includes('build')));
  }
});

test('bad origins, duplicate flags, historical tags and arbitrary arguments fail before Docker', () => {
  for (const values of [[], ['--check', '--build'], ['--check', '--check'], ['--check', '--unknown'],
    ['--build'], ['--check', '--origin', 'http://user:secret@postiz.test'],
    ['--check', '--origin', 'http://postiz.test/path'], ['--check', '--tag', 'max-postiz-linux:0.5.1']]) {
    assert.throws(() => main(values, () => assert.fail('No Docker for bad input')), /PRODUCTION_BUILD_/);
  }
  assert.equal(argumentsFor(['--check'])['--check'], true);
});

test('build failure remains failure with no image or runtime success receipt', () => {
  const { run } = docker({ buildFails: true });
  assert.throws(() => main(args, run, __dirname, {}), /PRODUCTION_BUILD_FAILED/);
});

test('Docker and Buildx environment overrides fail before any probe without changing their source', () => {
  for (const name of ['DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS', 'DOCKER_TLS_VERIFY',
    'DOCKER_CERT_PATH', 'BUILDX_BUILDER', 'BUILDKIT_HOST', 'docker_host']) {
    const environment = Object.freeze({ [name]: 'fixture-remote-selector', PATH: 'fixture-path' });
    assert.throws(() => main(args, () => assert.fail('No Docker probe with overrides'), __dirname, environment),
      /PRODUCTION_DOCKER_ENV_OVERRIDE_FORBIDDEN/);
    assert.equal(environment[name], 'fixture-remote-selector');
  }
});

test('every child receives a separate sanitized environment and the daemon default builder', () => {
  const environment = Object.freeze({ PATH: 'fixture-path', DOCKER_HOST: '', BUILDX_BUILDER: '' });
  const { run } = docker(); const invocations = [];
  main(args, (command, parameters, options) => {
    invocations.push({ parameters, options });
    assert.notEqual(options.env, environment);
    assert.equal(options.env.PATH, 'fixture-path');
    assert.equal('DOCKER_HOST' in options.env, false); assert.equal('BUILDX_BUILDER' in options.env, false);
    return run(command, parameters);
  }, __dirname, environment);
  const build = invocations.find(call => call.parameters.includes('build'));
  assert.equal(build.parameters[build.parameters.indexOf('--context') + 1], 'default');
  assert.equal(build.parameters[build.parameters.indexOf('--builder') + 1], 'default');
  assert.equal(environment.DOCKER_HOST, ''); assert.equal(environment.BUILDX_BUILDER, '');
});
