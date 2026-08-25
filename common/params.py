import os
import datetime

def get_default_params_dir() -> str:
    if os.getenv("PARAMS_DIR"):
        return os.getenv("PARAMS_DIR")
    if os.path.exists("/data") and os.access("/data", os.W_OK):
        return "/data/swing/params/d"
    # Fallback to local workspace directory
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_dir, ".data", "params", "d")

PARAMS_DIR = get_default_params_dir()

class Params:
    def __init__(self):
        os.makedirs(PARAMS_DIR, exist_ok=True)

    def _path(self, key: str) -> str:
        return os.path.join(PARAMS_DIR, key)

    def _read_raw(self, key: str) -> bytes | None:
        path = self._path(key)
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            return f.read()

    def get(self, key: str, return_default: bool = False, **kwargs):
        raw = self._read_raw(key)
        if raw is None:
            return 0 if return_default else None
        text = raw.decode()
        for parser in (int, float, datetime.datetime.fromisoformat):
            try:
                return parser(text)
            except ValueError:
                continue
        return text

    def get_bool(self, key: str) -> bool:
        return self._read_raw(key) == b"1"

    def put(self, key: str, val, **kwargs):
        if isinstance(val, bytes):
            data = val
        elif isinstance(val, datetime.datetime):
            data = val.isoformat().encode()
        else:
            data = str(val).encode()
        with open(self._path(key), "wb") as f:
            f.write(data)

    def put_bool(self, key: str, val: bool, **kwargs):
        self.put(key, "1" if val else "0")

    def remove(self, key: str):
        path = self._path(key)
        if os.path.exists(path):
            os.remove(path)
