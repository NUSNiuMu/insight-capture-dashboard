from pathlib import Path
import re
import subprocess

import pytest


RENDER = Path(__file__).resolve().parents[2] / "deploy/jetson_fan_profile.awk"


@pytest.mark.parametrize(("margin", "group_max", "expected"), [
    ("ENABLED", 105, [(0, 255), (30, 255), (40, 220), (50, 180), (60, 145), (105, 145)]),
    ("ENABLED", 95, [(0, 255), (20, 255), (30, 220), (40, 180), (50, 145), (95, 145)]),
    ("DISABLED", 105, [(0, 145), (45, 145), (55, 180), (65, 220), (75, 255), (105, 255)]),
])
def test_balanced_curve_uses_board_temperature_basis(margin, group_max, expected):
    thermal_group = f"THERMAL_GROUP 0 {{\n\tGROUP_MAX_TEMP {group_max}\n\tCPU-therm 100 0\n}}"
    source = f"""POLLING_INTERVAL 2
<FAN 1>
TMARGIN {margin}
FAN_GOVERNOR pid {{
    STEP_SIZE 10
}}
FAN_PROFILE quiet {{
    0 0 255 6000
    105 0 0 0
}}
{thermal_group}
FAN_DEFAULT_CONTROL close_loop
FAN_DEFAULT_PROFILE quiet
FAN_DEFAULT_GOVERNOR pid
KICKSTART_PWM 64
"""
    result = subprocess.run(["awk", "-f", str(RENDER)], input=source,
                            capture_output=True, text=True, check=True)
    config = result.stdout
    assert thermal_group in config
    assert "FAN_DEFAULT_PROFILE balanced" in config
    assert "FAN_DEFAULT_CONTROL open_loop" in config
    assert "FAN_DEFAULT_GOVERNOR cont" in config
    assert config.count("FAN_GOVERNOR cont {") == 1
    curve = re.search(r"FAN_PROFILE balanced \{\n(.*?)\n\s*\}", config, re.S).group(1)
    assert [tuple(map(int, row.split())) for row in curve.splitlines()] == [
        (temperature, 0, pwm, 0) for temperature, pwm in expected
    ]
