// Which receivers may be picked for the first and the extra dongles (the Listen page's SDR device card). Pure functions,
// so tests/test_dongles.mjs can run them without a browser. A "group" is one physical receiver; its entries are the ways
// to open it (SoapySDR, librtlsdr). A network receiver (rtl_tcp) is the one group that may be used several times.
export const groupOf = (d) => d.group || d.driver;      // the online demo's devices have no group
export const NETWORK = 'rtl_tcp';

export function groupDevices(devices) {
  const groups = [];
  for (const d of devices) {
    let g = groups.find((x) => x.group === groupOf(d));
    if (!g) groups.push(g = { group: groupOf(d), entries: [] });
    g.entries.push(d);
  }
  return groups;
}

// An extra dongle must be a specific one, and never a simulation next to real hardware (fake transmissions in the library).
export const usableFor = (sim) => (d) => d.driver !== 'auto' && (d.driver === 'simulated') === sim;

export const groupOfExtra = (groups, x) => groups.find((g) => g.entries.some((d) => d.driver === x.driver && (d.driver === NETWORK || d.args === x.device_args)));

// Groups already in use: the first dongle's and every extra's except the one at index `except` (-1 = none).
export function heldGroups(groups, mainGroup, extra, except) {
  return new Set([mainGroup, ...extra.filter((_, j) => j !== except).map((x) => (groupOfExtra(groups, x) || {}).group)].filter((g) => g && g !== NETWORK));
}

export const takenByExtra = (group, extra) => group.group !== NETWORK && extra.some((x) => group.entries.some((d) => d.driver === x.driver && d.args === x.device_args));

// The same receiver opened twice (only possible from an old config): the first dongle or another extra already has it.
export const isTwice = (main, extra, i) => {
  const x = extra[i];
  return (x.driver === main.driver && x.device_args === main.device_args) || extra.some((o, j) => j !== i && o.driver === x.driver && o.device_args === x.device_args);
};

// The group "Scan with another dongle" adds: a receiver nobody holds, else one more network receiver - unless one still has no address.
export function nextGroup(groups, mainGroup, mainDevice, extra) {
  const held = heldGroups(groups, mainGroup, extra, -1);
  const free = groups.find((g) => g.group !== NETWORK && !held.has(g.group));
  if (free) return free;
  const network = groups.find((g) => g.group === NETWORK);
  const addressMissing = [mainDevice, ...extra].some((x) => x.driver === NETWORK && !x.device_args);
  return network && !addressMissing ? network : null;
}
