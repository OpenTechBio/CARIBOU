from pathlib import Path
from types import SimpleNamespace

import docker

from caribou.sandbox import benchmarking_sandbox_management as docker_sandbox


class _FakeContainer:
    status = "running"
    short_id = "abc123"

    def exec_run(self, command):
        return SimpleNamespace(
            exit_code=0,
            output=b"__CARIBOU_PYTHON_VERSION__=3.12.4\n",
        )


class _FakeContainers:
    def __init__(self):
        self.container = None
        self.run_options = None

    def get(self, name):
        if self.container is None:
            raise docker.errors.NotFound("missing")
        return self.container

    def run(self, image, **options):
        self.run_options = options
        self.container = _FakeContainer()
        return self.container


class _FakeClient:
    def __init__(self):
        self.containers = _FakeContainers()
        self.images = SimpleNamespace(
            get=lambda image: SimpleNamespace(
                attrs={
                    "Config": {
                        "Labels": {"org.caribou.host-python-environment": "1"}
                    }
                }
            )
        )

    def ping(self):
        return True


def test_docker_mounts_selected_environment_read_only(
    tmp_path: Path, monkeypatch
) -> None:
    prefix = tmp_path / "analysis"
    python = prefix / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o755)
    (prefix / "conda-meta").mkdir()
    (prefix / "conda-meta" / "history").write_text("+python-3.12\n")
    client = _FakeClient()
    monkeypatch.setattr(docker_sandbox.docker, "from_env", lambda: client)
    monkeypatch.setattr(docker_sandbox.time, "sleep", lambda seconds: None)
    manager = docker_sandbox.SandboxManager()
    manager.set_python_environment(prefix)

    assert manager.start_container() is True

    options = client.containers.run_options
    assert options["volumes"][str(prefix.resolve())] == {
        "bind": str(prefix.resolve()),
        "mode": "ro",
    }
    assert options["environment"]["CARIBOU_PYTHON_EXECUTABLE"] == str(
        python.resolve()
    )
    assert options["environment"]["PYTHONNOUSERSITE"] == "1"
    assert manager.python_environment.mode == "host"
    assert manager.python_environment.python_version == "3.12.4"


def test_docker_rebuilds_legacy_image_once_for_host_environment(
    tmp_path: Path, monkeypatch
) -> None:
    prefix = tmp_path / "analysis"
    python = prefix / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o755)
    client = _FakeClient()
    client.images = SimpleNamespace(
        get=lambda image: SimpleNamespace(attrs={"Config": {"Labels": {}}})
    )
    monkeypatch.setattr(docker_sandbox.docker, "from_env", lambda: client)
    monkeypatch.setattr(docker_sandbox.time, "sleep", lambda seconds: None)
    manager = docker_sandbox.SandboxManager()
    manager.set_python_environment(prefix)
    rebuilds = []
    manager.build_image = lambda: rebuilds.append(True) or True

    assert manager.start_container() is True
    assert rebuilds == [True]
