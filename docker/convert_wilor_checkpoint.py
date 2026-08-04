#!/usr/bin/env python3
"""Convert floating-point WiLoR inference weights to FP16 in place."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import torch


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkpoint", type=Path)
    args = parser.parse_args()

    checkpoint_path = args.checkpoint.resolve()
    payload = torch.load(
        checkpoint_path,
        map_location="cpu",
        mmap=True,
        weights_only=True,
    )
    state_dict = payload.get("state_dict")
    if not isinstance(state_dict, dict) or not state_dict:
        raise RuntimeError("WiLoR checkpoint has no non-empty state_dict")

    converted = {}
    source_tensor_bytes = 0
    target_tensor_bytes = 0
    converted_tensors = 0
    for name, value in state_dict.items():
        if not torch.is_tensor(value):
            raise TypeError(f"state_dict entry {name!r} is not a tensor")
        source_tensor_bytes += value.numel() * value.element_size()
        if value.is_floating_point() and value.dtype != torch.float16:
            value = value.to(dtype=torch.float16)
            converted_tensors += 1
        converted[name] = value
        target_tensor_bytes += value.numel() * value.element_size()

    if converted_tensors == 0 or target_tensor_bytes >= source_tensor_bytes:
        raise RuntimeError("WiLoR checkpoint conversion did not reduce tensor size")

    output_payload = dict(payload)
    output_payload["state_dict"] = converted
    temporary_path = checkpoint_path.with_suffix(checkpoint_path.suffix + ".fp16")
    torch.save(output_payload, temporary_path)

    verified = torch.load(
        temporary_path,
        map_location="cpu",
        mmap=True,
        weights_only=True,
    )
    verified_state = verified.get("state_dict", {})
    if list(verified_state) != list(state_dict):
        raise RuntimeError("Converted WiLoR checkpoint changed state_dict keys")
    for name, source in state_dict.items():
        target = verified_state[name]
        if source.shape != target.shape:
            raise RuntimeError(f"Converted tensor shape changed for {name!r}")
        expected_dtype = torch.float16 if source.is_floating_point() else source.dtype
        if target.dtype != expected_dtype:
            raise RuntimeError(f"Unexpected converted dtype for {name!r}: {target.dtype}")
        expected = source.to(dtype=expected_dtype)
        if not torch.equal(target, expected):
            raise RuntimeError(f"Converted tensor values changed for {name!r}")

    os.replace(temporary_path, checkpoint_path)
    print(
        "Converted WiLoR checkpoint: "
        f"{converted_tensors} tensors, "
        f"{source_tensor_bytes} -> {target_tensor_bytes} tensor bytes"
    )


if __name__ == "__main__":
    main()
