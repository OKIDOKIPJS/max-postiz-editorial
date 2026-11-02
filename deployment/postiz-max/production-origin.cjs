const DEFAULT_ORIGIN_PROFILE = 'https-or-loopback-v1';
const LEGACY_ORIGIN_PROFILE = 'legacy-private-lan-http-v1';
const LEGACY_PRIVATE_ORIGIN = 'http://10.20.30.45:4007';

function securityForOrigin(value, profile = DEFAULT_ORIGIN_PROFILE) {
  if (![DEFAULT_ORIGIN_PROFILE, LEGACY_ORIGIN_PROFILE].includes(profile)) {
    throw Error('PRODUCTION_ORIGIN_PROFILE_INVALID');
  }
  let url;
  try { url = new URL(value); } catch { throw Error('PRODUCTION_BUILD_ORIGIN_INVALID'); }
  if (typeof value !== 'string' || value !== url.origin || url.username || url.password ||
      !['http:', 'https:'].includes(url.protocol)) throw Error('PRODUCTION_BUILD_ORIGIN_INVALID');
  if (profile === LEGACY_ORIGIN_PROFILE && value !== LEGACY_PRIVATE_ORIGIN) {
    throw Error('PRODUCTION_BUILD_ORIGIN_INVALID');
  }
  if (profile === DEFAULT_ORIGIN_PROFILE && url.protocol === 'http:' && !['127.0.0.1', '[::1]'].includes(url.hostname)) {
    throw Error('PRODUCTION_BUILD_ORIGIN_INVALID');
  }
  return { origin: value, not_secured: url.protocol === 'https:' ? '' : 'true', origin_profile: profile };
}

function validateBuildSecurity(origin, selected, profile = DEFAULT_ORIGIN_PROFILE) {
  const expected = securityForOrigin(origin, profile);
  // Upstream tests this flag for truthiness: even the text "false" is unsafe.
  if (selected !== expected.not_secured) throw Error('PRODUCTION_COOKIE_MODE_INVALID');
  return expected;
}

function main(args) {
  if (![2, 3].includes(args.length)) throw Error('PRODUCTION_ORIGIN_ARGUMENTS_INVALID');
  return validateBuildSecurity(args[0], args[1], args[2]);
}

module.exports = { DEFAULT_ORIGIN_PROFILE, LEGACY_ORIGIN_PROFILE, LEGACY_PRIVATE_ORIGIN,
  securityForOrigin, validateBuildSecurity, main };
if (require.main === module) {
  try { console.log(JSON.stringify(main(process.argv.slice(2)))); }
  catch (error) { console.error(error.message); process.exitCode = 1; }
}
