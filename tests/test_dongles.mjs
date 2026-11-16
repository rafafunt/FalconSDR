// Run by tests/test_addons.py (DongleChoiceTest) - the rules of the Listen page's extra-dongle choices, without a browser.
import assert from 'node:assert/strict';
import { groupDevices, groupOfExtra, heldGroups, isTwice, nextGroup, takenByExtra, usableFor } from '../falconsdr/web/static/js/views/dongles.js';

const dev = (driver, args, group, method) => ({ driver, args, group, method, label: `${group} - ${method}` });
// Two real dongles, each openable two ways, plus the network entry that is always listed.
const devices = [
  dev('auto', '', 'auto', 'auto'),
  dev('rtlsdr', 'serial=A', 'A', 'soapy'), dev('rtlsdr_native', 'index=0', 'A', 'librtlsdr'),
  dev('rtlsdr', 'serial=B', 'B', 'soapy'), dev('rtlsdr_native', 'index=1', 'B', 'librtlsdr'),
  dev('rtl_tcp', '', 'rtl_tcp', 'rtl_tcp'),
];
const groups = groupDevices(devices).filter((g) => g.entries.some(usableFor(false)));
const by = (name) => groups.find((g) => g.group === name);
const main = { driver: 'rtlsdr', device_args: 'serial=A' };

assert.deepEqual(groups.map((g) => g.group), ['A', 'B', 'rtl_tcp']);

// The second dongle is B whichever way it is opened - and A is held by the first.
const viaNative = { driver: 'rtlsdr_native', device_args: 'index=1' };
assert.equal(groupOfExtra(groups, viaNative).group, 'B');
assert.deepEqual([...heldGroups(groups, 'A', [viaNative], -1)].sort(), ['A', 'B']);
assert.deepEqual([...heldGroups(groups, 'A', [viaNative], 0)], ['A']);          // its own row may keep it
assert.equal(takenByExtra(by('B'), [viaNative]), true);
assert.equal(takenByExtra(by('A'), [viaNative]), false);

// "Scan with another dongle": B first, then nothing free but a network receiver, then none until the address is typed.
assert.equal(nextGroup(groups, 'A', main, []).group, 'B');
assert.equal(nextGroup(groups, 'A', main, [viaNative]).group, 'rtl_tcp');
const net1 = { driver: 'rtl_tcp', device_args: '' };
assert.equal(nextGroup(groups, 'A', main, [viaNative, net1]), null);
const net1typed = { driver: 'rtl_tcp', device_args: 'host=10.0.0.5,port=1234' };
assert.equal(nextGroup(groups, 'A', main, [viaNative, net1typed]).group, 'rtl_tcp');           // a second network receiver is fine
assert.equal(takenByExtra(by('rtl_tcp'), [net1typed]), false);

// The same receiver twice (an old config) is flagged on both sides.
assert.equal(isTwice(main, [{ driver: 'rtlsdr', device_args: 'serial=A' }], 0), true);
assert.equal(isTwice(main, [viaNative, { ...viaNative }], 1), true);
assert.equal(isTwice(main, [viaNative], 0), false);

// A simulation never mixes with real receivers.
const sim = [dev('simulated', '', 'simulated', 'simulated'), ...devices];
assert.deepEqual(groupDevices(sim).filter((g) => g.entries.some(usableFor(true))).map((g) => g.group), ['simulated']);
console.log('ok');
