from __future__ import annotations

from enum import IntEnum, IntFlag
import os
from pathlib import Path
import tempfile

SETTINGS_SIMPLE = 0
SETTINGS_ADVANCED = 1

# Persistent keys added in Python beyond prebuilt common/params_pyx.so so AGNOS
# can run without recompiling Cython extensions on-device.
_EXTRA_PERSISTENT_KEY_SPECS: dict[str, tuple[str, object, object, int, int]] = {
  # key: (ParamKeyType member name, default, stock_value, tuning_level, settings_tier)
  "LeadMarkerStyle": ("INT", 0, 0, 2, SETTINGS_ADVANCED),
  "LeadMetricsDesired": ("BOOL", True, True, 3, SETTINGS_ADVANCED),
  "LeadMetricsDistance": ("BOOL", True, True, 3, SETTINGS_ADVANCED),
  "LeadMetricsSpeed": ("BOOL", True, True, 3, SETTINGS_ADVANCED),
  "LeadMetricsTimeGap": ("BOOL", True, True, 3, SETTINGS_ADVANCED),
  "PauseLateralOnOverride": ("BOOL", False, False, 1, SETTINGS_SIMPLE),
}
_COMPILED_KEY_CACHE: dict[str, bool] = {}


def _normalize_extra_key(key) -> str:
  if isinstance(key, bytes):
    return key.decode("utf-8", errors="replace")
  return str(key)


def _coerce_extra_bool(value: object) -> bool:
  if isinstance(value, bool):
    return value
  if isinstance(value, (int, float)):
    return bool(value)
  if isinstance(value, bytes):
    return value.strip().lower() not in (b"", b"0", b"false", b"none", b"null")
  if isinstance(value, str):
    return value.strip().lower() not in ("", "0", "false", "none", "null")
  return bool(value)


def _encode_extra_value(type_name: str, value: object) -> bytes:
  if type_name == "BOOL":
    return b"1" if _coerce_extra_bool(value) else b"0"
  if isinstance(value, bytes):
    value = value.decode("utf-8")
  if type_name == "INT":
    return str(int(float(value))).encode("utf-8")
  if type_name == "FLOAT":
    return str(float(value)).encode("utf-8")
  return str(value).encode("utf-8")


def _decode_extra_value(type_name: str, raw: object, default: object):
  if type_name == "BOOL":
    if isinstance(raw, bool):
      return raw
    if isinstance(raw, bytes):
      return raw.strip() == b"1"
    if isinstance(raw, str):
      return raw.strip() == "1"
    return bool(raw)
  if type_name == "INT":
    if isinstance(raw, bool):
      return int(raw)
    if isinstance(raw, int):
      return raw
    try:
      text = raw.decode("utf-8").strip() if isinstance(raw, bytes) else str(raw).strip()
      return int(float(text))
    except (TypeError, ValueError, UnicodeDecodeError):
      return default
  if type_name == "FLOAT":
    try:
      text = raw.decode("utf-8").strip() if isinstance(raw, bytes) else str(raw).strip()
      return float(text)
    except (TypeError, ValueError, UnicodeDecodeError):
      return default
  if isinstance(raw, bytes):
    try:
      return raw.decode("utf-8")
    except UnicodeDecodeError:
      return default
  return str(raw)


def _load_settings_tiers() -> dict[str, int]:
  params_keys = Path(__file__).with_name("params_keys.h")
  if not params_keys.exists():
    return {}

  tiers = {}
  for line in params_keys.read_text(encoding="utf-8", errors="ignore").splitlines():
    if not line.lstrip().startswith('{"'):
      continue
    parts = line.split('"')
    if len(parts) >= 2:
      tiers[parts[1]] = SETTINGS_SIMPLE if "SETTINGS_SIMPLE" in line else SETTINGS_ADVANCED
  return tiers


_SETTINGS_TIERS = _load_settings_tiers()

