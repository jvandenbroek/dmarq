#!/bin/sh
# Regenerate backend/requirements.lock: resolve requirements.txt in the same
# base image the backend Dockerfile uses, then freeze the result.
set -eu
cd "$(dirname "$0")/../backend"
base=$(sed -n 's/^FROM \(python:[^ ]*\).*/\1/p' Dockerfile | head -1)
docker run --rm -v "$PWD/requirements.txt:/r.txt:ro" "$base" sh -c \
  'pip install -q --no-cache-dir --root-user-action=ignore -r /r.txt && pip freeze --exclude-editable' \
  2>/dev/null | grep -v '^Emulate' > requirements.lock.tmp
{ echo "# Generated from a built image: scripts/lock-requirements.sh. Do not edit by hand."; cat requirements.lock.tmp; } > requirements.lock
rm requirements.lock.tmp
echo "requirements.lock: $(grep -vc '^#' requirements.lock) packages"
