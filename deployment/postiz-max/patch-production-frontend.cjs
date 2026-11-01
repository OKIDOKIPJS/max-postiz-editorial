const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

const UPSTREAM_NEXT_CONFIG = 'apps/frontend/next.config.js';
const UPSTREAM_NEXT_CONFIG_SHA256 = 'd11527374f71986ebc735f639ebb799b817d57ca42e1907db687ce476a183c49';

function planFrontendConfig(raw) {
  if (!Buffer.isBuffer(raw) || crypto.createHash('sha256').update(raw).digest('hex') !== UPSTREAM_NEXT_CONFIG_SHA256) {
    throw Error('PRODUCTION_FRONTEND_SOURCE_MISMATCH');
  }
  const text = raw.toString('utf8');
  const marker = 'const nextConfig = {\n';
  if (text.split(marker).length !== 2 || text.includes('serverExternalPackages')) {
    throw Error('PRODUCTION_FRONTEND_CONFIG_ANCHOR_INVALID');
  }
  // Keep jsdom's Node filesystem resources in its installed package. The
  // sanitizer and the separate browser export remain enabled and unchanged.
  return text.replace(marker, marker + "  serverExternalPackages: ['isomorphic-dompurify'],\n");
}

function main(root = '/app') {
  const target = path.join(path.resolve(root), UPSTREAM_NEXT_CONFIG);
  const planned = planFrontendConfig(fs.readFileSync(target));
  fs.writeFileSync(target, planned);
  return { source: UPSTREAM_NEXT_CONFIG, source_sha256: UPSTREAM_NEXT_CONFIG_SHA256,
    server_external_packages: ['isomorphic-dompurify'], sanitizer_changed: false };
}

module.exports = { UPSTREAM_NEXT_CONFIG, UPSTREAM_NEXT_CONFIG_SHA256, planFrontendConfig, main };
if (require.main === module) console.log(JSON.stringify(main(process.argv[2] || '/app')));
