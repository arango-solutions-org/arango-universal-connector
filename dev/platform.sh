#!/usr/bin/env bash
# Local ArangoDB Platform (Enterprise) on kind + Podman, for developing and testing Links.
#
#   dev/platform.sh up            create the cluster and everything Links need (idempotent)
#   dev/platform.sh down          delete the cluster
#   dev/platform.sh status        show what's running
#   dev/platform.sh forward       port-forward ArangoDB to https://localhost:8529
#   dev/platform.sh load IMAGE    load a locally built image into the cluster
#
# Needs: podman (rootful machine, >= 8 GiB RAM), kind, kubectl, helm, and
# ARANGO_LICENSE_KEY="<client_id>:<client_secret>" (a dev/eval license).
#
# Installs what Links depend on: the kube-arangodb Enterprise operator (Link CRD, sidecar
# injection, gateway/routes), an ArangoDB single server with a gateway (job queue), and
# SeaweedFS behind an ArangoPlatformStorage (StorageV2, where link results go).
# PLATFORM_CHART=1 also installs the full platform chart (UI, AutoGraph, ...), which
# Links don't need.
set -euo pipefail

CLUSTER="${CLUSTER:-arango-platform}"
NS="${NS:-arango}"
NS_S3="seaweedfs"
OPERATOR_VERSION="${OPERATOR_VERSION:-1.4.5}"
ARANGODB_IMAGE="${ARANGODB_IMAGE:-arangodb/enterprise:3.12.11}"
SEAWEEDFS_IMAGE="chrislusf/seaweedfs:4.47"
KIND_NODE_IMAGE="kindest/node:v1.33.12@sha256:3f5c8443c620245e4d355cfe09e96a91ead32ceaa569d3f1ca9edf0cb2fe2ff4"
ROOT_PASSWORD="${ARANGO_ROOT_PASSWORD:-test}"
PLATFORM_CHART_URL="https://releases.license.arango.ai/releases/plg/arango-platform-release.tgz"

export KIND_EXPERIMENTAL_PROVIDER=podman

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

case "$(uname -m)" in
  arm64 | aarch64) ARCH=arm64 ;;
  x86_64 | amd64) ARCH=amd64 ;;
  *) die "unsupported architecture $(uname -m)" ;;
esac

