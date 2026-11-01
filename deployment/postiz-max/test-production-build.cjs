const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { argumentsFor, structuralCheck, main } = require('./build-production-image.cjs');

const args = ['--build', '--origin', 'http://postiz.test:14007', '--tag', 'max-postiz-linux:0.12.1-production-test'];
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
  assert.ok(builds[0].includes(path.join(__dirname, 'Dockerfile.production')));
  assert.ok(builds[0].includes('PUBLIC_ORIGIN=http://postiz.test:14007'));
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
