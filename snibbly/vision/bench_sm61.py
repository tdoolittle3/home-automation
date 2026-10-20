#!/usr/bin/env python3
"""Phase-0 gate for the vision pipeline: prove a modern runtime can still drive
the GTX 1050 Ti (Pascal, sm_61), and measure what the daily sweep would cost.

Verdict from the 2026-10-20 run — PASS on PyTorch, with two dead ends worth
remembering:

  * onnxruntime-gpu >= 1.31 is built against CUDA 13, which removed Pascal.
  * ORT 1.29 (CUDA-12 build, from the ms aiinfra "onnxruntime-cuda-12" feed)
    initializes, but every Conv dies in cuDNN 9's graph/frontend API, which
    requires Volta+. No runtime switch back to the legacy path exists.
  * torch 2.14.1+cu126 works: the cu126 wheel line still ships sm_50/sm_60
    SASS, and sm_61 executes sm_60 binaries. Measured: synthetic conv stack
    7.3 ms/frame (max abs diff vs CPU 7e-08); real YOLO11s fp32 @640
    26.5 ms/frame end-to-end, 224 MiB VRAM -> 173k frames/day = ~76 min.

Two constraints follow for every later phase: stay on the cu126 wheel index,
and stay fp32 (Pascal runs fp16 at 1/64 rate — "half" would be a 60x
slowdown, not an optimization).

Setup (snibbly's system python is 3.14, which cu126 torch wheels don't cover;
uv provides the 3.12):

  uv venv --python 3.12 /tmp/torch-venv
  UV_HTTP_TIMEOUT=600 uv pip install -p /tmp/torch-venv \
      --index-url https://download.pytorch.org/whl/cu126 torch torchvision
  uv pip install -p /tmp/torch-venv ultralytics   # only for --yolo
  /tmp/torch-venv/bin/python bench_sm61.py --device 1 [--yolo]
"""

import argparse
import sys
import time


def bench_synthetic(device, iters, size):
    import torch
    torch.manual_seed(61)
    layers, ch = [], 3
    # strided conv stack, roughly a detector backbone's FLOP shape
    for out_ch, stride in [(32, 2), (64, 2), (64, 1), (128, 2), (128, 1),
                           (256, 2), (256, 1), (512, 2)]:
        layers += [torch.nn.Conv2d(ch, out_ch, 3, stride, 1), torch.nn.ReLU()]
        ch = out_ch
    net = torch.nn.Sequential(*layers).eval()
    x = torch.randn(1, 3, size, size)
    with torch.no_grad():
        ref = net(x)
        net, xg = net.to(device), x.to(device)
        diff = (net(xg).cpu() - ref).abs().max().item()
        for _ in range(5):
            net(xg)
        torch.cuda.synchronize(device)
        t0 = time.perf_counter()
        for _ in range(iters):
            net(xg)
        torch.cuda.synchronize(device)
    ms = (time.perf_counter() - t0) / iters * 1000
    print(f"synthetic conv stack: {ms:.1f} ms/frame, max abs diff vs CPU {diff:.2e}")
    if diff > 1e-4:
        sys.exit("FAIL: GPU output diverges from CPU — kernels run but are wrong")
    return ms


def bench_yolo(device_index, iters, size):
    import numpy as np
    import torch
    from ultralytics import YOLO
    img = (np.random.default_rng(0).random((size, size, 3)) * 255).astype("uint8")
    m = YOLO("yolo11s.pt")  # auto-downloads on first run
    for _ in range(3):
        m.predict(img, device=device_index, verbose=False, imgsz=size)
    t0 = time.perf_counter()
    for _ in range(iters):
        m.predict(img, device=device_index, verbose=False, imgsz=size)
    ms = (time.perf_counter() - t0) / iters * 1000
    free, total = torch.cuda.mem_get_info(device_index)
    print(f"yolo11s fp32 @{size}: {ms:.1f} ms/frame end-to-end ({1000 / ms:.1f} fps), "
          f"{(total - free) / 2**20:.0f} MiB VRAM")
    return ms


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--device", type=int, default=1, help="CUDA device id (1 = the 1050 Ti)")
    ap.add_argument("--iters", type=int, default=50)
    ap.add_argument("--size", type=int, default=640)
    ap.add_argument("--yolo", action="store_true",
                    help="also benchmark real YOLO11s via ultralytics")
    args = ap.parse_args()

    import torch
    print(f"torch {torch.__version__} | arch list: {torch.cuda.get_arch_list()}")
    if not torch.cuda.is_available():
        sys.exit("FAIL: CUDA not available to torch")
    cap = torch.cuda.get_device_capability(args.device)
    print(f"device {args.device}: {torch.cuda.get_device_name(args.device)} sm_{cap[0]}{cap[1]}")

    ms = bench_synthetic(f"cuda:{args.device}", args.iters, args.size)
    if args.yolo:
        ms = bench_yolo(args.device, max(args.iters // 2, 10), args.size)

    daily_min = 173_000 * ms / 1000 / 60
    print(f"sweep budget: 173k frames/day -> {daily_min:.0f} min/day of GPU time")
    if ms > 120:
        sys.exit("FAIL: too slow — the daily sweep would not keep up with retention")
    print("PASS: sm_61 is viable for the vision pipeline")


if __name__ == "__main__":
    main()
