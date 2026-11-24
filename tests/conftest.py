import pytest

from agent_session_vault import archive_backend
from agent_session_vault import archive_maintenance as maintenance
from agent_session_vault import stable
from agent_session_vault.config import load_config
from fake_restic import FakeResticRepository


@pytest.fixture(autouse=True)
def _use_fake_restic_for_unit_tests(request, monkeypatch):
    """Use FakeResticRepository for fast unit tests; real restic runs in restic_integration tests."""
    if request.node.get_closest_marker("restic_integration") is not None:
        return
    monkeypatch.setattr(archive_backend, "ResticRepository", FakeResticRepository)
    monkeypatch.setattr(stable, "ResticRepository", FakeResticRepository)


@pytest.fixture
def environment(tmp_path):
    home = tmp_path / "home"
    source = home / ".codex"
    source.mkdir(parents=True)
    config_path = tmp_path / "config.toml"
    config_path.write_text(f'''[paths]
home = "{home}"
workspace_root = "{tmp_path / 'workspace'}"
import_root = "{tmp_path / 'imports'}"
projection_home = "{tmp_path / 'projection'}"
local_workspace_extras = "{tmp_path / 'extras'}"
stable_root = "{tmp_path / 'nas/stable'}"
[archive]
root = "{tmp_path / 'nas/full-fidelity'}"
staging_root = "{tmp_path / 'nas/full-fidelity/staging'}"
machine_id_path = "{tmp_path / 'machine-id'}"
source_paths = ["{source}"]
''')
    config = load_config(config_path)
    run_dir = maintenance.local_root(config) / "ops/archive-automation/test-run"
    run_dir.mkdir(parents=True)
    return config, run_dir
