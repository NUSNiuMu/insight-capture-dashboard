"""Pure TensorRT/CUDA runtime for the pinned feature models."""

from __future__ import annotations

import ctypes
import time
from pathlib import Path
from typing import Any

import numpy as np
import tensorrt as trt


IMAGE_HEIGHT = 640
IMAGE_WIDTH = 544
DESCRIPTOR_DIM = 256
OFFICIAL_COMMIT = "ddcf11f42e7e0732a0c4607648f9448ea8d73590"


class CudaError(RuntimeError):
    """Report a CUDA driver or runtime API failure."""


class CudaRuntime:
    """Manage one CUDA stream and raw device allocations through ctypes."""

    HOST_TO_DEVICE = 1
    DEVICE_TO_HOST = 2

    def __init__(self) -> None:
        self._runtime = ctypes.CDLL("libcudart.so.12")
        self._driver = ctypes.CDLL("libcuda.so.1")
        self._bind_runtime()
        self._bind_driver()
        self._check_driver(self._driver.cuInit(0), "cuInit")
        self.device = ctypes.c_int()
        self._check_driver(
            self._driver.cuDeviceGet(ctypes.byref(self.device), 0), "cuDeviceGet"
        )
        self.stream = ctypes.c_void_p()
        self._check(
            self._runtime.cudaStreamCreate(ctypes.byref(self.stream)),
            "cudaStreamCreate",
        )

    def _bind_runtime(self) -> None:
        self._runtime.cudaGetErrorString.argtypes = [ctypes.c_int]
        self._runtime.cudaGetErrorString.restype = ctypes.c_char_p
        self._runtime.cudaMalloc.argtypes = [
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_size_t,
        ]
        self._runtime.cudaMalloc.restype = ctypes.c_int
        self._runtime.cudaFree.argtypes = [ctypes.c_void_p]
        self._runtime.cudaFree.restype = ctypes.c_int
        self._runtime.cudaMemcpyAsync.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_int,
            ctypes.c_void_p,
        ]
        self._runtime.cudaMemcpyAsync.restype = ctypes.c_int
        self._runtime.cudaStreamCreate.argtypes = [
            ctypes.POINTER(ctypes.c_void_p)
        ]
        self._runtime.cudaStreamCreate.restype = ctypes.c_int
        self._runtime.cudaStreamSynchronize.argtypes = [ctypes.c_void_p]
        self._runtime.cudaStreamSynchronize.restype = ctypes.c_int
        self._runtime.cudaStreamDestroy.argtypes = [ctypes.c_void_p]
        self._runtime.cudaStreamDestroy.restype = ctypes.c_int
        self._runtime.cudaRuntimeGetVersion.argtypes = [
            ctypes.POINTER(ctypes.c_int)
        ]
        self._runtime.cudaRuntimeGetVersion.restype = ctypes.c_int

    def _bind_driver(self) -> None:
        self._driver.cuInit.argtypes = [ctypes.c_uint]
        self._driver.cuInit.restype = ctypes.c_int
        self._driver.cuDeviceGet.argtypes = [
            ctypes.POINTER(ctypes.c_int),
            ctypes.c_int,
        ]
        self._driver.cuDeviceGet.restype = ctypes.c_int
        self._driver.cuDeviceGetName.argtypes = [
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_int,
        ]
        self._driver.cuDeviceGetName.restype = ctypes.c_int
        self._driver.cuDeviceComputeCapability.argtypes = [
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.c_int,
        ]
        self._driver.cuDeviceComputeCapability.restype = ctypes.c_int
        self._driver.cuGetErrorString.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_char_p),
        ]
        self._driver.cuGetErrorString.restype = ctypes.c_int

    def _check(self, result: int, operation: str) -> None:
        if result == 0:
            return
        detail = self._runtime.cudaGetErrorString(result)
        message = detail.decode() if detail else f"CUDA error {result}"
        raise CudaError(f"{operation} failed: {message}")

    def _check_driver(self, result: int, operation: str) -> None:
        if result == 0:
            return
        detail = ctypes.c_char_p()
        self._driver.cuGetErrorString(result, ctypes.byref(detail))
        message = detail.value.decode() if detail.value else f"CUDA error {result}"
        raise CudaError(f"{operation} failed: {message}")

    @property
    def runtime_version(self) -> str:
        version = ctypes.c_int()
        self._check(
            self._runtime.cudaRuntimeGetVersion(ctypes.byref(version)),
            "cudaRuntimeGetVersion",
        )
        major = version.value // 1000
        minor = (version.value % 1000) // 10
        return f"{major}.{minor}"

    @property
    def device_name(self) -> str:
        name = ctypes.create_string_buffer(256)
        self._check_driver(
            self._driver.cuDeviceGetName(name, len(name), self.device.value),
            "cuDeviceGetName",
        )
        return name.value.decode()

    @property
    def compute_capability(self) -> tuple[int, int]:
        major = ctypes.c_int()
        minor = ctypes.c_int()
        self._check_driver(
            self._driver.cuDeviceComputeCapability(
                ctypes.byref(major), ctypes.byref(minor), self.device.value
            ),
            "cuDeviceComputeCapability",
        )
        return major.value, minor.value

    def allocate(self, size: int) -> ctypes.c_void_p:
        pointer = ctypes.c_void_p()
        self._check(
            self._runtime.cudaMalloc(ctypes.byref(pointer), int(size)),
            "cudaMalloc",
        )
        return pointer

    def free(self, pointer: ctypes.c_void_p) -> None:
        if pointer.value:
            self._check(self._runtime.cudaFree(pointer), "cudaFree")

    def copy_to_device(
        self, destination: ctypes.c_void_p, source: np.ndarray
    ) -> None:
        self._check(
            self._runtime.cudaMemcpyAsync(
                destination,
                ctypes.c_void_p(source.ctypes.data),
                source.nbytes,
                self.HOST_TO_DEVICE,
                self.stream,
            ),
            "cudaMemcpyAsync(H2D)",
        )

    def copy_to_host(
        self, destination: np.ndarray, source: ctypes.c_void_p
    ) -> None:
        self._check(
            self._runtime.cudaMemcpyAsync(
                ctypes.c_void_p(destination.ctypes.data),
                source,
                destination.nbytes,
                self.DEVICE_TO_HOST,
                self.stream,
            ),
            "cudaMemcpyAsync(D2H)",
        )

    def synchronize(self) -> None:
        self._check(
            self._runtime.cudaStreamSynchronize(self.stream),
            "cudaStreamSynchronize",
        )

    def close(self) -> None:
        if self.stream.value:
            self._check(
                self._runtime.cudaStreamDestroy(self.stream),
                "cudaStreamDestroy",
            )
            self.stream = ctypes.c_void_p()