try:
  from openpilot.common.params_pyx import Params as _Params, ParamKeyFlag, ParamKeyType, UnknownKeyName
except Exception:
  class UnknownKeyName(KeyError):
    pass

  class ParamKeyFlag(IntFlag):
    PERSISTENT = 0x02
    CLEAR_ON_MANAGER_START = 0x04
    CLEAR_ON_ONROAD_TRANSITION = 0x08
    CLEAR_ON_OFFROAD_TRANSITION = 0x10
    DONT_LOG = 0x20
    DEVELOPMENT_ONLY = 0x40
    CLEAR_ON_IGNITION_ON = 0x80
    ALL = 0xFFFFFFFF

  class ParamKeyType(IntEnum):
    STRING = 0
    BOOL = 1
    INT = 2
    FLOAT = 3
    TIME = 4
    JSON = 5
    BYTES = 6

  def _load_key_types() -> dict[str, ParamKeyType]:
    key_types: dict[str, ParamKeyType] = {}
    params_keys = Path(__file__).with_name("params_keys.h")
    if not params_keys.exists():
      return key_types

    for line in params_keys.read_text(encoding="utf-8", errors="ignore").splitlines():
      if not line.lstrip().startswith('{"'):
        continue

      parts = line.split('"')
      if len(parts) < 2:
        continue

      key = parts[1]
      for type_name in ParamKeyType.__members__:
        if f", {type_name}" in line:
          key_types[key] = ParamKeyType[type_name]
          break
      else:
        key_types[key] = ParamKeyType.STRING

    return key_types

  _KEY_TYPES = _load_key_types()
  _PERSISTENT_STORE: dict[str, object] = {}
  _MEMORY_STORE: dict[str, object] = {}

  class Params:
    def __init__(self, d: str | None = None, memory: bool = False, return_defaults: bool = False):
      self.d = d if d is not None else ""
      self.m = memory
      self.return_defaults = return_defaults
      self._store = _MEMORY_STORE if memory else _PERSISTENT_STORE

    def __reduce__(self):
      return type(self), (self.d, self.m, self.return_defaults)

    def clear_all(self, tx_flag=ParamKeyFlag.ALL):
      self._store.clear()

    def check_key(self, key):
      if isinstance(key, bytes):
        key = key.decode("utf-8")
      return str(key)

    def _coerce_bool(self, value) -> bool:
      if isinstance(value, bool):
        return value
      if isinstance(value, (int, float)):
        return bool(value)
      if isinstance(value, bytes):
        return value == b"1"
      if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "none", "null")
      return bool(value)

    def _coerce_value(self, key: str, value):
      value_type = self.get_type(key)
      if value_type == ParamKeyType.BOOL:
        return self._coerce_bool(value)
      if value_type == ParamKeyType.INT:
        return int(float(value))
      if value_type == ParamKeyType.FLOAT:
        return float(value)
      if value_type == ParamKeyType.BYTES and isinstance(value, str):
        return value.encode("utf-8")
      return value

    def get(self, key, block: bool = False, return_default: bool = False, encoding=None, default=None):
      key = self.check_key(key)
      if key in self._store:
        value = self._store[key]
      elif (return_default or (self.return_defaults and not block)) and key in _EXTRA_PERSISTENT_KEY_SPECS:
        return _EXTRA_PERSISTENT_KEY_SPECS[key][1]
      else:
        value = default
      if value is None:
        return default
      if encoding is not None and isinstance(value, bytes):
        try:
          return value.decode(encoding)
        except Exception:
          return value.decode("utf-8", errors="replace")
      return value

    def get_bool(self, key, block: bool = False, default: bool = False):
      value = self.get(key, block=block, return_default=True, default=default)
      return self._coerce_bool(value)

    def get_int(self, key, block: bool = False, return_default: bool = False, default: int = 0):
      value = self.get(key, block=block, return_default=return_default, encoding="utf-8", default=default)
      if value is None or value == "":
        return default
      try:
        return int(float(value))
      except (TypeError, ValueError):
        return default

    def get_float(self, key, block: bool = False, return_default: bool = False, default: float = 0.0):
      value = self.get(key, block=block, return_default=return_default, encoding="utf-8", default=default)
      if value is None or value == "":
        return default
      try:
        return float(value)
      except (TypeError, ValueError):
        return default

    def put(self, key, dat):
      key = self.check_key(key)
      self._store[key] = self._coerce_value(key, dat)

    def put_bool(self, key, val: bool):
      self.put(key, bool(val))

    def put_nonblocking(self, key, dat):
      self.put(key, dat)

    def put_bool_nonblocking(self, key, val: bool):
      self.put_bool(key, val)

    def put_int(self, key, val):
      self.put(key, int(val))

    def put_float(self, key, val):
      self.put(key, float(val))

    def remove(self, key):
      self._store.pop(self.check_key(key), None)

    def get_param_path(self, key: str = ""):
      base = Path(tempfile.gettempdir()) / ("params_memory" if self.m else "params")
      base.mkdir(parents=True, exist_ok=True)
      return str(base / key) if key else str(base)

    def get_type(self, key):
      k = self.check_key(key)
      if k in _KEY_TYPES:
        return _KEY_TYPES[k]
      if k in _EXTRA_PERSISTENT_KEY_SPECS:
        return getattr(ParamKeyType, _EXTRA_PERSISTENT_KEY_SPECS[k][0])
      return ParamKeyType.STRING

    def all_keys(self):
      return list(dict.fromkeys([*_KEY_TYPES, *_EXTRA_PERSISTENT_KEY_SPECS]))

    def get_default_value(self, key):
      k = self.check_key(key)
      if k in _EXTRA_PERSISTENT_KEY_SPECS:
        return _EXTRA_PERSISTENT_KEY_SPECS[k][1]
      return None

    def cpp2python(self, key, value):
      return self._coerce_value(self.check_key(key), value)

    def get_key_flag(self, key):
      return ParamKeyFlag.PERSISTENT

    def get_stock_value(self, key):
      k = self.check_key(key)
      if k in _EXTRA_PERSISTENT_KEY_SPECS:
        return _EXTRA_PERSISTENT_KEY_SPECS[k][2]
      return None

    def get_tuning_level(self, key):
      k = self.check_key(key)
      if k in _EXTRA_PERSISTENT_KEY_SPECS:
        return _EXTRA_PERSISTENT_KEY_SPECS[k][3]
      return 0

    def get_settings_tier(self, key):
      k = self.check_key(key)
      if k in _SETTINGS_TIERS:
        return _SETTINGS_TIERS[k]
      if k in _EXTRA_PERSISTENT_KEY_SPECS:
        return _EXTRA_PERSISTENT_KEY_SPECS[k][4]
      return SETTINGS_ADVANCED

