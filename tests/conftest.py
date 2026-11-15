import pytest

from agent_session_vault import archive_maintenance as maintenance
from agent_session_vault.config import load_config


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
