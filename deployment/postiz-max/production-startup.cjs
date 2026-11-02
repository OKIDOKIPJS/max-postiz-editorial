// Read-only build/image contract. Never loads project JS or starts an application.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

const STARTUP_CONTRACT = 'postiz-no-implicit-schema-v1';
const SAFE_ENTRYPOINT = Object.freeze([]);
const SAFE_CMD = Object.freeze(['/bin/sh', '/adapter/start-production.sh']);
const SCRIPT_SHA256 = '9272bf9f32b883f02269248962bf656966819015fd3f36f57f0a5774a445af41';
const APP_SCRIPTS = Object.freeze({
  backend: Object.freeze({
    start: 'dotenv -e ../../.env -- node --experimental-require-module ./dist/apps/backend/src/main.js',
    pm2: 'pm2 start pnpm --name backend -- start',
  }),
  orchestrator: Object.freeze({
    start: 'dotenv -e ../../.env -- node --experimental-require-module ./dist/apps/orchestrator/src/main.js',
    pm2: 'pm2 start pnpm --name orchestrator -- start',
  }),
  frontend: Object.freeze({
    start: 'dotenv -e ../../.env -- next start -p 4200',
    pm2: 'pm2 start pnpm --name frontend -- start',
  }),
});

function requireValue(value, code) { if (!value) throw Error(code); }

function regularFile(root, relative) {
  const parts = relative.split('/');
  let current = root;
  for (const [index, part] of parts.entries()) {
    current = path.join(current, part);
    const stat = fs.lstatSync(current);
    requireValue(!stat.isSymbolicLink() && (index === parts.length - 1 ? stat.isFile() : stat.isDirectory()),
      'PRODUCTION_STARTUP_SOURCE_INVALID');
  }
  requireValue(fs.statSync(current).size <= 256 * 1024, 'PRODUCTION_STARTUP_SOURCE_INVALID');
  return fs.readFileSync(current);
}

function validateStartup(root, script = path.join(__dirname, 'start-production.sh')) {
  try {
    requireValue(typeof root === 'string' && path.isAbsolute(root) && fs.realpathSync(root) === path.resolve(root),
      'PRODUCTION_STARTUP_ROOT_INVALID');
    const scriptRoot = path.dirname(script);
    requireValue(path.basename(script) === 'start-production.sh' && fs.realpathSync(scriptRoot) === path.resolve(scriptRoot),
      'PRODUCTION_STARTUP_SCRIPT_INVALID');
    const raw = regularFile(scriptRoot, 'start-production.sh');
    requireValue(crypto.createHash('sha256').update(raw).digest('hex') === SCRIPT_SHA256,
      'PRODUCTION_STARTUP_SCRIPT_MISMATCH');
    for (const [application, expected] of Object.entries(APP_SCRIPTS)) {
      const value = JSON.parse(regularFile(root, `apps/${application}/package.json`).toString('utf8'));
      requireValue(value && typeof value.scripts === 'object' && value.scripts !== null && !Array.isArray(value.scripts)
        && value.scripts.start === expected.start && value.scripts.pm2 === expected.pm2,
      'PRODUCTION_STARTUP_APP_SCRIPTS_MISMATCH');
    }
    return { startup_contract: STARTUP_CONTRACT, entrypoint: [...SAFE_ENTRYPOINT], command: [...SAFE_CMD],
      startup_script_sha256: SCRIPT_SHA256, application_count: Object.keys(APP_SCRIPTS).length,
      implicit_schema_changes: false, applications_started: false };
  } catch (error) {
    if (/^PRODUCTION_STARTUP_[A-Z_]+$/.test(error.message || '')) throw error;
    throw Error('PRODUCTION_STARTUP_SOURCE_INVALID');
  }
}

function main(args) {
  requireValue(args.length === 2 && args[0] === '--check', 'PRODUCTION_STARTUP_ARGUMENTS_INVALID');
  return validateStartup(args[1]);
}

module.exports = { STARTUP_CONTRACT, SAFE_ENTRYPOINT, SAFE_CMD, SCRIPT_SHA256, APP_SCRIPTS, validateStartup, main };
if (require.main === module) {
  try { console.log(JSON.stringify(main(process.argv.slice(2)))); }
  catch (error) { console.error(error.message); process.exitCode = 1; }
}
