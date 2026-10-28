#!/usr/bin/env python3
"""Small GPU, checkpoint, and persistent-output test for AIStation."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import socket
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # Each invocation keeps its own receipt and never replaces an older result.
    args.output.mkdir(parents=True, exist_ok=False)
    import torch

    started = time.time()
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is unavailable; this test requires one allocated GPU")
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.manual_seed(42)
    matrix = torch.randn(512, 512, dtype=torch.float32)
    reference = matrix @ matrix.T
    gpu_matrix = matrix.to("cuda")
    result = gpu_matrix @ gpu_matrix.T
    torch.cuda.synchronize()
    actual = result.cpu()
    max_error = float((actual - reference).abs().max())
    if not torch.allclose(actual, reference, rtol=1e-4, atol=1e-3):
        raise RuntimeError(f"GPU result differs from CPU reference: max_error={max_error}")
    checkpoint = args.output / "checkpoint.pt"
    torch.save({"result": actual}, checkpoint)
    restored = torch.load(checkpoint, map_location="cpu", weights_only=True)["result"]
    if not torch.equal(actual, restored):
        raise RuntimeError("Checkpoint round trip failed")
    receipt = {
        "status": "PASS",
        "hostname": socket.gethostname(),
        "uid": os.getuid(),
        "python": platform.python_version(),
        "torch": torch.__version__,
        "torch_cuda": torch.version.cuda,
        "gpu_count_visible": torch.cuda.device_count(),
        "gpu": torch.cuda.get_device_name(0),
        "gpu_memory_bytes": torch.cuda.get_device_properties(0).total_memory,
        "matrix_shape": [512, 512],
        "max_absolute_error": max_error,
        "checkpoint_sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
        "output": str(args.output.resolve()),
        "elapsed_seconds": round(time.time() - started, 3),
    }
    (args.output / "result.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2), flush=True)


if __name__ == "__main__":
    main()
