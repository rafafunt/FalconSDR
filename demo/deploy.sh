#!/bin/sh
# Publishes demo/dist (run demo/build.py first) to the web host named in
# demo/deploy.env (not in git; see deploy.env.example).
set -e
cd "$(dirname "$0")"
[ -f deploy.env ] && . ./deploy.env
: "${DEPLOY_TARGET:?set DEPLOY_TARGET=user@host:/var/www/site/ (and optionally DEPLOY_SSH_PORT) in demo/deploy.env}"
rsync -az --delete -e "ssh -p ${DEPLOY_SSH_PORT:-22}" dist/ "$DEPLOY_TARGET"
