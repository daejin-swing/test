import os

PARAMS_DIR = "/data/swing/params/d"

class Params:
    def __init__(self):
        os.makedirs(PARAMS_DIR, exist_ok=True)

    def get(self, key: str, **kwargs) -> bytes:
        path = os.path.join(PARAMS_DIR, key)
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            return f.read()

    def get_bool(self, key: str) -> bool:
        val = self.get(key)
        return val == b"1"

    def put(self, key: str, val: str, **kwargs):
        path = os.path.join(PARAMS_DIR, key)
        with open(path, "wb") as f:
            f.write(val.encode() if isinstance(val, str) else val)

    def put_bool(self, key: str, val: bool):
        self.put(key, "1" if val else "0")