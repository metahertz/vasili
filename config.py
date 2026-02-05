"""
Configuration file support for Vasili.

Supports loading configuration from YAML or JSON files. Provides sensible
defaults when no config file is present.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# Try to import PyYAML - it's optional but preferred
try:
    import yaml

    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False


@dataclass
class InterfaceConfig:
    """WiFi interface preferences."""

    # Preferred interfaces, ordered by priority
    preferred: list[str] = field(default_factory=list)
    # Interfaces to exclude from use
    excluded: list[str] = field(default_factory=list)
    # Dedicated interface for scanning (optional)
    scan_interface: Optional[str] = None


@dataclass
class ModuleConfig:
    """Module enable/disable settings."""

    # List of enabled module names. None means all modules are enabled.
    enabled: Optional[list[str]] = None


@dataclass
class ScannerConfig:
    """Network scanner settings."""

    # Interval between scans in seconds
    scan_interval: int = 5


@dataclass
class WebConfig:
    """Web interface settings."""

    host: str = '0.0.0.0'
    port: int = 5000
    enabled: bool = True


@dataclass
class LoggingConfig:
    """Logging settings."""

    level: str = 'INFO'


@dataclass
class AutoSelectionConfig:
    """Auto-selection mode settings."""

    # Enable automatic connection selection
    enabled: bool = False
    # Seconds between evaluations of available connections
    evaluation_interval: int = 30
    # Minimum score improvement required to switch connections
    min_score_improvement: float = 10.0
    # Initial delay before first auto-selection (seconds)
    initial_delay: int = 10


@dataclass
class VasiliConfig:
    """Main configuration container."""

    interfaces: InterfaceConfig = field(default_factory=InterfaceConfig)
    modules: ModuleConfig = field(default_factory=ModuleConfig)
    scanner: ScannerConfig = field(default_factory=ScannerConfig)
    web: WebConfig = field(default_factory=WebConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    auto_selection: AutoSelectionConfig = field(default_factory=AutoSelectionConfig)

    @classmethod
    def from_dict(cls, data: dict) -> 'VasiliConfig':
        """Create a VasiliConfig from a dictionary."""
        config = cls()

        if 'interfaces' in data:
            iface_data = data['interfaces']
            config.interfaces = InterfaceConfig(
                preferred=iface_data.get('preferred', []),
                excluded=iface_data.get('excluded', []),
                scan_interface=iface_data.get('scan_interface'),
            )

        if 'modules' in data:
            mod_data = data['modules']
            config.modules = ModuleConfig(
                enabled=mod_data.get('enabled'),
            )

        if 'scanner' in data:
            scan_data = data['scanner']
            config.scanner = ScannerConfig(
                scan_interval=scan_data.get('scan_interval', 5),
            )

        if 'web' in data:
            web_data = data['web']
            config.web = WebConfig(
                host=web_data.get('host', '0.0.0.0'),
                port=web_data.get('port', 5000),
                enabled=web_data.get('enabled', True),
            )

        if 'logging' in data:
            log_data = data['logging']
            config.logging = LoggingConfig(
                level=log_data.get('level', 'INFO'),
            )

        if 'auto_selection' in data:
            auto_data = data['auto_selection']
            config.auto_selection = AutoSelectionConfig(
                enabled=auto_data.get('enabled', False),
                evaluation_interval=auto_data.get('evaluation_interval', 30),
                min_score_improvement=auto_data.get('min_score_improvement', 10.0),
                initial_delay=auto_data.get('initial_delay', 10),
            )

        return config


def load_config(config_path: Optional[str] = None) -> VasiliConfig:
    """
    Load configuration from a file.

    Args:
        config_path: Path to config file. If None, searches for config.yaml,
                     config.yml, or config.json in the current directory
                     and the directory containing vasili.py.

    Returns:
        VasiliConfig with loaded settings, or defaults if no config found.
    """
    # Determine search paths
    search_paths = []

    if config_path:
        search_paths = [config_path]
    else:
        # Search in current directory and script directory
        script_dir = os.path.dirname(os.path.abspath(__file__))
        search_dirs = [os.getcwd(), script_dir]

        for search_dir in search_dirs:
            search_paths.extend(
                [
                    os.path.join(search_dir, 'config.yaml'),
                    os.path.join(search_dir, 'config.yml'),
                    os.path.join(search_dir, 'config.json'),
                ]
            )

    # Try to load from each path
    for path in search_paths:
        if os.path.exists(path):
            try:
                config = _load_config_file(path)
                logger.info(f'Loaded configuration from {path}')
                return config
            except Exception as e:
                logger.warning(f'Failed to load config from {path}: {e}')

    # Return default configuration
    logger.info('No configuration file found, using defaults')
    return VasiliConfig()


def _load_config_file(path: str) -> VasiliConfig:
    """Load configuration from a specific file."""
    with open(path, 'r') as f:
        content = f.read()

    # Determine file type by extension
    _, ext = os.path.splitext(path)
    ext = ext.lower()

    if ext in ('.yaml', '.yml'):
        if not YAML_AVAILABLE:
            raise ImportError(
                'PyYAML is required to load YAML config files. Install it with: pip install pyyaml'
            )
        data = yaml.safe_load(content)
    elif ext == '.json':
        data = json.loads(content)
    else:
        # Try YAML first (if available), then JSON
        data = None
        if YAML_AVAILABLE:
            try:
                data = yaml.safe_load(content)
            except Exception:
                pass
        if data is None:
            data = json.loads(content)

    # Handle empty files
    if data is None:
        data = {}

    return VasiliConfig.from_dict(data)


def apply_logging_config(config: VasiliConfig) -> None:
    """Apply logging configuration."""
    level_map = {
        'DEBUG': logging.DEBUG,
        'INFO': logging.INFO,
        'WARNING': logging.WARNING,
        'ERROR': logging.ERROR,
        'CRITICAL': logging.CRITICAL,
    }

    level = level_map.get(config.logging.level.upper(), logging.INFO)
    logging.getLogger().setLevel(level)
    logger.debug(f'Set logging level to {config.logging.level}')
