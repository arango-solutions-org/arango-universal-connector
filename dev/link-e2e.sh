#!/usr/bin/env bash
# End-to-end check of a link on the local platform (make platform-up first):
# build the links image, load it into kind, install the chart, submit a job through the
# gateway as an AI tool would, wait for it to complete, and verify the JSONL result in
# StorageV2.
#
#   dev/link-e2e.sh            the selftest link (no credentials needed)
#
# Exits non-zero on any failure. Leaves the release installed for inspection;
# `helm --kube-context kind-arango-platform -n arango uninstall selftest` removes it.
set -euo pipefail

CLUSTER="${CLUSTER:-arango-platform}"
NS="${NS:-arango}"
RELEASE="selftest"
IMAGE="localhost/arango-connector-link:dev"
PORT=18531
CTX="kind-${CLUSTER}"
ROOT_PASSWORD="${ARANGO_ROOT_PASSWORD:-test}"
cd "$(dirname "$0")/.."

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }
kube() { kubectl --context "$CTX" -n "$NS" "$@"; }
api() { curl -ksS -u "root:${ROOT_PASSWORD}" "$@"; }

kind get clusters 2>/dev/null | grep -qx "$CLUSTER" || die "no cluster; run: make platform-up"

log "building ${IMAGE}"
podman build -q -f packages/links/Dockerfile -t "$IMAGE" . >/dev/null
dev/platform.sh load "$IMAGE" >/dev/null

log "installing release ${RELEASE}"
helm --kube-context "$CTX" upgrade --install "$RELEASE" charts/arango-connector-link -n "$NS" \
  --set link=selftest \
  --set arangodb_platform.deployment.name=deployment \
  --set image.repository="${IMAGE%:*}" --set image.tag="${IMAGE##*:}" --set image.pullPolicy=Never \
  >/dev/null
kube rollout restart deployment/"$RELEASE" >/dev/null
kube rollout status deployment/"$RELEASE" --timeout=180s >/dev/null

kube port-forward service/deployment-ea "${PORT}:8529" >/dev/null 2>&1 &
forward=$!
trap 'kill $forward 2>/dev/null || true' EXIT
base="https://localhost:${PORT}/link/${RELEASE}"

log "waiting for the gateway to serve /link/${RELEASE}/ (route config can take a minute)"
deadline=$((SECONDS + 300))
until [[ "$(api -o /dev/null -w '%{http_code}' "${base}/job" 2>/dev/null || true)" == 200 ]]; do
  ((SECONDS < deadline)) || die "route never became reachable"
  sleep 5
done

log "submitting a job"
input=$(printf '{"operation":"echo","text":"e2e","count":3}' | base64 | tr -d '\n')
job=$(api -X POST "${base}/job" -H 'Content-Type: application/json' \
  -d "{\"input\":\"${input}\",\"timeout\":\"60s\"}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

deadline=$((SECONDS + 120))
while :; do
  state=$(api "${base}/job/${job}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["statuses"][0]["state"])')
  [[ "$state" == JOB_STATE_COMPLETED || "$state" == JOB_STATE_FAILED ]] && break
  ((SECONDS < deadline)) || die "job ${job} stuck in ${state}"
  sleep 2
done
[[ "$state" == JOB_STATE_COMPLETED ]] || die "job ${job} ended ${state}"

log "verifying the result in StorageV2"
result=$(api "${base}/job/${job}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"])')
s3pod=$(kubectl --context "$CTX" -n seaweedfs get pod -l app=seaweedfs -o jsonpath='{.items[0].metadata.name}')
records=$(kubectl --context "$CTX" -n seaweedfs exec "$s3pod" -- \
  wget -qO- "http://localhost:8888/buckets/arango-platform-storage${result}result.0000000.jsonl" |
  python3 -c 'import json,sys; rows=[json.loads(l) for l in sys.stdin]; assert all(r["payload"]["text"]=="e2e" for r in rows); print(len(rows))')
[[ "$records" == 3 ]] || die "expected 3 records, found ${records}"

log "passed: job ${job} completed with ${records} records at ${result}"
