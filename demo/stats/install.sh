#!/bin/sh
# Installs the visitor counting on the server: run there as root from this folder.
set -e
install -d -m 750 -o root -g www-data /var/lib/falconsdr-stats
install -D -m 644 visits.js /etc/nginx/njs/falconsdr-visits.js
install -m 644 nginx-visits.conf /etc/nginx/conf.d/falconsdr-visits.conf
install -m 755 falconsdr-stats /usr/local/bin/falconsdr-stats
echo '0 0 * * * root /usr/local/bin/falconsdr-stats --rotate' > /etc/cron.d/falconsdr-stats
[ -f /var/lib/falconsdr-stats/salt ] || /usr/local/bin/falconsdr-stats --rotate
