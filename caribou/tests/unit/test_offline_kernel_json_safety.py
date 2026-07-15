"""Regression coverage for the offline kernel's numpy-aware JSON encoder.

Observed for real: agent-generated code computing a per-cluster mean
expression value from an AnnData/pandas pipeline and then calling
json.dump/json.dumps on a dict containing that value raised an unhandled
TypeError ("Object of type float32 is not JSON serializable"), losing the
run's very last turn with no turns left to recover (agent_pilot_v4,
run_5d8d32dc44314aa0823f14a359053130, turn 12).
"""
from __future__ import annotations

import json
import os

import numpy as np
import pytest

# Importing offline_kernel sets several os.environ defaults for in-container
# use (MPLCONFIGDIR, CELLTYPIST_HOME, ...) as an import-time side effect.
# Restore the host test process's environment immediately so this import
# does not leak ambient values into unrelated tests later in the same
# pytest session (e.g. singularity-backend tests asserting on the absence
# of a CELLTYPIST env var).
_ENV_KEYS = (
    "MPLCONFIGDIR",
    "NUMBA_CACHE_DIR",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "CELLTYPIST_HOME",
    "CELLTYPIST_FOLDER",
    "TRANSFORMERS_CACHE",
)
_env_before_import = {key: os.environ.get(key) for key in _ENV_KEYS}

from caribou.sandbox import offline_kernel  # noqa: E402

for _key, _value in _env_before_import.items():
    if _value is None:
        os.environ.pop(_key, None)
    else:
        os.environ[_key] = _value


def test_numpy_scalar_is_json_serializable_after_patch():
    result = offline_kernel._run(
        "import json\n"
        "import numpy as np\n"
        "print(json.dumps({'mean_expr': np.float32(1.5), 'count': np.int64(3)}))\n",
        {"__builtins__": __builtins__},
    )

    assert result["status"] == "ok"
    payload = json.loads(result["stdout"])
    assert payload == {"mean_expr": pytest.approx(1.5), "count": 3}


def test_numpy_array_is_json_serializable_after_patch():
    result = offline_kernel._run(
        "import json\n"
        "import numpy as np\n"
        "print(json.dumps({'values': np.array([1.0, 2.0, 3.0], dtype=np.float32)}))\n",
        {"__builtins__": __builtins__},
    )

    assert result["status"] == "ok"
    payload = json.loads(result["stdout"])
    assert payload == {"values": [1.0, 2.0, 3.0]}


def test_genuinely_unserializable_object_still_errors():
    result = offline_kernel._run(
        "import json\n"
        "class Unserializable:\n"
        "    pass\n"
        "print(json.dumps({'x': Unserializable()}))\n",
        {"__builtins__": __builtins__},
    )

    assert result["status"] == "error"
    assert "not JSON serializable" in result["stderr"]


def test_patch_delegates_to_original_default_for_unknown_types():
    with pytest.raises(TypeError):
        json.JSONEncoder().default(object())
