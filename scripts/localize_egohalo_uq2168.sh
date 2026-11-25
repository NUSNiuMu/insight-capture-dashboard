#!/usr/bin/env bash
set -euo pipefail

if (( $# < 4 || $# > 6 )); then
    echo "Usage: $0 TAKE NEW_REPORT.json LEFT_CALIBRATION RIGHT_CALIBRATION [LEFT_ROTATION [RIGHT_ROTATION]]" >&2
    exit 2
fi

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
take=$(realpath -e -- "$1")
report=$2
left=$(realpath -e -- "$3")
right=$(realpath -e -- "$4")
left_rotation=${5:-0}
right_rotation=${6:-0}

[[ -f "$take/time_alignment.json" && -f "$take/metadata.yaml" ]] || {
    echo "Expected a merged EgoHalo Take: $take" >&2
    exit 2
}
[[ ! -e "$report" && -d "$(dirname -- "$report")" ]] || {
    echo "Report needs an existing parent directory and a new filename: $report" >&2
    exit 2
}
[[ "$left_rotation" =~ ^(0|180)$ && "$right_rotation" =~ ^(0|180)$ ]] || {
    echo "Each rotation must be 0 or 180 degrees" >&2
    exit 2
}

for service in insight-dashboard insight9-sparse-mapper superglue-inference; do
    mapfile -t ids < <(docker ps -q --filter "label=com.docker.compose.service=$service")
    if (( ${#ids[@]} != 1 )); then
        echo "Expected exactly one running $service service" >&2
        exit 1
    fi
    case "$service" in
        insight-dashboard) dashboard_image=$(docker inspect --format '{{.Image}}' "${ids[0]}") ;;
        insight9-sparse-mapper)
            mapper_id=${ids[0]}
            mapper_image=$(docker inspect --format '{{.Image}}' "$mapper_id")
            ;;
        superglue-inference)
            matcher_volume=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/run/superglue"}}{{.Name}}{{end}}{{end}}' "${ids[0]}")
            ;;
    esac
done
docker volume inspect "$matcher_volume" >/dev/null

trial=$(mktemp -d)
container=
cleanup() {
    if [[ -n "$container" ]]; then docker stop --time 2 "$container" >/dev/null || true; fi
    rm -f -- "$trial/map.sock" "$trial/report.json" "$trial/insight_mapping"
    rmdir -- "$trial"
}
trap cleanup EXIT

# The active service may run an executable mounted over the image version.
docker cp "$mapper_id:/usr/local/bin/insight_mapping" "$trial/insight_mapping"
container=$(docker run --rm -d --pull never --network none \
    -v "$trial:/run/trial" -v "$matcher_volume:/run/superglue:ro" \
    -v "$trial/insight_mapping:/usr/local/bin/insight_mapping:ro" \
    "$mapper_image" /usr/local/bin/insight_mapping /run/trial/map.sock)
for ((i = 0; i < 100; i++)); do
    [[ ! -S "$trial/map.sock" ]] || break
    sleep .1
done
[[ -S "$trial/map.sock" ]] || { echo "Private mapper did not start" >&2; exit 1; }

status=0
docker run --rm --pull never --network none --runtime nvidia \
    -e NVIDIA_VISIBLE_DEVICES=all -e NVIDIA_DRIVER_CAPABILITIES=all \
    -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/source -e OPENBLAS_NUM_THREADS=1 \
    -v "$root:/source:ro" -v "$take:/take:ro" -v "$trial:/run/trial" \
    -v "$matcher_volume:/run/superglue:ro" -v "$left:/left.json:ro" -v "$right:/right.json:ro" \
    --workdir /source --entrypoint /bin/bash "$dashboard_image" \
    -c 'source /opt/ros/humble/setup.bash && exec python3 -m insight_capture.postprocess.bags.egohalo_uq2168 "$@"' \
    trial /take --mapper /run/trial/map.sock --matcher /run/superglue/matcher.sock \
    --left-calibration /left.json --right-calibration /right.json \
    --left-rotation "$left_rotation" --right-rotation "$right_rotation" --output /run/trial/report.json \
    || status=$?
if [[ -f "$trial/report.json" ]]; then
    (set -C; cat "$trial/report.json" > "$report")
    printf 'Report %s\n' "$report"
fi
exit "$status"
