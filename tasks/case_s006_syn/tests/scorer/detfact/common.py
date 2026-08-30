import hashlib
import json
import os


def canonical_bytes(doc):
    return json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_doc(doc):
    return hashlib.sha256(canonical_bytes(doc)).hexdigest()


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path):
    with open(path) as f:
        return json.load(f)


def atomic_write_text(path, text):
    """Write via temp file + os.replace so a concurrently-serving HTTP server
    never reads a half-written file (Content-Length mismatch)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp." + str(os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def write_json(path, doc):
    atomic_write_text(path, json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
