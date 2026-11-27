from __future__ import annotations

from enum import IntEnum, IntFlag
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
PARAMS_PY_PATH = REPO_ROOT / "common" / "params.py"


class _FakeParamKeyFlag(IntFlag):
  PERSISTENT = 0x02
  CLEAR_ON_MANAGER_START = 0x04
  CLEAR_ON_ONROAD_TRANSITION = 0x08
  CLEAR_ON_OFFROAD_TRANSITION = 0x10
  DEVELOPMENT_ONLY = 0x40
  CLEAR_ON_IGNITION_ON = 0x80
  ALL = 0xFFFFFFFF
  DONT_LOG = 0x20


class _FakeParamKeyType(IntEnum):
  STRING = 0
  BOOL = 1
  INT = 2
  FLOAT = 3
  TIME = 4
  JSON = 5
  BYTES = 6


class _FakeUnknownKeyName(Exception):
  pass


def _make_fake_params_pyx(extra_compiled_keys: dict[str, tuple[_FakeParamKeyType, object]] | None = None):
  compiled_keys: dict[str, tuple[_FakeParamKeyType, object, int]] = {
    "DongleId": (_FakeParamKeyType.STRING, None, int(_FakeParamKeyFlag.PERSISTENT)),
    "CarParams": (_FakeParamKeyType.BYTES, None, int(_FakeParamKeyFlag.CLEAR_ON_MANAGER_START)),
    "IsMetric": (_FakeParamKeyType.BOOL, b"0", int(_FakeParamKeyFlag.PERSISTENT)),
  }
  if extra_compiled_keys:
    for k, (kt, default_val) in extra_compiled_keys.items():
      compiled_keys[k] = (kt, default_val, int(_FakeParamKeyFlag.PERSISTENT))

  mod = types.ModuleType("openpilot.common.params_pyx")
  mod.ParamKeyFlag = _FakeParamKeyFlag
  mod.ParamKeyType = _FakeParamKeyType
  mod.UnknownKeyName = _FakeUnknownKeyName

  class _FakeParams:
    def __new__(cls, d="", *, memory=False, return_defaults=False):
      self = super().__new__(cls)
      base_dir = Path(d) if d else Path(tempfile.gettempdir()) / ("fake_mem_params" if memory else "fake_params")
      self._d_dir = base_dir / "d"
      self._d_dir.mkdir(parents=True, exist_ok=True)
      self._m = bool(memory)
      self._return_defaults = bool(return_defaults or memory)
      return self

    def _norm(self, key) -> str:
      return key.decode("utf-8") if isinstance(key, bytes) else str(key)

    def check_key(self, key):
      k = self._norm(key)
      if k not in compiled_keys:
        raise _FakeUnknownKeyName(key)
      return k.encode("utf-8")

    def get_param_path(self, key=""):
      if not key:
        return str(self._d_dir)
      return str(self._d_dir / self._norm(key))

    def get(self, key, block=False, return_default=False):
      k = self.check_key(key).decode("utf-8")
      kt, default_val, _ = compiled_keys[k]
      path = self._d_dir / k
      raw = path.read_bytes() if path.is_file() else b""
      if raw == b"":
        raw = default_val if (return_default or (self._return_defaults and not block)) and default_val is not None else None
      if raw is None:
        return None
      if kt == _FakeParamKeyType.BOOL:
        return raw == b"1"
      if kt == _FakeParamKeyType.STRING:
        return raw.decode("utf-8")
      if kt == _FakeParamKeyType.INT:
        return int(raw.decode("utf-8"))
      return raw

    def put(self, key, dat):
      k = self.check_key(key).decode("utf-8")
      kt, _, _ = compiled_keys[k]
      if kt == _FakeParamKeyType.BOOL:
        raw = b"1" if dat else b"0"
      elif kt == _FakeParamKeyType.INT:
        raw = str(int(dat)).encode("utf-8")
      elif isinstance(dat, bytes):
        raw = dat
      else:
        raw = str(dat).encode("utf-8")
      (self._d_dir / k).write_bytes(raw)

    def put_bool(self, key, val):
      self.put(key, bool(val))

    def put_nonblocking(self, key, dat):
      self.put(key, dat)

    def put_bool_nonblocking(self, key, val):
      self.put_bool(key, val)

    def remove(self, key):
      k = self.check_key(key).decode("utf-8")
      try:
        (self._d_dir / k).unlink()
      except FileNotFoundError:
        pass

    def clear_all(self, tx_flag=_FakeParamKeyFlag.ALL):
      # Mirror C++ Params::clearAll: remove matching flags and any files absent from keys map.
      for entry in list(self._d_dir.iterdir()):
        if entry.is_dir():
          continue
        spec = compiled_keys.get(entry.name)
        if spec is None or (spec[2] & int(tx_flag)):
          entry.unlink()

    def all_keys(self):
      return [k.encode("utf-8") for k in compiled_keys]

    def get_type(self, key):
      k = self.check_key(key).decode("utf-8")
      return compiled_keys[k][0]

    def get_default_value(self, key):
      k = self.check_key(key).decode("utf-8")
      kt, default_val, _ = compiled_keys[k]
      if default_val is None:
        return None
      if kt == _FakeParamKeyType.BOOL:
        return default_val == b"1"
      return default_val

    def get_stock_value(self, key):
      return self.get_default_value(key)

    def get_tuning_level(self, key):
      self.check_key(key)
      return 0

    def get_key_flag(self, key):
      k = self.check_key(key).decode("utf-8")
      return _FakeParamKeyFlag(compiled_keys[k][2])

  mod.Params = _FakeParams
  return mod


