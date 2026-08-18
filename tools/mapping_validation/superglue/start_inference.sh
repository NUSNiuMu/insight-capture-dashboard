#!/usr/bin/env bash

set -euo pipefail

ENGINE_DIR="${SUPERGLUE_ENGINE_DIR:-/opt/insight/engines}"
PREPARE=(python3 /opt/insight/prepare_superglue_tensorrt.py --engine-dir "${ENGINE_DIR}")

mkdir -p "${ENGINE_DIR}"
if ! "${PREPARE[@]}" check; then
    "${PREPARE[@]}" stage

    /opt/tensorrt/bin/trtexec \
        --onnx="${ENGINE_DIR}/superpoint.onnx" \
        --saveEngine="${ENGINE_DIR}/superpoint_fp16.plan.building" \
        --fp16 \
        --skipInference \
        --memPoolSize=workspace:64 \
        --builderOptimizationLevel=0 \
        --maxAuxStreams=0 \
        --avgTiming=1 \
        --noCompilationCache
    mv "${ENGINE_DIR}/superpoint_fp16.plan.building" \
        "${ENGINE_DIR}/superpoint_fp16.plan"

    /opt/tensorrt/bin/trtexec \
        --onnx="${ENGINE_DIR}/superglue.onnx" \
        --saveEngine="${ENGINE_DIR}/superglue_fp32.plan.building" \
        --skipInference \
        --memPoolSize=workspace:64 \
        --builderOptimizationLevel=0 \
        --maxAuxStreams=0 \
        --avgTiming=1 \
        --noCompilationCache \
        --minShapes=keypoints0:1x1x2,keypoints1:1x1x2,scores0:1x1,scores1:1x1,descriptors0:1x256x1,descriptors1:1x256x1 \
        --optShapes=keypoints0:1x256x2,keypoints1:1x256x2,scores0:1x256,scores1:1x256,descriptors0:1x256x256,descriptors1:1x256x256 \
        --maxShapes=keypoints0:1x1024x2,keypoints1:1x1024x2,scores0:1x1024,scores1:1x1024,descriptors0:1x256x1024,descriptors1:1x256x1024
    mv "${ENGINE_DIR}/superglue_fp32.plan.building" \
        "${ENGINE_DIR}/superglue_fp32.plan"

    "${PREPARE[@]}" finalize
fi

exec python3 -u /opt/insight/superglue_inference_worker.py \
    --engine-dir "${ENGINE_DIR}"