require() {
  local missing=()
  for tool in podman kind kubectl helm; do
    command -v "$tool" >/dev/null || missing+=("$tool")
  done
  ((${#missing[@]} == 0)) || die "missing: ${missing[*]} (macOS: brew install ${missing[*]})"
  podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null | grep -q false ||
    die "kind needs a rootful podman machine: podman machine stop && podman machine set --rootful && podman machine start"
}

kube() { kubectl --context "kind-${CLUSTER}" "$@"; }

apply() { kube apply -f - >/dev/null; }

license_parts() {
  local key="${ARANGO_LICENSE_KEY:-}"
  [[ "$key" == *:* ]] || die "set ARANGO_LICENSE_KEY to '<client_id>:<client_secret>'"
  LICENSE_ID="${key%%:*}"
  LICENSE_SECRET="${key#*:}"
}

wait_for() { # wait_for <description> <seconds> <command...>
  local what="$1" timeout="$2" deadline
  shift 2
  deadline=$((SECONDS + timeout))
  until "$@" >/dev/null 2>&1; do
    ((SECONDS < deadline)) || die "timed out after ${timeout}s waiting for ${what}"
    sleep 5
  done
}

cluster_exists() { kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; }

# -- up -------------------------------------------------------------------------------

create_cluster() {
  if cluster_exists; then
    log "cluster ${CLUSTER} exists"
  else
    log "creating kind cluster ${CLUSTER} (podman)"
    kind create cluster --name "$CLUSTER" --image "$KIND_NODE_IMAGE" --wait 120s
  fi
  kube get namespace "$NS" >/dev/null 2>&1 || kube create namespace "$NS" >/dev/null
}

create_secrets() {
  license_parts
  log "license and root password secrets"
  kube -n "$NS" create secret generic arango-license-key \
    --from-literal=license-client-id="$LICENSE_ID" \
    --from-literal=license-client-secret="$LICENSE_SECRET" \
    --dry-run=client -o yaml | apply
  kube -n "$NS" create secret generic arango-root-password \
    --from-literal=username=root --from-literal=password="$ROOT_PASSWORD" \
    --dry-run=client -o yaml | apply
}

install_operator() {
  log "kube-arangodb enterprise operator ${OPERATOR_VERSION}"
  helm --kube-context "kind-${CLUSTER}" upgrade --install operator --namespace "$NS" \
    "https://github.com/arangodb/kube-arangodb/releases/download/${OPERATOR_VERSION}/kube-arangodb-enterprise-${OPERATOR_VERSION}.tgz" \
    --set webhooks.enabled=true \
    --set "operator.args[0]=--deployment.feature.gateway=true" \
    --set "operator.architectures={${ARCH}}" \
    --wait --timeout 5m >/dev/null
}

deploy_arangodb() {
  log "ArangoDB ${ARANGODB_IMAGE} (single server + gateway)"
  apply <<EOF
apiVersion: database.arangodb.com/v1
kind: ArangoDeployment
metadata:
  name: deployment
  namespace: ${NS}
spec:
  mode: Single
  image: ${ARANGODB_IMAGE}
  architecture: [${ARCH}]
  gateway:
    enabled: true
    dynamic: true
  gateways:
    count: 1
  license:
    secretName: arango-license-key
  bootstrap:
    passwordSecretNames:
      root: arango-root-password
EOF
  wait_for "ArangoDB pods" 600 pods_ready "$NS" arango_deployment=deployment 2
}

setup_storage() {
  log "SeaweedFS (S3) + ArangoPlatformStorage"
  kube get namespace "$NS_S3" >/dev/null 2>&1 || kube create namespace "$NS_S3" >/dev/null
  if ! kube -n "$NS_S3" get secret seaweedfs-config >/dev/null 2>&1; then
    local secret
    # Read a fixed number of bytes: an endless `tr </dev/urandom | head` dies of SIGPIPE
    # under pipefail.
    secret="$(head -c 24 /dev/urandom | base64 | tr -d '/+=')"
    kube -n "$NS_S3" create secret generic seaweedfs-config --from-literal=s3.json="$(
      printf '{"identities":[{"name":"admin","credentials":[{"accessKey":"seaweedfsadmin","secretKey":"%s"}],"actions":["Admin","Read","Write","List","Tagging"]}]}' "$secret"
    )" >/dev/null
    kube -n "$NS" create secret generic seaweedfs-credentials \
      --from-literal=accessKey=seaweedfsadmin --from-literal=secretKey="$secret" \
      --dry-run=client -o yaml | apply
  fi
  apply <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: seaweedfs-data, namespace: ${NS_S3}}
spec:
  accessModes: [ReadWriteOnce]
  resources: {requests: {storage: 5Gi}}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: seaweedfs, namespace: ${NS_S3}}
spec:
  replicas: 1
  selector: {matchLabels: {app: seaweedfs}}
  template:
    metadata: {labels: {app: seaweedfs}}
    spec:
      containers:
        - name: seaweedfs
          image: ${SEAWEEDFS_IMAGE}
          args: [server, -dir=/data, -ip.bind=0.0.0.0, -master.volumeSizeLimitMB=1024,
                 -volume.max=0, -s3, -s3.port=8333, -s3.config=/etc/seaweedfs/s3.json]
          ports: [{containerPort: 8333}]
          volumeMounts:
            - {name: data, mountPath: /data}
            - {name: s3-config, mountPath: /etc/seaweedfs, readOnly: true}
      volumes:
        - {name: data, persistentVolumeClaim: {claimName: seaweedfs-data}}
        - name: s3-config
          secret: {secretName: seaweedfs-config, items: [{key: s3.json, path: s3.json}]}
---
apiVersion: v1
kind: Service
metadata: {name: seaweedfs, namespace: ${NS_S3}}
spec:
  selector: {app: seaweedfs}
  ports: [{port: 8333, targetPort: 8333}]
---
apiVersion: platform.arangodb.com/v1beta1
kind: ArangoPlatformStorage
metadata: {name: deployment, namespace: ${NS}}
spec:
  backend:
    s3:
      bucketName: arango-platform-storage
      credentialsSecret: {name: seaweedfs-credentials}
      endpoint: http://seaweedfs.${NS_S3}.svc.cluster.local:8333
      allowInsecure: true
EOF
  kube -n "$NS_S3" rollout status deployment/seaweedfs --timeout=180s >/dev/null
  wait_for "ArangoPlatformStorage Ready" 120 condition_true arangoplatformstorage deployment Ready
}

