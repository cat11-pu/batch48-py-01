"""Function level binary fingerprint index tolerating compiler drift."""

import threading


class FingerprintError(ValueError):
    """Raised for every rejected indexing or matching operation."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class BinaryFingerprintIndex:
    """Index function features and match unknown blobs against them."""

    def __init__(self, *, max_functions=50000, max_function_bytes=1048576,
                 max_input_bytes=67108864):
        self.max_functions = max_functions
        self.max_function_bytes = max_function_bytes
        self.max_input_bytes = max_input_bytes
        self._lock = threading.RLock()

    def add_function(self, name, code, strings):
        raise NotImplementedError

    def match(self, blob, threshold=0.5):
        raise NotImplementedError

    def explain(self, match_id):
        raise NotImplementedError

    def prune(self, threshold):
        raise NotImplementedError

    def annotate(self, name, library, version):
        raise NotImplementedError

    def versions(self, name):
        raise NotImplementedError

    def features(self, name):
        raise NotImplementedError

    def functions(self):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, state):
        raise NotImplementedError
