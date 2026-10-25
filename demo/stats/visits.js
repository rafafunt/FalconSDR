// njs for nginx: a visitor id = hash of the IP with today's salt. Only this id
// is logged, never the IP; the salt is replaced and deleted every midnight
// (falconsdr-stats --rotate), after which the ids can't be tied to anyone.
import fs from 'fs';
import crypto from 'crypto';

function visitor(r) {
  let salt;
  try { salt = fs.readFileSync('/var/lib/falconsdr-stats/salt'); } catch (e) { return '-'; }
  return crypto.createHash('sha256').update(salt).update(r.remoteAddress).digest('hex').slice(0, 16);
}

export default { visitor };
