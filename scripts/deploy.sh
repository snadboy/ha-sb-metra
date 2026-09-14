#!/usr/bin/env bash
# Dev deploy: copy custom_components/metra to a Home Assistant host over SSH.
#
# For iterating before a release. Normal installs come from HACS. Needs an SSH
# host alias with passwordless sudo (default: homeassistant) and, for --reload,
# HASS_SERVER + HASS_TOKEN in the environment.
#
#   scripts/deploy.sh             copy files only
#   scripts/deploy.sh --reload    also reload custom templates (macro-only changes)
#   Python changes need a full Home Assistant restart afterwards.
set -euo pipefail

HOST="${HA_SSH_HOST:-homeassistant}"
DEST="/config/custom_components/metra"
cd "$(dirname "$0")/.."

# metra.jinja carries non-breaking spaces that editors silently turn into plain
# spaces; refuse to deploy if the count changed.
EXPECTED_NBSP=62
nbsp=$(python3 -c "print(open('custom_components/metra/templates/metra.jinja', encoding='utf-8').read().count(' '))")
if [[ "$nbsp" != "$EXPECTED_NBSP" ]]; then
  echo "metra.jinja has $nbsp non-breaking spaces, expected $EXPECTED_NBSP; not deploying" >&2
  exit 1
fi

python3 -m py_compile custom_components/metra/*.py
find custom_components/metra -name __pycache__ -prune -exec rm -rf {} +

stamp=$(date +%Y%m%d_%H%M%S)
ssh "$HOST" "sudo sh -c 'test -d $DEST && cp -a $DEST /config/metra_backup_$stamp || true'"
tar -C custom_components -cf - --exclude=__pycache__ metra \
  | ssh "$HOST" "sudo sh -c 'mkdir -p $DEST && tar -C /config/custom_components -xf - && chown -R root:root $DEST'"

local_sum=$(cd custom_components/metra && find . -type f ! -path '*/__pycache__/*' | sort | xargs md5sum | md5sum | cut -d' ' -f1)
remote_sum=$(ssh "$HOST" "sudo sh -c 'cd $DEST && find . -type f ! -path \"*/__pycache__/*\" | sort | xargs md5sum | md5sum'" | cut -d' ' -f1)
if [[ "$local_sum" != "$remote_sum" ]]; then
  echo "checksum mismatch after copy (local $local_sum, remote $remote_sum)" >&2
  exit 1
fi
echo "deployed to $HOST:$DEST (backup: /config/metra_backup_$stamp)"

if [[ "${1:-}" == "--reload" ]]; then
  : "${HASS_SERVER:?set HASS_SERVER}" "${HASS_TOKEN:?set HASS_TOKEN}"
  curl -sf -X POST -H "Authorization: Bearer $HASS_TOKEN" \
    "$HASS_SERVER/api/services/homeassistant/reload_custom_templates" >/dev/null
  echo "custom templates reloaded (the installed copy updates on the next restart)"
fi