else:
  assert _Params
  assert ParamKeyFlag
  assert ParamKeyType
  assert UnknownKeyName

  class Params(_Params):
    def __init__(self, d="", *args, memory=False, return_defaults=False):
      self._return_defaults = bool(return_defaults or memory)

    def _is_extra_key(self, key) -> str | None:
      k = _normalize_extra_key(key)
      if k not in _EXTRA_PERSISTENT_KEY_SPECS:
        return None
      compiled = _COMPILED_KEY_CACHE.get(k)
      if compiled is None:
        try:
          super().check_key(k)
          compiled = True
        except UnknownKeyName:
          compiled = False
        _COMPILED_KEY_CACHE[k] = compiled
      return None if compiled else k

    def _extra_dir(self) -> Path:
      # Keep extra keys outside d/ so prebuilt C++ clearAll(CLEAR_ON_*) never wipes them.
      base = Path(super().get_param_path())
      return base.parent / f"custom_{base.name}"

    def _extra_read(self, key: str, return_default: bool):
      spec = _EXTRA_PERSISTENT_KEY_SPECS[key]
      try:
        raw = (self._extra_dir() / key).read_bytes()
      except OSError:
        return spec[1] if return_default else None
      if raw == b"":
        return spec[1] if return_default else None
      fallback = spec[1] if return_default else None
      return _decode_extra_value(spec[0], raw, fallback)

    def _extra_write(self, key: str, raw: bytes) -> None:
      custom_dir = self._extra_dir()
      custom_dir.mkdir(parents=True, exist_ok=True)
      fd, tmp_path = tempfile.mkstemp(dir=custom_dir, prefix=f".tmp_{key}_")
      f = None
      try:
        f = os.fdopen(fd, "wb")
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
        f.close()
        f = None
        os.chmod(tmp_path, 0o664)
        os.replace(tmp_path, custom_dir / key)
      except Exception:
        if f is not None:
          f.close()
        else:
          try:
            os.close(fd)
          except OSError:
            pass
        try:
          os.unlink(tmp_path)
        except OSError:
          pass
        raise
      try:
        dir_fd = os.open(custom_dir, os.O_RDONLY)
        try:
          os.fsync(dir_fd)
        finally:
          os.close(dir_fd)
      except OSError:
        pass

    def check_key(self, key):
      extra_key = self._is_extra_key(key)
      if extra_key is not None:
        return extra_key.encode("utf-8")
      return super().check_key(key)

    def get_param_path(self, key=""):
      if key:
        extra_key = self._is_extra_key(key)
        if extra_key is not None:
          return str(self._extra_dir() / extra_key)
      return super().get_param_path(key)

    def remove(self, key):
      extra_key = self._is_extra_key(key)
      if extra_key is not None:
        try:
          (self._extra_dir() / extra_key).unlink()
        except FileNotFoundError:
          pass
        return
      super().remove(key)

    def clear_all(self, tx_flag=ParamKeyFlag.ALL):
      super().clear_all(tx_flag)
      if int(tx_flag) & int(ParamKeyFlag.PERSISTENT):
        custom_dir = self._extra_dir()
        for key in _EXTRA_PERSISTENT_KEY_SPECS:
          try:
            (custom_dir / key).unlink()
          except FileNotFoundError:
            pass

    def get_type(self, key):
      extra_key = self._is_extra_key(key)
      if extra_key is not None:
        return getattr(ParamKeyType, _EXTRA_PERSISTENT_KEY_SPECS[extra_key][0])
      return super().get_type(key)

    def all_keys(self):
      keys = list(super().all_keys())
      seen = set(keys)
      for key in _EXTRA_PERSISTENT_KEY_SPECS:
        key_bytes = key.encode("utf-8")
        if key_bytes not in seen:
          keys.append(key_bytes)
          seen.add(key_bytes)
      return keys

    def get_default_value(self, key):
      extra_key = self._is_extra_key(key)
      if extra_key is not None:
        return _EXTRA_PERSISTENT_KEY_SPECS[extra_key][1]
      return super().get_default_value(key)

    def get_stock_value(self, key):
      extra_key = self._is_extra_key(key)
      if extra_key is not None:
        return _EXTRA_PERSISTENT_KEY_SPECS[extra_key][2]
      return super().get_stock_value(key)

    def get_tuning_level(self, key):
      extra_key = self._is_extra_key(key)
      if extra_key is not None:
        return _EXTRA_PERSISTENT_KEY_SPECS[extra_key][3]
      return super().get_tuning_level(key)

    def get_settings_tier(self, key):
      extra_key = self._is_extra_key(key)
      if extra_key is not None:
        return _EXTRA_PERSISTENT_KEY_SPECS[extra_key][4]
      try:
        return super().get_settings_tier(key)
      except AttributeError:
        return _SETTINGS_TIERS.get(_normalize_extra_key(self.check_key(key)), SETTINGS_ADVANCED)

    def get_key_flag(self, key):
      if self._is_extra_key(key) is not None:
        return ParamKeyFlag.PERSISTENT
      return super().get_key_flag(key)

    def cpp2python(self, key, value):
      extra_key = self._is_extra_key(key)
      if extra_key is not None:
        spec = _EXTRA_PERSISTENT_KEY_SPECS[extra_key]
        return _decode_extra_value(spec[0], value, spec[1])
      return super().cpp2python(key, value)

    def get(self, key, block=False, return_default=False, encoding=None, default=None):
      extra_key = self._is_extra_key(key)
      if extra_key is not None:
        value = self._extra_read(extra_key, return_default=(return_default or (self._return_defaults and not block)))
      else:
        try:
          value = super().get(key, block=block, return_default=return_default)
        except UnknownKeyName:
          return default
      if value is None:
        return default
      if encoding is not None and isinstance(value, bytes):
        try:
          return value.decode(encoding)
        except Exception:
          return value.decode("utf-8", errors="replace")
      return value

    def get_bool(self, key, block=False, default=False):
      extra_key = self._is_extra_key(key)
      if extra_key is not None:
        result = self._extra_read(extra_key, return_default=True)
        return bool(default) if result is None else bool(result)
      try:
        result = super().get(key, block=block, return_default=True)
        if result is None:
          return bool(default)
        return bool(result)
      except UnknownKeyName:
        return bool(default)

    def put(self, key, dat):
      extra_key = self._is_extra_key(key)
      if extra_key is not None:
        self._extra_write(extra_key, _encode_extra_value(_EXTRA_PERSISTENT_KEY_SPECS[extra_key][0], dat))
        return
      super().put(key, dat)

    def put_bool(self, key, val):
      extra_key = self._is_extra_key(key)
      if extra_key is not None:
        self._extra_write(extra_key, b"1" if val else b"0")
        return
      super().put_bool(key, val)

    def put_nonblocking(self, key, dat):
      extra_key = self._is_extra_key(key)
      if extra_key is not None:
        self._extra_write(extra_key, _encode_extra_value(_EXTRA_PERSISTENT_KEY_SPECS[extra_key][0], dat))
        return
      super().put_nonblocking(key, dat)

    def put_bool_nonblocking(self, key, val):
      extra_key = self._is_extra_key(key)
      if extra_key is not None:
        self._extra_write(extra_key, b"1" if val else b"0")
        return
      super().put_bool_nonblocking(key, val)

    def get_int(self, key, block=False, return_default=False, default=0):
      val = self.get(key, block=block, return_default=return_default, encoding="utf-8")
      if val is None or val == "":
        return default
      try:
        return int(float(val))
      except ValueError:
        return default

    def get_float(self, key, block=False, return_default=False, default=0.0):
      val = self.get(key, block=block, return_default=return_default, encoding="utf-8")
      if val is None or val == "":
        return default
      try:
        return float(val)
      except ValueError:
        return default

    def put_int(self, key, val):
      t = self.get_type(key)
      if t == ParamKeyType.FLOAT:
        self.put(key, float(val))
      elif t == ParamKeyType.INT:
        self.put(key, int(val))
      elif t == ParamKeyType.BOOL:
        self.put(key, bool(val))
      else:
        self.put(key, str(int(val)))

    def put_float(self, key, val):
      t = self.get_type(key)
      if t == ParamKeyType.FLOAT:
        self.put(key, float(val))
      elif t == ParamKeyType.INT:
        self.put(key, int(val))
      elif t == ParamKeyType.BOOL:
        self.put(key, bool(val))
      else:
        self.put(key, str(float(val)))


if __name__ == "__main__":
  import sys

  params = Params()
  key = sys.argv[1]
  assert params.check_key(key), f"unknown param: {key}"

  if len(sys.argv) == 3:
    val = sys.argv[2]
    print(f"SET: {key} = {val}")
    params.put(key, val)
  elif len(sys.argv) == 2:
    print(f"GET: {key} = {params.get(key)}")
