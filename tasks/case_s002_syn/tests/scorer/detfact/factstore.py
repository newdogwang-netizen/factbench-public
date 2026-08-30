import copy
import json
import os
import tempfile

from detfact.common import canonical_bytes, read_json
from detfact.schema import ValidationError, validate_factset


class FactstoreError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _clean_part(value, name):
    value = str(value or "").strip()
    if not value or "/" in value or value in {".", ".."}:
        raise FactstoreError(400, "bad_" + name, "invalid " + name)
    return value


def factset_path(root, factset_id, version):
    fid = _clean_part(factset_id, "factset_id")
    ver = _clean_part(version, "version")
    return os.path.join(root, fid, ver + ".json")


def resolve_factset_path(root, factset_id, version):
    direct = factset_path(root, factset_id, version)
    if os.path.isfile(direct):
        return direct
    legacy = os.path.join(root, _clean_part(factset_id, "factset_id"), "v" + _clean_part(version, "version") + ".json")
    if os.path.isfile(legacy):
        return legacy
    raise FactstoreError(404, "factset_not_found", "factset version not found")


def validate_factset_id(doc, factset_id=None, version=None):
    try:
        validate_factset(doc)
    except ValidationError as exc:
        raise FactstoreError(400, exc.code, exc.message) from exc
    meta = doc.get("factset") or {}
    if not meta.get("id") or not meta.get("version"):
        raise FactstoreError(400, "bad_factset_meta", "factset.id and factset.version are required")
    if factset_id is not None and str(meta.get("id")) != str(factset_id):
        raise FactstoreError(409, "factset_id_mismatch", "URL factset id does not match document")
    if version is not None and str(meta.get("version")) != str(version):
        raise FactstoreError(409, "factset_version_mismatch", "URL version does not match document")
    if not isinstance(doc.get("facts"), list):
        raise FactstoreError(400, "bad_facts", "facts must be a list")


def put_factset(root, doc, factset_id=None, version=None):
    validate_factset_id(doc, factset_id=factset_id, version=version)
    meta = doc["factset"]
    path = factset_path(root, meta["id"], meta["version"])
    body = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    if os.path.exists(path):
        with open(path) as f:
            old = f.read()
        if old != body:
            raise FactstoreError(409, "immutable_conflict", "factset version already exists with different bytes")
        return {"path": path, "changed": False, "sha256": meta.get("sha256")}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=os.path.dirname(path), text=True)
    except OSError as exc:
        raise FactstoreError(503, "factstore_not_writable", "cannot write factstore: " + str(exc)) from exc
    try:
        try:
            with os.fdopen(fd, "w") as f:
                f.write(body)
            os.replace(tmp, path)
        except OSError as exc:
            raise FactstoreError(503, "factstore_not_writable", "cannot write factstore: " + str(exc)) from exc
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return {"path": path, "changed": True, "sha256": meta.get("sha256")}


def load_factset(root, factset_id, version):
    return read_json(resolve_factset_path(root, factset_id, version))


def factset_meta(doc):
    out = copy.deepcopy(doc)
    facts = out.pop("facts", [])
    out.setdefault("selection", {})
    out["selection"]["fact_count"] = len(facts)
    out["etag"] = (out.get("factset") or {}).get("sha256")
    out["byte_sha256"] = None
    try:
        out["byte_sha256"] = __import__("hashlib").sha256(canonical_bytes(doc)).hexdigest()
    except Exception:
        pass
    return out
