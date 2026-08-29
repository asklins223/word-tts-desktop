# 性能基线（目标设备模型模拟）

STATUS: PASS
TARGET_DEVICE_STATUS: SIMULATED

本报告是基于开发机实测结果的保守模型模拟，不是目标设备实测，不得用于宣称最低设备支持；仍需现场测量。

```json
{
  "artifacts_per_run": [
    11
  ],
  "classification": "SIMULATED_TARGET_MODEL",
  "latency_seconds": {
    "max": 0.180664,
    "mean": 0.1772,
    "min": 0.170982,
    "p95": 0.180594
  },
  "peak_rss_raw": [
    34258944,
    34291712
  ],
  "resource_policy": {
    "audio": "on-demand artifact ticket",
    "max_active": 1,
    "max_queue": 4
  },
  "runtime": {
    "pid": 61800,
    "platform": "macOS-26.6.1-arm64-arm-64bit",
    "python": "3.12.14"
  },
  "side_effect_policy": "FAKE_PROVIDER_ONLY",
  "simulation": {
    "cpu_slowdown_factor": 2.0,
    "hardware_claim": false,
    "io_latency_ms_per_item": 8.0,
    "memory_budget_bytes": 536870912,
    "memory_budget_fit": true,
    "memory_budget_mb": 512,
    "peak_rss_observed_on_development_host": 34291712
  },
  "status": "PASS",
  "target_device_status": "SIMULATED",
  "workload": {
    "items_per_run": 10,
    "runs": 3
  }
}
```
