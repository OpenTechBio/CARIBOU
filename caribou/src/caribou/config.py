# caribou/config.py
import os
from pathlib import Path
from platformdirs import PlatformDirs

# Define app-specific identifiers for platformdirs
APP_NAME = "caribou"
APP_AUTHOR = "OpenTechBio"
dirs = PlatformDirs(APP_NAME, APP_AUTHOR)

# Define the root directory for all user-specific CARIBOU files.
# This respects the CARIBOU_HOME environment variable but has a sensible default.
CARIBOU_HOME = Path(os.environ.get("CARIBOU_HOME", dirs.user_data_dir)).expanduser()

# Define standard subdirectories
DEFAULT_AGENT_DIR = CARIBOU_HOME / "agent_systems"
DEFAULT_DATASETS_DIR = CARIBOU_HOME / "datasets"

# The benchmark-validated full multi-agent system used in end-to-end evaluation
DEFAULT_BLUEPRINT_NAME = "caribou_fully_connected_v2.json"

# Define the path to the environment file for storing secrets like API keys
ENV_FILE = CARIBOU_HOME / ".env"


def init_caribou_home():
    """Ensures the main CARIBOU directory and its subdirectories exist."""
    CARIBOU_HOME.mkdir(parents=True, exist_ok=True)
    DEFAULT_AGENT_DIR.mkdir(exist_ok=True)
    DEFAULT_DATASETS_DIR.mkdir(exist_ok=True)
