#!/usr/bin/env bash
# dev-sync.sh — watches scripts/ and Dockerfile for changes, and on any change:
# rebuilds the image, loads it into minikube under a fresh tag, and updates the
# live Deployment (kubectl set image) so the running pod picks it up.
#
# Each build gets a unique tag (dev-<epoch>) instead of reusing one tag, because
# minikube's image cache won't reliably overwrite a tag that's still referenced
# by a running container — a fresh tag sidesteps that entirely.
#
# Note: this uses `kubectl set image`, which bypasses Helm. The Helm release's
# recorded values still say image.tag=local — a future `helm upgrade` without
# --reuse-values pointed at this tag would revert the running image. Re-run
# `helm upgrade --reuse-values --set image.tag=<last dev-tag>` if you want Helm's
# state to catch up, or just re-run this script's loop.
set -uo pipefail

NAMESPACE=pod-observer
DEPLOYMENT=pod-observer
CONTAINER=pod-observer
IMAGE=k8s-pod-observer
WATCH_PATHS=(scripts Dockerfile)
POLL_INTERVAL=2
KEEP_IMAGES=3   # how many recent dev-tag images to keep cached in minikube

cd "$(dirname "${BASH_SOURCE[0]}")"

hash_watched() {
    find "${WATCH_PATHS[@]}" -type f -print0 2>/dev/null \
        | sort -z \
        | xargs -0 shasum -a 256 2>/dev/null \
        | shasum -a 256 \
        | awk '{print $1}'
}

prune_old_images() {
    local tags
    tags=$(minikube image ls --format table 2>/dev/null \
        | awk -v img="$IMAGE" '$2==img && $3 ~ /^dev-/ {print $3}' \
        | sort -t- -k2 -n)
    local count
    count=$(echo "$tags" | wc -l | tr -d ' ')
    if [[ "$count" -gt "$KEEP_IMAGES" ]]; then
        echo "$tags" | head -n "$((count - KEEP_IMAGES))" | while read -r old_tag; do
            [[ -z "$old_tag" ]] && continue
            echo "$(date '+%F %T') pruning old image $IMAGE:$old_tag"
            minikube image rm "$IMAGE:$old_tag" >/dev/null 2>&1
        done
    fi
}

last_hash=$(hash_watched)
echo "$(date '+%F %T') dev-sync started — watching: ${WATCH_PATHS[*]} (poll every ${POLL_INTERVAL}s)"

while true; do
    sleep "$POLL_INTERVAL"
    current_hash=$(hash_watched)
    if [[ "$current_hash" == "$last_hash" ]]; then
        continue
    fi
    last_hash="$current_hash"

    tag="dev-$(date +%s)"
    echo "$(date '+%F %T') change detected -> building $IMAGE:$tag"

    if ! docker buildx build --builder desktop-linux --platform linux/arm64 \
            -t "$IMAGE:$tag" --load . ; then
        echo "$(date '+%F %T') BUILD FAILED — will retry on next change"
        continue
    fi

    echo "$(date '+%F %T') loading $IMAGE:$tag into minikube"
    if ! minikube image load "$IMAGE:$tag"; then
        echo "$(date '+%F %T') minikube image load FAILED — will retry on next change"
        continue
    fi

    echo "$(date '+%F %T') updating deployment/$DEPLOYMENT to $tag"
    if ! kubectl set image "deployment/$DEPLOYMENT" "$CONTAINER=$IMAGE:$tag" -n "$NAMESPACE"; then
        echo "$(date '+%F %T') kubectl set image FAILED — will retry on next change"
        continue
    fi

    if kubectl rollout status "deployment/$DEPLOYMENT" -n "$NAMESPACE" --timeout=120s; then
        echo "$(date '+%F %T') rollout complete: $tag"
        prune_old_images
    else
        echo "$(date '+%F %T') rollout did NOT complete cleanly for $tag — check: kubectl get pods -n $NAMESPACE"
    fi
done
