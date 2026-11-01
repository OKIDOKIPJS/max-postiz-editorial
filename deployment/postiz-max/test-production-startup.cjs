const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const startup = require('./production-startup.cjs');

function fixture(fn) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'postiz-startup-contract-'));
  try {
    for (const [name, scripts] of Object.entries(startup.APP_SCRIPTS)) {
      const directory = path.join(root, 'apps', name);
      fs.mkdirSync(directory, { recursive: true });
      fs.writeFileSync(path.join(directory, 'package.json'), JSON.stringify({ name, scripts }));
    }
    return fn(root);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
}

test('the explicit shell command replaces the inherited entrypoint and pins the reviewed bytes', () => fixture(root => {
  assert.deepEqual(startup.SAFE_ENTRYPOINT, []);
  assert.deepEqual(startup.SAFE_CMD, ['/bin/sh', '/adapter/start-production.sh']);
  const raw = fs.readFileSync(path.join(__dirname, 'start-production.sh'));
  assert.equal(crypto.createHash('sha256').update(raw).digest('hex'), startup.SCRIPT_SHA256);
  assert.equal(raw.includes(13), false);
  assert.deepEqual(startup.main(['--check', root]), {
    startup_contract: 'postiz-no-implicit-schema-v1', entrypoint: [],
    command: ['/bin/sh', '/adapter/start-production.sh'], startup_script_sha256: startup.SCRIPT_SHA256,
    application_count: 3, implicit_schema_changes: false, applications_started: false,
  });
}));

test('boot starts only the exact three filtered application scripts, never a root schema alias', () => {
  const source = fs.readFileSync(path.join(__dirname, 'start-production.sh'), 'utf8');
  assert.ok(source.startsWith('#!/bin/sh\n'));
  assert.match(source, /^set -eu$/m);
  assert.match(source, /^cd \/app$/m);
  assert.deepEqual(source.split('\n').filter(line => line.startsWith('run ')), [
    'run nginx', 'run pnpm --filter ./apps/backend run pm2',
    'run pnpm --filter ./apps/orchestrator run pm2',
    'run pnpm --filter ./apps/frontend run pm2', 'run pm2 logs',
  ]);
  assert.doesNotMatch(source, /prisma|db[- ]push|migrat(?:e|ion)|force-reset|pnpm (?:run )?pm2|pm2-run/);
});

test('fixed local cleanup catches both termination signals and bounds its clients', () => {
  const source = fs.readFileSync(path.join(__dirname, 'start-production.sh'), 'utf8');
  assert.match(source, /trap 'cleanup; exit 130' INT/);
  assert.match(source, /trap 'cleanup; exit 143' TERM/);
  assert.match(source, /trap cleanup EXIT/);
  assert.match(source, /trap - EXIT INT TERM/);
  assert.match(source, /\[\["pm2", \["kill"\]\], \["nginx", \["-s", "quit"\]\]\]/);
  assert.match(source, /setTimeout\(\(\) => process\.exit\(1\), 6000\)/);
  assert.match(source, /child\.kill\("SIGKILL"\)/);
  assert.match(source, /"\$@" &\n  wait "\$!"/);
  assert.doesNotMatch(source, /killall|pkill|systemctl|docker|\beval\b/);
});

for (const application of Object.keys(startup.APP_SCRIPTS)) {
  for (const field of ['start', 'pm2']) {
    test(`changed ${application} ${field} is rejected without running project commands`, () => fixture(root => {
      const file = path.join(root, 'apps', application, 'package.json');
      const value = JSON.parse(fs.readFileSync(file));
      value.scripts[field] += ' && pnpm run prisma-db-push';
      fs.writeFileSync(file, JSON.stringify(value));
      const before = fs.readFileSync(file);
      assert.throws(() => startup.validateStartup(root), /PRODUCTION_STARTUP_APP_SCRIPTS_MISMATCH/);
      assert.deepEqual(fs.readFileSync(file), before);
    }));
  }
}

test('untrusted root aliases and unrelated package fields are never loaded or executed', () => fixture(root => {
  fs.writeFileSync(path.join(root, 'package.json'), JSON.stringify({
    scripts: { pm2: 'pnpm run prisma-db-push', postinstall: 'throw private data' },
  }));
  fs.writeFileSync(path.join(root, 'index.js'), "throw new Error('PROJECT_JS_MUST_NOT_BE_LOADED');\n");
  assert.equal(startup.validateStartup(root).application_count, 3);
  const checker = fs.readFileSync(path.join(__dirname, 'production-startup.cjs'), 'utf8');
  assert.doesNotMatch(checker, /child_process|\bspawn(?:Sync)?\s*\(|\bexec(?:Sync|File)?\s*\(|writeFile|appendFile/);
}));

test('changed startup bytes and a different script name are rejected', () => fixture(root => {
  const directory = path.join(root, 'adapter');
  fs.mkdirSync(directory);
  const file = path.join(directory, 'start-production.sh');
  fs.copyFileSync(path.join(__dirname, 'start-production.sh'), file);
  assert.equal(startup.validateStartup(root, file).applications_started, false);
  fs.appendFileSync(file, '\npnpm run pm2\n');
  assert.throws(() => startup.validateStartup(root, file), /PRODUCTION_STARTUP_SCRIPT_MISMATCH/);
  assert.throws(() => startup.validateStartup(root, path.join(directory, 'other.sh')), /PRODUCTION_STARTUP_SCRIPT_INVALID/);
}));

test('missing, malformed and oversized source contracts fail closed', () => fixture(root => {
  const file = path.join(root, 'apps/backend/package.json');
  for (const value of ['{', 'null', JSON.stringify({ scripts: [] }), ' '.repeat(256 * 1024 + 1)]) {
    fs.writeFileSync(file, value);
    assert.throws(() => startup.validateStartup(root), /PRODUCTION_STARTUP_(?:SOURCE_INVALID|APP_SCRIPTS_MISMATCH)/);
  }
  fs.rmSync(file);
  assert.throws(() => startup.validateStartup(root), /PRODUCTION_STARTUP_SOURCE_INVALID/);
  assert.throws(() => startup.main(['--check']), /PRODUCTION_STARTUP_ARGUMENTS_INVALID/);
  assert.throws(() => startup.main(['--start', root]), /PRODUCTION_STARTUP_ARGUMENTS_INVALID/);
}));

test('a linked application directory cannot replace a reviewed source contract', () => fixture(root => {
  const original = path.join(root, 'apps/backend');
  const renamed = path.join(root, 'apps/backend-original');
  fs.renameSync(original, renamed);
  fs.symlinkSync(renamed, original, process.platform === 'win32' ? 'junction' : 'dir');
  assert.throws(() => startup.validateStartup(root), /PRODUCTION_STARTUP_SOURCE_INVALID/);
  assert.equal(JSON.parse(fs.readFileSync(path.join(renamed, 'package.json'))).scripts.start,
    startup.APP_SCRIPTS.backend.start);
}));
