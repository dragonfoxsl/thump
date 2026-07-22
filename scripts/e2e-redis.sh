#!/usr/bin/env bash
# End-to-end against real containers: two thump replicas + a persistent Redis +
# a probe target. Exercises readiness, heartbeats, probes, the single-prober
# lease, and leader failover — the parts unit tests and fakeredis can't prove.
#
#   docker build -t thump:e2e .
#   THUMP_IMAGE=thump:e2e scripts/e2e-redis.sh
set -euo pipefail

IMAGE="${THUMP_IMAGE:-thump:e2e}"
NET=thump-e2e
SECRET=e2e-secret
CFG="$(mktemp)"

cleanup() {
  docker rm -f thump-a thump-b thump-redis thump-target >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  rm -f "$CFG"
}
trap cleanup EXIT

cat > "$CFG" <<'YAML'
store: {driver: redis, dsn: redis://thump-redis:6379/0}
server: {listen: ":8080", secret: ${THUMP_SECRET}, timezone: UTC}
defaults: {grace: 5m, timeout: 3s, history: 50, failure_threshold: 2}
checks:
  - name: nightly-backup
    type: heartbeat
    interval: 24h
    grace: 1h
  - name: internal-target
    type: probe
    url: http://thump-target/
    interval: 2s
    expect_status: 200
YAML
chmod 644 "$CFG"  # thump runs as non-root (uid 10001); make the mount readable

docker network create "$NET" >/dev/null

docker run -d --name thump-redis --network "$NET" redis:7-alpine \
  redis-server --appendonly yes --maxmemory-policy noeviction >/dev/null
docker run -d --name thump-target --network "$NET" nginx:alpine >/dev/null

run_thump() {  # name port holder
  docker run -d --name "$1" --network "$NET" -p "$2:8080" \
    -e THUMP_SECRET="$SECRET" -e THUMP_HOLDER="$3" -e THUMP_LEASE_TTL=4 \
    -v "$CFG:/etc/thump/config.yaml:ro" "$IMAGE" >/dev/null
}
run_thump thump-a 8091 thump-a
run_thump thump-b 8092 thump-b

poll() {  # url expected [timeout]
  local url="$1" want="$2" t="${3:-30}" got=
  for _ in $(seq 1 "$t"); do
    got=$(curl -s -o /dev/null -w '%{http_code}' "$url" || true)
    [ "$got" = "$want" ] && { echo "  ok   $url -> $got"; return 0; }
    sleep 1
  done
  echo "  FAIL $url: wanted $want, last got '$got'"; docker logs thump-a; exit 1
}
rget() { docker exec thump-redis redis-cli "$@"; }

echo "1) both replicas become ready"
poll http://localhost:8091/readyz 200
poll http://localhost:8092/readyz 200

echo "2) heartbeat: pending -> up -> down (state shared across replicas)"
poll http://localhost:8091/status/nightly-backup 200 5
TOKEN=$(docker exec thump-a python -c \
  "from thump.tokens import derive_token; print(derive_token('$SECRET','nightly-backup'))")
curl -fsS "http://localhost:8091/ping/$TOKEN" >/dev/null           # ping replica A
poll http://localhost:8091/status/nightly-backup 200 5
curl -fsS "http://localhost:8092/ping/$TOKEN/fail" >/dev/null      # fail via replica B
poll http://localhost:8091/status/nightly-backup 503 5             # A sees it -> shared

echo "3) probe reports up while the target is healthy"
poll http://localhost:8091/status/internal-target 200 15

echo "4) exactly one replica holds the probe lease"
sleep 3
LEADER=$(rget get thump:probe-leader)
echo "  leader = $LEADER"
[ "$LEADER" = "thump-a" ] || [ "$LEADER" = "thump-b" ] || { echo "  no single leader"; exit 1; }

echo "5) probe goes down when the target dies, recovers when it returns"
docker stop thump-target >/dev/null
poll http://localhost:8091/status/internal-target 503 20
docker start thump-target >/dev/null
poll http://localhost:8091/status/internal-target 200 20

echo "6) leader failover: kill the leader, the survivor takes the lease over"
SURVIVOR=$([ "$LEADER" = "thump-a" ] && echo thump-b || echo thump-a)
SPORT=$([ "$SURVIVOR" = "thump-a" ] && echo 8091 || echo 8092)
docker stop "$LEADER" >/dev/null
NOW=
for _ in $(seq 1 20); do
  NOW=$(rget get thump:probe-leader)
  [ "$NOW" = "$SURVIVOR" ] && break
  sleep 1
done
echo "  new leader = $NOW"
[ "$NOW" = "$SURVIVOR" ] || { echo "  failover did not transfer to $SURVIVOR"; exit 1; }
poll "http://localhost:$SPORT/status/internal-target" 200 15  # survivor keeps probing

echo
echo "ALL E2E CHECKS PASSED"
