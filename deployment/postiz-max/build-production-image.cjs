const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawnSync } = require('node:child_process');
const { UPSTREAM_NEXT_CONFIG, UPSTREAM_NEXT_CONFIG_SHA256 } = require('./patch-production-frontend.cjs');
const { securityForOrigin } = require('./production-origin.cjs');

function argumentsFor(args) {
  const options = {};
  for (let index = 0; index < args.length; index++) {
    const key = args[index];
    if (!['--check', '--build', '--origin', '--tag', '--source-root'].includes(key) || key in options) throw Error('PRODUCTION_BUILD_ARGUMENTS_INVALID');
    if (key === '--check' || key === '--build') options[key] = true;
    else {
      const value = args[++index];
      if (!value || value.startsWith('--')) throw Error('PRODUCTION_BUILD_ARGUMENTS_INVALID');
      options[key] = value;
    }
  }
  if (Boolean(options['--check']) === Boolean(options['--build'])) throw Error('PRODUCTION_BUILD_ARGUMENTS_INVALID');
  if (options['--build'] && (!options['--origin'] || !options['--tag'])) throw Error('PRODUCTION_BUILD_ARGUMENTS_INVALID');
  if (options['--origin']) {
    securityForOrigin(options['--origin']);
  }
  if (options['--tag'] && (!/^max-postiz-linux:[A-Za-z0-9][A-Za-z0-9_.-]{0,100}$/.test(options['--tag']) ||
      ['max-postiz-linux:0.5.1', 'max-postiz-linux:0.10.0-editorial', 'max-postiz-linux:0.10.8-reliability', 'max-postiz-linux:0.11.2-illustrated-cas'].includes(options['--tag']))) {
    throw Error('PRODUCTION_BUILD_TAG_INVALID');
  }
  return options;
}

function structuralCheck(adapter, sourceRoot) {
  const upstream = JSON.parse(fs.readFileSync(path.join(adapter, 'source-manifest.json')));
  const editorial = JSON.parse(fs.readFileSync(path.join(adapter, 'editorial-manifest.json')));
  const pinned = 'ghcr.io/gitroomhq/postiz-app@sha256:d2b37802e96757729fdea2d3d71287f841c2b619faa1cb4c21dac07df657fde1';
  const dockerfile = fs.readFileSync(path.join(adapter, 'Dockerfile.production'), 'utf8').replace(/\r\n/g, '\n');
  const maxPatch = fs.readFileSync(path.join(adapter, 'patch-image.cjs'), 'utf8');
  if (upstream.base_image !== pinned || !dockerfile.includes('\nFROM ' + pinned + '\n') || editorial.typescript_version !== '5.5.4' ||
      Object.keys(editorial.sources).sort().join('|') !== ['apps/backend/src/api/api.module.ts', 'apps/frontend/src/components/layout/top.menu.tsx'].join('|')) {
    throw Error('PRODUCTION_BUILD_PINS_INVALID');
  }
  for (const name of Object.keys(editorial.sources)) {
    if (name in upstream.sha256 || maxPatch.includes(name)) throw Error('PRODUCTION_BUILD_OVERLAYS_CONFLICT');
  }
  const stages = ['node /adapter/patch-image.cjs', "planPatch('/app','/adapter',{...m,compiled:{}})",
    'pnpm exec prisma generate --schema', 'pnpm --filter ./apps/backend build',
    'pnpm --filter ./apps/orchestrator build', 'node /adapter/patch-production-frontend.cjs',
    'pnpm --filter ./apps/frontend exec next build --webpack',
    'node /adapter/production-origin.cjs "$PUBLIC_ORIGIN" "$PRODUCTION_NOT_SECURED"',
    'ENV NOT_SECURED=$PRODUCTION_NOT_SECURED'];
  let previous = -1;
  for (const stage of stages) { const position = dockerfile.indexOf(stage); if (position <= previous) throw Error('PRODUCTION_BUILD_ORDER_INVALID'); previous = position; }
  if (!dockerfile.includes('planPatch') || /FROM max-postiz|prisma (?:db push|migrate)|COPY \. /.test(dockerfile)) throw Error('PRODUCTION_BUILD_RECIPE_INVALID');
  const pins = { ...upstream.sha256, ...editorial.sources, [UPSTREAM_NEXT_CONFIG]: UPSTREAM_NEXT_CONFIG_SHA256 };
  if (Object.values(pins).some(value => !/^[a-f0-9]{64}$/.test(value))) throw Error('PRODUCTION_BUILD_PINS_INVALID');
  // Every copied adapter file must exist; COPY uses an explicit list, never private runtime.
  for (const line of dockerfile.split('\n').filter(line => line.startsWith('COPY '))) {
    for (const name of line.split(/\s+/).slice(1, -1)) {
      if (!/^[A-Za-z0-9_.-]+$/.test(name) || !fs.statSync(path.join(adapter, name)).isFile()) throw Error('PRODUCTION_BUILD_SOURCE_MISSING');
    }
  }
  if (sourceRoot) {
    const root = path.resolve(sourceRoot);
    for (const [name, expected] of Object.entries(pins)) {
      const actual = crypto.createHash('sha256').update(fs.readFileSync(path.join(root, name))).digest('hex');
      if (actual !== expected) throw Error('PRODUCTION_UPSTREAM_SOURCE_MISMATCH');
    }
  }
  return { recipe: 'fresh-pinned-upstream-v1', upstream_source_pin_count: Object.keys(upstream.sha256).length,
    editorial_source_pin_count: Object.keys(editorial.sources).length, upstream_source_checked: Boolean(sourceRoot),
    frontend_config_source_pin_count: 1,
    compiled_artifact_reused: false, full_native_rebuild_required: true, image_built: false,
    build_status: 'NOT_BUILT', runtime_verified: false };
}