install_platform_chart() {
  [[ "${PLATFORM_CHART:-0}" == 1 ]] || return 0
  log "platform chart (optional, large)"
  local chart
  chart="$(mktemp -t arango-platform).tgz"
  curl -fsSL --retry 3 "$PLATFORM_CHART_URL" -o "$chart" || die "could not download the platform chart"
  kube -n "$NS" create secret generic fps-recovery-credentials \
    --from-literal=fernet-key="$(head -c 32 /dev/urandom | base64 | tr '+/' '-_')" \
    --dry-run=client -o yaml | apply
  helm --kube-context "kind-${CLUSTER}" upgrade --install platform "$chart" --namespace "$NS" \
    --set deployment=deployment \
    --set charts.arangodb-autograph.overrides.fps_recovery_username=fps-recovery \
    --set charts.arangodb-autograph.overrides.file_parser.recovery_username=fps-recovery \
    --set charts.arangodb-autograph.overrides.file_parser.recovery_secret_name=fps-recovery-credentials \
    --timeout 15m >/dev/null
  rm -f "$chart"
}

pods_ready() { # pods_ready <namespace> <selector> <min_count>
  local ns="$1" selector="$2" min="$3" total ready
  total=$(kube -n "$ns" get pods -l "$selector" --no-headers 2>/dev/null | wc -l | tr -d ' ')
  ready=$(kube -n "$ns" get pods -l "$selector" --no-headers 2>/dev/null |
    awk '{split($2, r, "/"); if (r[1] == r[2] && $3 == "Running") n++} END {print n + 0}')
  ((total >= min && ready == total))
}

condition_true() { # condition_true <kind> <name> <condition>
  [[ "$(kube -n "$NS" get "$1" "$2" -o jsonpath="{.status.conditions[?(@.type==\"$3\")].status}")" == True ]]
}

up() {
  require
  license_parts
  local start=$SECONDS
  create_cluster
  create_secrets
  install_operator
  deploy_arangodb
  setup_storage
  install_platform_chart
  log "ready in $(((SECONDS - start) / 60))m$(((SECONDS - start) % 60))s"
  cat <<EOF

  cluster   kind-${CLUSTER}  (kubectl --context kind-${CLUSTER} -n ${NS} ...)
  ArangoDB  make platform-forward, then https://localhost:8529  (root / ${ROOT_PASSWORD})
  images    make platform-load IMAGE=<image>

EOF
}

# -- other commands -------------------------------------------------------------------

down() {
  if cluster_exists; then
    log "deleting cluster ${CLUSTER}"
    kind delete cluster --name "$CLUSTER"
  else
    log "no cluster ${CLUSTER}"
  fi
}

status() {
  cluster_exists || die "no cluster ${CLUSTER}; run: make platform-up"
  kube -n "$NS" get arangodeployment,arangoplatformstorage,arangoplatformlink,pods 2>/dev/null
  kube -n "$NS_S3" get pods 2>/dev/null
}

forward() {
  cluster_exists || die "no cluster ${CLUSTER}; run: make platform-up"
  log "https://localhost:8529 (root / ${ROOT_PASSWORD}); Ctrl+C to stop"
  kube -n "$NS" port-forward service/deployment-ea 8529:8529
}

load() {
  local image="${1:-}"
  [[ -n "$image" ]] || die "usage: $0 load <image>"
  cluster_exists || die "no cluster ${CLUSTER}; run: make platform-up"
  local archive
  archive="$(mktemp -t kind-image).tar"
  log "loading ${image} into ${CLUSTER}"
  podman save "$image" -o "$archive"
  kind load image-archive "$archive" --name "$CLUSTER"
  rm -f "$archive"
}

case "${1:-}" in
  up) up ;;
  down) down ;;
  status) status ;;
  forward) forward ;;
  load) load "${2:-}" ;;
  *) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
