"""Launchpad-side executor seam for Skill Lab (D16 / N13).

One module per exec CLI; today exactly one — `executors/opencode.py`. The vendored
half lives in `vendor/skillopt/skillopt/model/opencode_backend.py` (subprocess
boundary: this package never imports the vendored tree)."""
