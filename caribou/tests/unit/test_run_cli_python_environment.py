from pathlib import Path

from caribou.cli.run_cli import _extract_common_kwargs


def test_python_environment_option_is_forwarded_to_shared_initialization(
    tmp_path: Path,
) -> None:
    prefix = tmp_path / "analysis"

    options = _extract_common_kwargs({"python_env": prefix})

    assert options["python_env"] == prefix