class DeviceBuffer:
    """Grow-only allocation reused across inference requests."""

    def __init__(self, cuda: CudaRuntime) -> None:
        self._cuda = cuda
        self.pointer = ctypes.c_void_p()
        self.capacity = 0

    def reserve(self, size: int) -> ctypes.c_void_p:
        if size > self.capacity:
            self.close()
            self.pointer = self._cuda.allocate(size)
            self.capacity = size
        return self.pointer

    def close(self) -> None:
        if self.pointer.value:
            self._cuda.free(self.pointer)
            self.pointer = ctypes.c_void_p()
            self.capacity = 0


def _numpy_dtype(dtype: trt.DataType) -> np.dtype:
    return np.dtype(trt.nptype(dtype))


class TensorRTEngine:
    """Execute a TensorRT 10 engine using raw CUDA buffers."""

    def __init__(self, cuda: CudaRuntime, plan_path: Path) -> None:
        self._cuda = cuda
        self._logger = trt.Logger(trt.Logger.WARNING)
        self._runtime = trt.Runtime(self._logger)
        self._engine = self._runtime.deserialize_cuda_engine(plan_path.read_bytes())
        if self._engine is None:
            raise RuntimeError(f"could not deserialize TensorRT engine {plan_path}")
        self._context = self._engine.create_execution_context()
        if self._context is None:
            raise RuntimeError(f"could not create TensorRT context for {plan_path}")
        self.input_names = tuple(
            self._engine.get_tensor_name(index)
            for index in range(self._engine.num_io_tensors)
            if self._engine.get_tensor_mode(self._engine.get_tensor_name(index))
            == trt.TensorIOMode.INPUT
        )
        self.output_names = tuple(
            self._engine.get_tensor_name(index)
            for index in range(self._engine.num_io_tensors)
            if self._engine.get_tensor_mode(self._engine.get_tensor_name(index))
            == trt.TensorIOMode.OUTPUT
        )
        self._buffers = {
            name: DeviceBuffer(cuda)
            for name in (*self.input_names, *self.output_names)
        }

    def __call__(self, inputs: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        if set(inputs) != set(self.input_names):
            raise ValueError(
                f"TensorRT inputs {sorted(inputs)} differ from {sorted(self.input_names)}"
            )

        contiguous: dict[str, np.ndarray] = {}
        for name in self.input_names:
            expected = _numpy_dtype(self._engine.get_tensor_dtype(name))
            value = np.ascontiguousarray(inputs[name], dtype=expected)
            if not self._context.set_input_shape(name, value.shape):
                raise ValueError(f"TensorRT rejected {name} shape {value.shape}")
            contiguous[name] = value

        outputs: dict[str, np.ndarray] = {}
        for name in self.output_names:
            shape = tuple(self._context.get_tensor_shape(name))
            if any(dimension < 0 for dimension in shape):
                raise RuntimeError(f"TensorRT did not resolve {name} shape: {shape}")
            outputs[name] = np.empty(
                shape, dtype=_numpy_dtype(self._engine.get_tensor_dtype(name))
            )

        for name, value in contiguous.items():
            pointer = self._buffers[name].reserve(value.nbytes)
            if not self._context.set_tensor_address(name, pointer.value):
                raise RuntimeError(f"could not bind TensorRT input {name}")
            self._cuda.copy_to_device(pointer, value)
        for name, value in outputs.items():
            pointer = self._buffers[name].reserve(value.nbytes)
            if not self._context.set_tensor_address(name, pointer.value):
                raise RuntimeError(f"could not bind TensorRT output {name}")

        if not self._context.execute_async_v3(self._cuda.stream.value):
            raise RuntimeError("TensorRT execution failed")
        for name, value in outputs.items():
            self._cuda.copy_to_host(value, self._buffers[name].pointer)
        self._cuda.synchronize()
        return outputs

    def close(self) -> None:
        for buffer in self._buffers.values():
            buffer.close()


class TensorRTMatcher:
    """Run SuperPoint and SuperGlue without importing PyTorch."""

    def __init__(
        self,
        engine_dir: Path,
        *,
        keypoint_threshold: float,
    ) -> None:
        self._keypoint_threshold = float(keypoint_threshold)
        self._cuda = CudaRuntime()
        self._superpoint = TensorRTEngine(
            self._cuda, engine_dir / "superpoint_fp16.plan"
        )
        self._superglue = TensorRTEngine(
            self._cuda, engine_dir / "superglue_fp32.plan"
        )
        self.backend_name = "tensorrt-cuda"
        self.runtime_version = trt.__version__
        self.cuda_version = self._cuda.runtime_version
        self.device_name = self._cuda.device_name
        self.compute_capability = self._cuda.compute_capability

    @staticmethod
    def _image_tensor(image: np.ndarray) -> np.ndarray:
        if image.shape != (IMAGE_HEIGHT, IMAGE_WIDTH):
            raise ValueError(
                f"TensorRT image must be {IMAGE_HEIGHT}x{IMAGE_WIDTH}, got {image.shape}"
            )
        return np.ascontiguousarray(
            image.astype(np.float32, copy=False)[None, None] / 255.0
        )

    def extract(self, image: np.ndarray):
        output = self._superpoint({"image": self._image_tensor(image)})
        scores = output["scores"]
        keep = scores > self._keypoint_threshold
        return (
            output["keypoints"][keep].astype(np.float32, copy=False),
            output["descriptors"][keep].astype(np.float32, copy=False),
            scores[keep].astype(np.float32, copy=False),
        )

    def warmup(self, height: int = 640, width: int = 544, runs: int = 2) -> None:
        yy, xx = np.indices((height, width))
        left = (((xx // 32 + yy // 32) % 2) * 180 + 35).astype(np.uint8)
        right = np.roll(left, -8, axis=1).copy()
        for index in range(max(1, int(runs))):
            started = time.perf_counter()
            result = self.match(left, right)
            print(
                f"TensorRT/CUDA warmup {index + 1}/{runs}: "
                f"matches={len(result[0])}, "
                f"elapsed_ms={(time.perf_counter() - started) * 1000.0:.2f}",
                flush=True,
            )

    def match(self, left: np.ndarray, right: np.ndarray):
        keypoints0, descriptors0, scores0 = self.extract(left)
        keypoints1, descriptors1, scores1 = self.extract(right)
        if not len(keypoints0) or not len(keypoints1):
            empty_points = np.empty((0, 2), dtype=np.float32)
            return (
                empty_points,
                empty_points.copy(),
                np.empty((0, DESCRIPTOR_DIM), dtype=np.float32),
                np.empty((0,), dtype=np.float32),
                len(keypoints0),
                len(keypoints1),
            )
        output = self._superglue(
            {
                "keypoints0": keypoints0[None],
                "keypoints1": keypoints1[None],
                "scores0": scores0[None],
                "scores1": scores1[None],
                "descriptors0": descriptors0.T[None],
                "descriptors1": descriptors1.T[None],
            }
        )
        matches0 = output["matches0"][0]
        left_indices = np.flatnonzero(matches0 >= 0)
        right_indices = matches0[left_indices].astype(np.int64)
        return (
            keypoints0[left_indices].astype(np.float32, copy=False),
            keypoints1[right_indices].astype(np.float32, copy=False),
            descriptors0[left_indices].astype(np.float32, copy=False),
            output["matching_scores0"][0, left_indices].astype(
                np.float32, copy=False
            ),
            len(keypoints0),
            len(keypoints1),
        )

    def close(self) -> None:
        self._superpoint.close()
        self._superglue.close()
        self._cuda.close()
