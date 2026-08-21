"""Drop-in plugin discovery.

Load external modules that register DER plugins, archetypes, weather providers,
or anything else, so the engine is extended without editing the package. Two
sources, both off by default:

  - ``DER_PLUGIN_MODULES``: comma-separated importable module names (on the
    PYTHONPATH), e.g. "myco.heatpump,myco.electrolyser".
  - ``DER_PLUGINS_DIR``: a directory whose ``*.py`` files are imported in name
    order (files starting with "_" are skipped).

Each loaded module simply calls the relevant ``register()`` at import time (e.g.
``from app.profiles.der_plugins import register, DERPlugin``). Import errors are
logged and skipped, never fatal — one bad plugin cannot break startup.
"""

import importlib
import importlib.util
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def load_external_plugins(modules: str = "", plugins_dir: str = "") -> list[str]:
    """Import the configured external plugin modules and directory files.

    Returns the list of successfully loaded module names / file paths.
    """
    loaded: list[str] = []

    for name in (m.strip() for m in (modules or "").split(",")):
        if not name:
            continue
        try:
            importlib.import_module(name)
            loaded.append(name)
            logger.info("Loaded plugin module '%s'", name)
        except Exception as exc:  # noqa: BLE001 — one bad plugin must not break boot
            logger.warning("Could not load plugin module '%s': %s", name, exc)

    if plugins_dir and os.path.isdir(plugins_dir):
        for path in sorted(Path(plugins_dir).glob("*.py")):
            if path.name.startswith("_"):
                continue
            try:
                spec = importlib.util.spec_from_file_location(f"der_plugin_{path.stem}", path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                loaded.append(str(path))
                logger.info("Loaded plugin file '%s'", path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not load plugin file '%s': %s", path, exc)

    return loaded
