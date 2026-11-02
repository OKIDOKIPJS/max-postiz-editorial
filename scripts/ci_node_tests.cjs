// Проверяем контракты на mocks; образ Postiz, Docker и рабочие сервисы не нужны.
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const root = path.resolve(__dirname, '..');
const adapter = path.join(root, 'deployment', 'postiz-max');
const tooling = path.join(root, '.github', 'ci-node', 'node_modules');
const hash = value => crypto.createHash('sha256').update(value).digest('hex');

function main() {
  const ts = require(path.join(tooling, 'typescript'));
  assert.equal(ts.version, '5.5.4', 'Install the pinned .github/ci-node dependencies with npm ci');
  assert.equal(require(path.join(tooling, 'tslib', 'package.json')).version, '2.8.1');
  const manifest = JSON.parse(fs.readFileSync(path.join(adapter, 'cas-manifest.json'), 'utf8'));
  assert.equal(manifest.contract, 'max-browser-cas-draft-schedule-v1');
  for (const name of ['target_source_sha256', 'target_compiled_sha256']) {
    assert.match(manifest[name], /^[a-f0-9]{64}$/);
  }

  // Git хранит LF; Windows checkout может заменить их на CRLF без изменения кода.
  const source = fs.readFileSync(path.join(adapter, 'max-browser.guard.ts'), 'utf8').replace(/\r\n/g, '\n');
  assert.equal(hash(source), manifest.target_source_sha256, 'Pinned CAS source mismatch');
  const compiled = ts.transpileModule(source, {
    fileName: 'max-browser.guard.ts',
    reportDiagnostics: true,
    compilerOptions: {
      target: ts.ScriptTarget.ES2017,
      module: ts.ModuleKind.CommonJS,
      experimentalDecorators: true,
      emitDecoratorMetadata: true,
      importHelpers: true,
      esModuleInterop: true,
      sourceMap: false,
      newLine: ts.NewLineKind.LineFeed,
    },
  });
  assert.deepEqual((compiled.diagnostics || []).filter(item => item.category === ts.DiagnosticCategory.Error), []);
  const alias = '@gitroom/nestjs-libraries/';
  assert.equal(compiled.outputText.split(alias).length, 3, 'Unexpected CAS guard imports');
  const output = compiled.outputText.split(alias).join('../../../libraries/nestjs-libraries/src/');
  assert.ok(!output.includes('@gitroom/'), 'Unresolved CAS guard imports');
  assert.equal(hash(output), manifest.target_compiled_sha256, 'Pinned CAS compilation mismatch');

  const temporaryParent = path.resolve(os.tmpdir());
  const temporary = fs.mkdtempSync(path.join(temporaryParent, 'max-postiz-ci-node-'));
  try {
    // Все текстовые fixtures читаются с теми же LF, которые хранятся в Git.
    // Копия нужна также существующим проверкам буквальных TypeScript-фрагментов.
    const fixtureAdapter = path.join(temporary, 'adapter');
    fs.mkdirSync(fixtureAdapter);
    for (const entry of fs.readdirSync(adapter, { withFileTypes: true })) {
      if (!entry.isFile() || !(entry.name.startsWith('Dockerfile') ||
          entry.name === 'start-production.sh' ||
          ['.cjs', '.ts', '.tsx', '.json', '.css'].includes(path.extname(entry.name)))) continue;
      fs.writeFileSync(path.join(fixtureAdapter, entry.name),
        fs.readFileSync(path.join(adapter, entry.name), 'utf8').replace(/\r\n/g, '\n'), { flag: 'wx' });
    }
    // Копируем только явно требуемый публичный fixture, без runtime-каталогов.
    for (const name of ['fixtures/production-next.config.upstream.cjs', 'fixtures/production-backend-main.upstream.ts']) {
      const destination = path.join(fixtureAdapter, name);
      fs.mkdirSync(path.dirname(destination), { recursive: true });
      fs.writeFileSync(destination,
        fs.readFileSync(path.join(adapter, name), 'utf8').replace(/\r\n/g, '\n'), { flag: 'wx' });
    }
    // Structural mock проверяет COPY этой публичной иконки; её байты не нормализуем.
    fs.copyFileSync(path.join(adapter, 'max-browser.png'), path.join(fixtureAdapter, 'max-browser.png'),
      fs.constants.COPYFILE_EXCL);
    const filename = path.join(temporary, 'max-browser.guard.js');
    fs.writeFileSync(filename, output, { flag: 'wx' });
    const suites = [
      'test-cas-build.cjs',
      'test-cas-guard.cjs',
      'test-editorial-contract.cjs',
      'test-provider-reliability-build.cjs',
      'test-provider-unavailable.cjs',
      'test-production-build.cjs',
      'test-production-startup.cjs',
    ];
    assert.deepEqual(fs.readdirSync(adapter).filter(name => /^test-.*\.cjs$/.test(name)).sort(),
      [...suites].sort(), 'Update the CI runner when adding a Node contract suite');
    const ownerPortal = path.join(temporary, 'owner-portal');
    fs.mkdirSync(ownerPortal);
    for (const name of ['test-owner-portal.cjs', '50-max-postiz-editorial-restart.rules']) {
      fs.writeFileSync(path.join(ownerPortal, name),
        fs.readFileSync(path.join(root, 'deployment', 'linux', name), 'utf8').replace(/\r\n/g, '\n'), { flag: 'wx' });
    }
    const result = spawnSync(process.execPath, [
      '--unhandled-rejections=strict', '--test', '--test-reporter=tap',
      ...suites.map(name => path.join(fixtureAdapter, name)),
      path.join(ownerPortal, 'test-owner-portal.cjs'),
    ], {
      cwd: root,
      env: {
        ...process.env,
        EDITORIAL_TOOLING: tooling,
        MAX_CAS_COMPILED: filename,
        MAX_CAS_TSLIB: path.join(tooling, 'tslib'),
        // Проверяем исходник провайдера, не унаследованный путь из Docker.
        MAX_PROVIDER_COMPILED: '',
      },
      encoding: 'utf8',
      timeout: 120000,
      maxBuffer: 8 * 1024 * 1024,
      windowsHide: true,
    });
    if (result.stdout) process.stdout.write(result.stdout);
    if (result.stderr) process.stderr.write(result.stderr);
    if (result.error) throw result.error;
    assert.equal(result.status, 0, `Node contract checks failed (${result.signal || result.status})`);
    const summary = Object.fromEntries(
      [...result.stdout.matchAll(/^# (tests|pass|fail|cancelled|skipped|todo) (\d+)\r?$/gm)]
        .map(match => [match[1], Number(match[2])]),
    );
    assert.ok(summary.tests > 0, 'Missing Node test summary');
    assert.equal(summary.pass, summary.tests, 'Every Node contract test must pass');
    for (const name of ['fail', 'cancelled', 'skipped', 'todo']) {
      assert.equal(summary[name], 0, `Node contract checks must have zero ${name}`);
    }
    console.log(JSON.stringify({
      node_contract_checks: 'passed',
      scope: 'mocked_contracts',
      files: suites.length,
      tests: summary.tests,
      cas_source_sha256: manifest.target_source_sha256,
      cas_compiled_sha256: manifest.target_compiled_sha256,
    }));
  } finally {
    // Удаляем только созданный этим процессом каталог внутри системного temp.
    assert.equal(path.dirname(temporary), temporaryParent);
    assert.ok(path.basename(temporary).startsWith('max-postiz-ci-node-'));
    fs.rmSync(temporary, { recursive: true, force: true });
  }
}

try {
  main();
} catch (error) {
  console.error(error);
  process.exitCode = 1;
}
