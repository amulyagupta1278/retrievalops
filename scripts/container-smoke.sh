#!/bin/sh
set -eu

image="${1:?usage: container-smoke.sh IMAGE}"
name="retrievalops-smoke-$$"
runtime="${CONTAINER_RUNTIME:-docker}"
cleanup() {
  "$runtime" rm --force "$name" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

"$runtime" run --detach --name "$name" --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --tmpfs /data:rw,nosuid,uid=10001,gid=10001,size=256m \
  -p 18000:8000 "$image" >/dev/null

attempt=0
until curl --fail --silent http://127.0.0.1:18000/healthz >/dev/null; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 30 ]; then
    "$runtime" logs "$name"
    exit 1
  fi
  sleep 1
done

test "$("$runtime" inspect --format '{{.Config.User}}' "$name")" = "10001:10001"
curl --fail --silent http://127.0.0.1:18000/healthz