def _load_wrapped_params_module(fake_pyx_mod):
  saved = {
    name: sys.modules.get(name)
    for name in ("openpilot", "openpilot.common", "openpilot.common.params_pyx", "openpilot.common.params_under_test")
  }
  openpilot_pkg = types.ModuleType("openpilot")
  openpilot_pkg.__path__ = [str(REPO_ROOT)]
  common_pkg = types.ModuleType("openpilot.common")
  common_pkg.__path__ = [str(REPO_ROOT / "common")]

  sys.modules["openpilot"] = openpilot_pkg
  sys.modules["openpilot.common"] = common_pkg
  if fake_pyx_mod is None:
    sys.modules.pop("openpilot.common.params_pyx", None)
  else:
    sys.modules["openpilot.common.params_pyx"] = fake_pyx_mod

  spec = importlib.util.spec_from_file_location("openpilot.common.params_under_test", PARAMS_PY_PATH)
  module = importlib.util.module_from_spec(spec)
  assert spec is not None and spec.loader is not None
  try:
    sys.modules["openpilot.common.params_under_test"] = module
    spec.loader.exec_module(module)
    return module
  finally:
    for name, prev in saved.items():
      if prev is None:
        sys.modules.pop(name, None)
      else:
        sys.modules[name] = prev


class TestExtraPersistentParams(unittest.TestCase):
  def test_extra_keys_persist_survive_manager_clear_and_reset_on_all(self):
    fake_pyx = _make_fake_params_pyx()
    params_mod = _load_wrapped_params_module(fake_pyx)
    Params = params_mod.Params
    ParamKeyFlag = params_mod.ParamKeyFlag
    ParamKeyType = params_mod.ParamKeyType
    UnknownKeyName = params_mod.UnknownKeyName

    with tempfile.TemporaryDirectory() as tmp:
      p = Params(tmp)

      # Defaults before any writes
      self.assertEqual(p.get_int("LeadMarkerStyle", return_default=True, default=0), 0)
      self.assertEqual(p.get_int("LeadMarkerStyle", default=0), 0)
      self.assertIsNone(p.get("LeadMarkerStyle"))
      for key in ("LeadMetricsDesired", "LeadMetricsDistance", "LeadMetricsSpeed", "LeadMetricsTimeGap"):
        self.assertTrue(p.get_bool(key), f"{key} should default to True")
      self.assertFalse(p.get_bool("PauseLateralOnOverride"))

      # Persist writes across fresh Params(tmp) instances
      p.put_int("LeadMarkerStyle", 1)
      p.put_bool("LeadMetricsDesired", False)
      p.put_bool("LeadMetricsDistance", False)
      p.put_bool("LeadMetricsSpeed", False)
      p.put_bool("LeadMetricsTimeGap", False)
      p.put_bool("PauseLateralOnOverride", True)
      p.put("CarParams", b"transient")
      p.put("DongleId", "ev6-test")

      # Even if an uncompiled file ends up in d/, C++ clear_all removes d/ unknowns,
      # while extra persistent keys live safely in custom_d/ beside d/.
      rogue_d_file = Path(tmp) / "d" / "StrayUnknown"
      rogue_d_file.write_text("stray", encoding="utf-8")

      p2 = Params(tmp)
      self.assertEqual(p2.get_int("LeadMarkerStyle", return_default=True, default=0), 1)
      self.assertEqual(p2.get_int("LeadMarkerStyle", default=0), 1)
      p2.put_int("LeadMarkerStyle", 2)
      self.assertEqual(Params(tmp).get_int("LeadMarkerStyle"), 2)
      for key in ("LeadMetricsDesired", "LeadMetricsDistance", "LeadMetricsSpeed", "LeadMetricsTimeGap"):
        self.assertFalse(p2.get_bool(key), f"{key} should persist False")
      self.assertTrue(p2.get_bool("PauseLateralOnOverride"))
      self.assertTrue(Path(p2.get_param_path("PauseLateralOnOverride")).is_file())
      self.assertEqual(p2.get_type("LeadMarkerStyle"), ParamKeyType.INT)
      self.assertEqual(p2.get_type("PauseLateralOnOverride"), ParamKeyType.BOOL)

      # CLEAR_ON_MANAGER_START clears transient CarParams & rogue d/ files, keeps extra keys
      p2.clear_all(ParamKeyFlag.CLEAR_ON_MANAGER_START)
      self.assertIsNone(p2.get("CarParams"))
      self.assertFalse(rogue_d_file.exists())
      self.assertEqual(p2.get("DongleId"), "ev6-test")
      self.assertEqual(Params(tmp).get_int("LeadMarkerStyle"), 2)
      self.assertFalse(Params(tmp).get_bool("LeadMetricsDesired"))
      self.assertTrue(Params(tmp).get_bool("PauseLateralOnOverride"))

      # all_keys() + get_default_value() integration (mirrors StarPilotVariables.__init__)
      p_defaults = Params(tmp, return_defaults=True)
      defaults_map = {k.decode(): p_defaults.get_default_value(k) for k in p_defaults.all_keys()}
      self.assertEqual(defaults_map["LeadMarkerStyle"], 0)
      self.assertTrue(defaults_map["LeadMetricsDesired"])
      self.assertTrue(defaults_map["LeadMetricsDistance"])
      self.assertTrue(defaults_map["LeadMetricsSpeed"])
      self.assertTrue(defaults_map["LeadMetricsTimeGap"])
      self.assertFalse(defaults_map["PauseLateralOnOverride"])

      # Unknown keys remain strictly rejected
      self.assertIsNone(p2.get("swag"))
      self.assertFalse(p2.get_bool("swag"))
      with self.assertRaises(UnknownKeyName):
        p2.put("swag", "abc")
      with self.assertRaises(UnknownKeyName):
        p2.put_bool("swag", True)

      # ALL resets persistent keys including extra keys
      p2.clear_all(ParamKeyFlag.ALL)
      p3 = Params(tmp)
      self.assertEqual(p3.get_int("LeadMarkerStyle", return_default=True, default=0), 0)
      self.assertTrue(p3.get_bool("LeadMetricsDesired"))
      self.assertFalse(p3.get_bool("PauseLateralOnOverride"))

      # Metadata, nonblocking puts, cpp2python, and remove
      self.assertEqual(p3.get_stock_value("LeadMarkerStyle"), 0)
      self.assertEqual(p3.get_tuning_level("LeadMetricsDesired"), 3)
      self.assertEqual(p3.get_settings_tier("PauseLateralOnOverride"), params_mod.SETTINGS_SIMPLE)
      self.assertEqual(p3.get_key_flag("PauseLateralOnOverride"), ParamKeyFlag.PERSISTENT)
      self.assertEqual(p3.cpp2python("LeadMarkerStyle", b"2"), 2)
      self.assertFalse(p3.cpp2python("LeadMetricsDesired", b"0"))
      p3.put_nonblocking("LeadMarkerStyle", "1")
      p3.put_bool_nonblocking("PauseLateralOnOverride", True)
      self.assertEqual(Params(tmp).get_int("LeadMarkerStyle"), 1)
      self.assertTrue(Params(tmp).get_bool("PauseLateralOnOverride"))
      p3.remove("LeadMarkerStyle")
      self.assertIsNone(Params(tmp).get("LeadMarkerStyle"))

  def test_uses_compiled_storage_when_prebuilt_already_includes_key(self):
    fake_pyx = _make_fake_params_pyx(extra_compiled_keys={"PauseLateralOnOverride": (_FakeParamKeyType.BOOL, b"0")})
    params_mod = _load_wrapped_params_module(fake_pyx)
    Params = params_mod.Params

    with tempfile.TemporaryDirectory() as tmp:
      p = Params(tmp)
      p.put_bool("PauseLateralOnOverride", True)
      self.assertTrue((Path(tmp) / "d" / "PauseLateralOnOverride").is_file())
      self.assertFalse((Path(tmp) / "custom_d" / "PauseLateralOnOverride").exists())
      self.assertTrue(Params(tmp).get_bool("PauseLateralOnOverride"))

  def test_fallback_params_without_params_pyx(self):
    params_mod = _load_wrapped_params_module(None)
    params_mod._PERSISTENT_STORE.clear()
    Params = params_mod.Params

    p = Params(return_defaults=True)
    self.assertEqual(p.get("LeadMarkerStyle"), 0)
    self.assertTrue(p.get_bool("LeadMetricsDesired"))
    self.assertFalse(p.get_bool("PauseLateralOnOverride"))
    self.assertEqual(p.get_default_value("LeadMarkerStyle"), 0)
    self.assertEqual(p.get_stock_value("LeadMetricsDistance"), True)
    self.assertEqual(p.get_tuning_level("PauseLateralOnOverride"), 1)
    p.put_bool("LeadMetricsDesired", False)
    self.assertFalse(p.get_bool("LeadMetricsDesired"))
    params_mod._PERSISTENT_STORE.clear()


if __name__ == "__main__":
  unittest.main()
