"""Explicit local configuration; never select a paid fallback."""
import json
from pathlib import Path

from . import findings
from . import review

DEFAULT_CHECKS = ['rust-format', 'rust-clippy', 'rust-tests', 'supervisor-tests']

# Verified no-cost backends only; anything else is a rejected paid fallback.
FREE_MODELS = ('swe-2-high', 'swe-2-medium', 'swe-2-max',
               'opencode/union-alpha',
               'opencode/muse-spark-1.3-contributor-free',
               'opencode/muse-spark-1.2-contributor-free',
               'opencode/ling-3.0-flash-fin-free',
               'opencode/mimo-v2.5-free',
               'opencode/nemotron-3-ultra-free',
               'opencode/nemotron-3.5-lightning-free')

def load(path=None):
    path = Path(path or Path.home() / '.config/dcs-agents/config.json')
    config = json.loads(path.read_text())
    models = config.get('models') or [config.get('model', 'swe-2-high')]
    if not isinstance(models, list) or not models or any(not isinstance(m, str) or not m for m in models):
        raise ValueError('models must be a non-empty list of model identifiers')
    unknown = [m for m in models if m not in FREE_MODELS]
    if unknown:
        raise ValueError('This installation permits only verified free models; rejected: ' + ', '.join(unknown))
    config['models'] = models
    caps = config.get('model_caps') or {}
    if not isinstance(caps, dict) or any(not isinstance(v, int) or v < 1 for v in caps.values()):
        raise ValueError('model_caps must map model identifiers to positive integers')
    unknown = [m for m in caps if m not in FREE_MODELS]
    if unknown:
        raise ValueError('model_caps references unpermitted models: ' + ', '.join(unknown))
    config['model_caps'] = caps
    config.setdefault('required_checks', DEFAULT_CHECKS)
    if config['required_checks'] != DEFAULT_CHECKS:
        raise ValueError('Required CI checks cannot be weakened in active configuration')
    config.setdefault('poll_seconds', 60)
    config.setdefault('timeout_seconds', 7200)
    config['review'] = review.settings(config)
    config['qa'] = findings.settings(config)
    return config
