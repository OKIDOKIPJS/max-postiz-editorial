function securityForOrigin(value) {
  let url;
  try { url = new URL(value); } catch { throw Error('PRODUCTION_BUILD_ORIGIN_INVALID'); }
  if (typeof value !== 'string' || value !== url.origin || url.username || url.password ||
      !['http:', 'https:'].includes(url.protocol)) throw Error('PRODUCTION_BUILD_ORIGIN_INVALID');
  if (url.protocol === 'http:' && !['127.0.0.1', '[::1]'].includes(url.hostname)) {
    throw Error('PRODUCTION_BUILD_ORIGIN_INVALID');
  }
  return { origin: value, not_secured: url.protocol === 'https:' ? '' : 'true' };
}

function validateBuildSecurity(origin, selected) {
  const expected = securityForOrigin(origin);
  // Upstream tests this flag for truthiness: even the text "false" is unsafe.
  if (selected !== expected.not_secured) throw Error('PRODUCTION_COOKIE_MODE_INVALID');
  return expected;
}

function main(args) {
  if (args.length !== 2) throw Error('PRODUCTION_ORIGIN_ARGUMENTS_INVALID');
  return validateBuildSecurity(args[0], args[1]);
}

module.exports = { securityForOrigin, validateBuildSecurity, main };
if (require.main === module) {
  try { console.log(JSON.stringify(main(process.argv.slice(2)))); }
  catch (error) { console.error(error.message); process.exitCode = 1; }
}
