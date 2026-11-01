const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

const UPSTREAM_BACKEND_MAIN = 'apps/backend/src/main.ts';
const UPSTREAM_BACKEND_MAIN_SHA256 = 'b1af24e3193e02c5919219225351d57db67b7a36ade2fcc067e0c04653af923a';
const EDITORIAL_BODY_LIMIT = '15mb';
const ANCHOR = '  app.use(cookieParser());';
const ADDITION = `  // Only the owner-only editorial admin envelope can contain a bounded image.
  // Ordinary routes retain Nest's default JSON limit; authorization remains in BFF.
  app.use((req: any, res: any, next: any) => {
    if (req.method === 'POST' && req.path === '/editorial/route-admin') {
      return json({ limit: '${EDITORIAL_BODY_LIMIT}' })(req, res, next);
    }
    return next();
  });

`;

function planBackendMain(raw) {
  if (!Buffer.isBuffer(raw) || crypto.createHash('sha256').update(raw).digest('hex') !== UPSTREAM_BACKEND_MAIN_SHA256) {
    throw Error('PRODUCTION_BACKEND_SOURCE_MISMATCH');
  }
  const text = raw.toString('utf8');
  if (text.split(ANCHOR).length !== 2) throw Error('PRODUCTION_BACKEND_ANCHOR_INVALID');
  return text.replace(ANCHOR, ADDITION + ANCHOR);
}

function main(root = '/app') {
  const target = path.join(root, UPSTREAM_BACKEND_MAIN);
  const planned = planBackendMain(fs.readFileSync(target));
  fs.writeFileSync(target, planned);
  return { route: '/editorial/route-admin', json_limit: EDITORIAL_BODY_LIMIT,
    ordinary_route_limits_changed: false, owner_authorization_required: true };
}

module.exports = { UPSTREAM_BACKEND_MAIN, UPSTREAM_BACKEND_MAIN_SHA256, EDITORIAL_BODY_LIMIT,
  ADDITION, planBackendMain, main };
if (require.main === module) {
  try { console.log(JSON.stringify(main())); }
  catch (error) { console.error(error.message); process.exitCode = 1; }
}
