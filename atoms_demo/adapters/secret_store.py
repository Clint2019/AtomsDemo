"""Encrypted local SecretStore; key is supplied by the process environment."""
import os
import hashlib
import hmac
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken

from atoms_demo.ports.runtime import SecretStore
from atoms_demo.config import Settings, database_path
from atoms_demo.core.types import EntityId, Scope, SecretRef
from atoms_demo.adapters.persistence import connect, migrate


class EncryptedSecretStore:
    def __init__(self, path, key: str):
        try:
            self.cipher = Fernet(key.encode("ascii"))
        except (ValueError, TypeError) as exc:
            raise ValueError("ATOMS_SECRET_ENCRYPTION_KEY is invalid") from exc
        self.path = path
        self.fingerprint_key = key.encode("ascii")
        migrate(path)

    def fingerprint(self, value: str) -> str:
        return hmac.new(self.fingerprint_key,
            b"secret-idempotency-v1\0" + value.encode("utf-8"), hashlib.sha256).hexdigest()

    async def put(self, scope: Scope, value: str) -> SecretRef:
        if not value:
            raise ValueError("Secret value is empty")
        secret_id = EntityId(uuid4().hex)
        with connect(self.path) as db:
            db.execute("INSERT INTO secrets(id,workspace_id,project_id,ciphertext) VALUES (?,?,?,?)",
                       (str(secret_id), str(scope.workspace_id) if scope.workspace_id else None,
                        str(scope.project_id) if scope.project_id else None, self.cipher.encrypt(value.encode("utf-8"))))
        return SecretRef(id=secret_id)

    async def resolve(self, reference: SecretRef) -> str:
        with connect(self.path) as db:
            row = db.execute("SELECT ciphertext FROM secrets WHERE id=? AND revoked=0", (str(reference.id),)).fetchone()
        if row is None:
            raise KeyError("Secret reference unavailable")
        try:
            return self.cipher.decrypt(row["ciphertext"]).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("Secret decryption failed") from exc

    async def revoke(self, reference: SecretRef) -> None:
        with connect(self.path) as db:
            db.execute("UPDATE secrets SET revoked=1,ciphertext=? WHERE id=?",
                       (b"", str(reference.id)))


def create_adapter(settings: Settings) -> SecretStore:
    key = os.environ.get("ATOMS_SECRET_ENCRYPTION_KEY")
    if not key:
        raise ValueError("Missing configuration: ATOMS_SECRET_ENCRYPTION_KEY")
    return EncryptedSecretStore(database_path(settings), key)
