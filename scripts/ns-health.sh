#!/usr/bin/env bash
# ns-health.sh — Namespace-wide health sweep
# Usage: ./ns-health.sh <namespace>   (omit for all namespaces)

set -euo pipefail

NS="${1:-}"
NS_FLAG="${NS:+-n $NS}"
NS_LABEL="${NS:-all namespaces}"

echo "========================================"
echo "NAMESPACE HEALTH SWEEP: $NS_LABEL"
echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "========================================"

echo ""
echo "--- NON-RUNNING PODS ---"
if [ -n "$NS" ]; then
  kubectl get pods -n "$NS" | grep -vE '\bRunning\b|\bCompleted\b|^NAME' || echo "(all pods running)"
else
  kubectl get pods -A | grep -vE '\bRunning\b|\bCompleted\b|^NAMESPACE' || echo "(all pods running)"
fi

echo ""
echo "--- HIGH RESTART COUNTS (top 10) ---"
if [ -n "$NS" ]; then
  kubectl get pods -n "$NS" \
    --sort-by='.status.containerStatuses[0].restartCount' \
    | tail -10
else
  kubectl get pods -A \
    --sort-by='.status.containerStatuses[0].restartCount' \
    | tail -10
fi

echo ""
echo "--- WARNING EVENTS (newest first) ---"
if [ -n "$NS" ]; then
  kubectl get events -n "$NS" \
    --sort-by='.lastTimestamp' \
    | grep -i warning | tail -20 || echo "(no warning events)"
else
  kubectl get events -A \
    --sort-by='.lastTimestamp' \
    | grep -i warning | tail -20 || echo "(no warning events)"
fi

echo ""
echo "--- RESOURCE USAGE (top memory consumers) ---"
if [ -n "$NS" ]; then
  kubectl top pods -n "$NS" --sort-by=memory 2>/dev/null | head -15 || echo "(metrics-server not available)"
else
  kubectl top pods -A --sort-by=memory 2>/dev/null | head -15 || echo "(metrics-server not available)"
fi

echo ""
echo "--- NODE CONDITIONS ---"
kubectl describe nodes | grep -A5 "Conditions:" | grep -E "MemoryPressure|DiskPressure|PIDPressure|Ready" | head -20

echo ""
echo "--- COREDNS HEALTH ---"
kubectl get pods -n kube-system -l k8s-app=kube-dns -o wide 2>/dev/null || echo "(kube-system not accessible)"

echo ""
echo "--- PVC STATUS ---"
if [ -n "$NS" ]; then
  kubectl get pvc -n "$NS" 2>/dev/null || echo "(no PVCs)"
else
  kubectl get pvc -A 2>/dev/null || echo "(no PVCs)"
fi
