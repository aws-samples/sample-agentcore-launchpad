"""Skill Lab — SkillOpt-based skill evaluation & training.

The vendored SkillOpt tree (vendor/skillopt) is consumed ONLY as a subprocess
running in the dedicated venv (data/skill-lab-venv). Never import vendored
modules into the backend process: the boto3 funnel guard does not scan
vendor/, and skillopt captures env vars at import time.
"""
