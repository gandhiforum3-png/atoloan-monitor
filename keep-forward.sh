#!/usr/bin/env bash
# keep-forward.sh — kubectl port-forward dies whenever the pod it's attached to
# is replaced (e.g. by dev-sync.sh's rollouts, or manual scaling). This wraps
# it in a restart loop so the dashboard stays reachable at localhost:8080
# across pod restarts, without needing to notice and manually re-run it.
set -uo pipefail

NAMESPACE=pod-observer
SERVICE=pod-observer
LOCAL_PORT=8080
REMOTE_PORT=80

echo "$(date '+%F %T') keep-forward started — localhost:$LOCAL_PORT -> svc/$SERVICE:$REMOTE_PORT"

while true; do
    kubectl port-forward -n "$NAMESPACE" "svc/$SERVICE" "$LOCAL_PORT:$REMOTE_PORT"
    echo "$(date '+%F %T') port-forward exited (pod likely replaced) — restarting in 2s"
    sleep 2
done
