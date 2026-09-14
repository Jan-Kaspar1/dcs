"""Explicit local configuration; never select a paid fallback."""
import json
from pathlib import Path

from . import review

DEFAULT_CHECKS = ['rust-format', 'rust-clippy', 'rust-tests', 'supervisor-tests']

def load(path=None):
    path = Path(path or Path.home() / '.config/dcs-agents/config.json')
    config = json.loads(path.read_text())
    if config.get('model', 'swe-2-high') != 'swe-2-high':
        raise ValueError('This installation permits only swe-2-high')
    config.setdefault('required_checks', DEFAULT_CHECKS)
    if config['required_checks'] != DEFAULT_CHECKS:
        raise ValueError('Required CI checks cannot be weakened in active configuration')
    config.setdefault('poll_seconds', 60)
    config.setdefault('timeout_seconds', 7200)
    config['review'] = review.settings(config)
    return config
