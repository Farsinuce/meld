"""The gpu_accel setting: default, flag pass-through, and the host-specific rule.

The measured facts behind this toggle live in
arnis-283-src/docs/PHASE2-GPU-MEASURED.md; what Meld owns is only that the setting
reaches arnis as --gpu, that nonsense degrades to off, and that the choice never
travels inside world metadata to a machine with different hardware.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import arnis_cmd  # noqa: E402


def _cmd(settings):
    base = {"ground_level": -62, "rotation": 0, "terrain": False, "scale": 1.0}
    base.update(settings)
    return arnis_cmd.build_arnis_cmd(
        arnis_exe="arnis.exe",
        bbox={"south": 52.0, "west": 5.0, "north": 52.01, "east": 5.01},
        output_path="out",
        settings=base,
        origin={},
        elevation=None,
        seed=1,
    )


def test_off_by_default_passes_no_flag():
    assert "--gpu" not in _cmd({})
    assert "--gpu" not in _cmd({"gpu_accel": "off"})


def test_each_mode_passes_through():
    for mode in ("auto", "dgpu", "igpu"):
        cmd = _cmd({"gpu_accel": mode})
        assert cmd[cmd.index("--gpu") + 1] == mode


def test_nonsense_degrades_to_off():
    # An arbitrary string must never reach arnis as an adapter filter: a user's
    # stale or hand-edited settings should degrade to the CPU, not to a
    # surprising adapter match.
    assert "--gpu" not in _cmd({"gpu_accel": "quadro"})
    assert "--gpu" not in _cmd({"gpu_accel": None})


def test_gpu_choice_is_host_specific_in_server_meta():
    # server.py excludes host-specific settings from world metadata so a project
    # opened on another machine does not inherit this machine's GPU choice.
    text = (Path(__file__).resolve().parent.parent / "server.py").read_text(
        encoding="utf-8"
    )
    assert '"gpu_accel", "master_world_dir"' in text, (
        "gpu_accel must stay in _META_SKIP_SETTINGS"
    )