function main(args, run = spawnSync, adapter = __dirname, environment = process.env) {
  const options = argumentsFor(args), checked = structuralCheck(adapter, options['--source-root']);
  if (options['--check']) return checked; // No Docker, network, files or services are changed.
  const cookieMode = securityForOrigin(options['--origin']).not_secured;
  const overrides = new Set(['DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS', 'DOCKER_TLS_VERIFY',
    'DOCKER_CERT_PATH', 'BUILDX_BUILDER', 'BUILDKIT_HOST']);
  const childEnv = { ...environment };
  for (const [name, value] of Object.entries(childEnv)) {
    if (['NOT_SECURED', 'PRODUCTION_NOT_SECURED'].includes(name.toUpperCase())) {
      if (value !== undefined && value !== cookieMode) throw Error('PRODUCTION_COOKIE_MODE_INVALID');
      delete childEnv[name];
    }
    if (overrides.has(name.toUpperCase())) {
      if (value !== undefined && value !== '') throw Error('PRODUCTION_DOCKER_ENV_OVERRIDE_FORBIDDEN');
      delete childEnv[name];
    }
  }
  const binary = process.platform === 'win32' ? 'docker.exe' : 'docker';
  const execute = params => run(binary, params, { encoding: 'utf8', env: childEnv });
  const selected = execute(['context', 'show']);
  const context = selected.stdout?.trim();
  if (selected.status !== 0 || !context || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$/.test(context)) throw Error('PRODUCTION_DOCKER_UNAVAILABLE');
  const inspected = execute(['context', 'inspect', context, '--format', '{{json .Endpoints.docker.Host}}']);
  let endpoint;
  try { endpoint = JSON.parse(inspected.stdout); } catch { throw Error('PRODUCTION_DOCKER_LOCAL_REQUIRED'); }
  if (inspected.status !== 0 || !['unix:///var/run/docker.sock', 'npipe:////./pipe/docker_engine', 'npipe:////./pipe/dockerDesktopLinuxEngine'].includes(endpoint)) throw Error('PRODUCTION_DOCKER_LOCAL_REQUIRED');
  const docker = params => execute(['--context', context, ...params]);
  if (docker(['version', '--format', '{{.Server.Version}}']).status !== 0) throw Error('PRODUCTION_DOCKER_UNAVAILABLE');
  const tag = options['--tag'];
  if (docker(['image', 'inspect', '--format', '{{.Id}}', tag]).status === 0) throw Error('PRODUCTION_TAG_ALREADY_EXISTS');
  const absent = docker(['image', 'ls', '--quiet', '--no-trunc', '--filter', `reference=${tag}`]);
  if (absent.status !== 0) throw Error('PRODUCTION_DOCKER_UNAVAILABLE');
  if (absent.stdout.trim()) throw Error('PRODUCTION_TAG_ALREADY_EXISTS');
  const built = run(binary, ['--context', context, 'build', '--builder', 'default', '--file', path.join(adapter, 'Dockerfile.production'),
    '--build-arg', `PUBLIC_ORIGIN=${options['--origin']}`, '--build-arg', `PRODUCTION_NOT_SECURED=${cookieMode}`,
    '--tag', tag, adapter], { stdio: 'inherit', env: childEnv });
  if (built.status !== 0) throw Error('PRODUCTION_BUILD_FAILED');
  return { ...checked, image_built: true, build_status: 'BUILT', tag, runtime_verified: false };
}
module.exports = { argumentsFor, structuralCheck, main };
if (require.main === module) {
  try { console.log(JSON.stringify(main(process.argv.slice(2)))); }
  catch (error) { console.error(/^[A-Z][A-Z0-9_]+$/.test(error.message) ? error.message : 'PRODUCTION_BUILD_INPUT_INVALID'); process.exitCode = 1; }
}
