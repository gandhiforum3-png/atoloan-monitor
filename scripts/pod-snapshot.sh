#!/usr/bin/env bash
# pod-snapshot.sh — Full pod state snapshot in one pass
# Usage: ./pod-snapshot.sh <pod-name> <namespace>

set -euo pipefail

POD="${1:?Usage: pod-snapshot.sh <pod-name> <namespace>}"
NS="${2:-default}"

echo "========================================"
echo "POD SNAPSHOT: $POD / $NS"
echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "========================================"

echo ""
echo "--- STATUS ---"
kubectl get pod "$POD" -n "$NS" -o wide

echo ""
echo "--- CONDITIONS ---"
kubectl get pod "$POD" -n "$NS" \
  -o jsonpath='{range .status.conditions[*]}{.type}: {.status} ({.reason}){"\n"}{end}'

echo ""
echo "--- CONTAINER STATUSES ---"
kubectl get pod "$POD" -n "$NS" \
  -o jsonpath='{range .status.containerStatuses[*]}Container: {.name}{"\n"}  Ready: {.ready}{"\n"}  Restarts: {.restartCount}{"\n"}  State: {.state}{"\n"}  Last State: {.lastState}{"\n"}{end}'

echo ""
echo "--- EVENTS (newest first) ---"
kubectl get events -n "$NS" \
  --field-selector "involvedObject.name=$POD" \
  --sort-by='.lastTimestamp' 2>/dev/null | tail -20 || echo "(no events found)"

echo ""
echo "--- CURRENT LOGS (last 100 lines) ---"
kubectl logs "$POD" -n "$NS" --tail=100 2>/dev/null || echo "(no current logs)"

echo ""
echo "--- PREVIOUS LOGS (last 100 lines) ---"
kubectl logs "$POD" -n "$NS" --previous --tail=100 2>/dev/null || echo "(no previous container logs)"

echo ""
echo "--- RESOURCE USAGE ---"
kubectl top pod "$POD" -n "$NS" --containers 2>/dev/null || echo "(metrics-server not available)"

echo ""
echo "--- DESCRIBE (full) ---"
kubectl describe pod "$POD" -n "$NS"
