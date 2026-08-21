"""Tests for drop-in plugin discovery (DER plugins / archetypes / providers)."""

from app.profiles import archetypes, der_plugins
from app.profiles.plugin_loader import load_external_plugins

_DER_PLUGIN_FILE = '''
import numpy as np
from app.profiles.der_plugins import register, DERPlugin


class HeatPumpExtPlugin(DERPlugin):
    name, order = "heatpump_ext", 35
    net_load = {"heatpump_kw": +1.0}

    def applies_to(self, bus):
        return bool(bus.get("has_heatpump"))

    def generate(self, ctx):
        ctx.series["heatpump_kw"] = np.full(ctx.timesteps, 2.0)


register(HeatPumpExtPlugin())
'''

_ARCHETYPE_MODULE = '''
import numpy as np
from app.profiles.archetypes import register_archetype

register_archetype(
    "com_test_arch", "commercial",
    lambda season, day_type="weekday": np.full(96, 3.0),
)
'''


def test_empty_config_is_noop():
    assert load_external_plugins() == []
    assert load_external_plugins("", "") == []


def test_bad_module_is_skipped():
    assert load_external_plugins(modules="does.not.exist.xyz") == []


def test_loads_der_plugin_from_directory(tmp_path):
    (tmp_path / "heatpump.py").write_text(_DER_PLUGIN_FILE)
    saved = dict(der_plugins._REGISTRY)
    try:
        loaded = load_external_plugins(plugins_dir=str(tmp_path))
        assert any("heatpump.py" in s for s in loaded)
        assert "heatpump_ext" in der_plugins.der_types()  # registered without editing the package
    finally:
        der_plugins._REGISTRY.clear()
        der_plugins._REGISTRY.update(saved)


def test_loads_module_by_name(tmp_path, monkeypatch):
    (tmp_path / "ext_archetype_mod.py").write_text(_ARCHETYPE_MODULE)
    monkeypatch.syspath_prepend(str(tmp_path))
    saved = dict(archetypes._REGISTRY)
    try:
        loaded = load_external_plugins(modules="ext_archetype_mod")
        assert "ext_archetype_mod" in loaded
        assert "com_test_arch" in archetypes.available_archetypes()
    finally:
        archetypes._REGISTRY.clear()
        archetypes._REGISTRY.update(saved)


def test_broken_file_is_skipped(tmp_path):
    (tmp_path / "broken.py").write_text("this is not valid python @#$%")
    assert load_external_plugins(plugins_dir=str(tmp_path)) == []


def test_underscore_files_are_skipped(tmp_path):
    (tmp_path / "_private.py").write_text("raise RuntimeError('should not import')")
    assert load_external_plugins(plugins_dir=str(tmp_path)) == []
