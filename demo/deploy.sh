#!/bin/sh
# Publishes demo/dist (run demo/build.py first) to falconsdr.com.
set -e
cd "$(dirname "$0")"
rsync -az --delete -e 'ssh -p 2137' dist/ ubuntu@deravativecraft.fun:/var/www/falconsdr.com/
