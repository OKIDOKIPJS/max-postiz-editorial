const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('Polkit grants only the fixed restart to the constrained helper service identity', () => {
  let rule;
  const Result = { YES: 'yes', NOT_HANDLED: 'not-handled' };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '50-max-postiz-editorial-restart.rules'), 'utf8'), {
    polkit: { Result, addRule: value => { rule = value; } },
  });
  const input = { action: 'org.freedesktop.systemd1.manage-units', unit: 'max-postiz-editorial.service',
    verb: 'restart', user: 'editorial', system_unit: 'max-postiz-editorial-restart.service', no_new_privileges: true };
  function allowed(value) {
    return rule({ id: value.action, lookup: key => value[key] }, {
      user: value.user, system_unit: value.system_unit, no_new_privileges: value.no_new_privileges,
    });
  }
  assert.equal(allowed(input), Result.YES);
  for (const mutation of [{ action: 'org.freedesktop.systemd1.manage-unit-files' }, { unit: 'ssh.service' },
    { unit: 'max-postiz-editorial.service.evil' }, { verb: 'stop' }, { verb: 'start' }, { user: 'another' },
    { system_unit: 'max-postiz-editorial.service' }, { system_unit: undefined }, { no_new_privileges: false }]) {
    assert.equal(allowed({ ...input, ...mutation }), Result.NOT_HANDLED);
  }
});
