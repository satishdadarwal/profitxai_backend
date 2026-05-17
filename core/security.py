import json
import logging
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

logger = logging.getLogger(__name__)


class CredentialEncryption:
    """
    Secure encryption/decryption for broker credentials.
    Uses Fernet symmetric encryption.
    """

    REQUIRED_KEYS = {"api_key", "api_secret", "access_token"}

    def __init__(self):
        key = getattr(settings, "ENCRYPTION_KEY", None)

        if not key:
            raise ValueError("ENCRYPTION_KEY not set in settings")

        # Ensure bytes
        if isinstance(key, str):
            key = key.encode()

        try:
            self.cipher = Fernet(key)
        except Exception as e:
            raise ValueError("Invalid ENCRYPTION_KEY format") from e

    # ─────────────────────────────────────────────
    # Encrypt
    # ─────────────────────────────────────────────
    def encrypt(self, data: dict) -> str:
        """
        Encrypt credentials dict → string
        """
        if not isinstance(data, dict):
            raise ValueError("Credentials must be a dictionary")

        missing = self.REQUIRED_KEYS - data.keys()
        if missing:
            raise ValueError(f"Missing required credential keys: {missing}")

        try:
            json_str = json.dumps(data)
            encrypted = self.cipher.encrypt(json_str.encode())
            return encrypted.decode()
        except Exception as e:
            logger.error("Credential encryption failed: %s", e)
            raise

    # ─────────────────────────────────────────────
    # Decrypt
    # ─────────────────────────────────────────────
    def decrypt(self, encrypted_str: str) -> dict:
        """
        Decrypt string → credentials dict
        """
        if not encrypted_str:
            raise ValueError("Empty encrypted credentials")

        try:
            decrypted = self.cipher.decrypt(encrypted_str.encode())
            return json.loads(decrypted.decode())

        except InvalidToken:
            logger.error("Invalid encryption token (key mismatch or corrupted data)")
            raise ValueError("Invalid or corrupted encrypted credentials")

        except Exception as e:
            logger.error("Credential decryption failed: %s", e)
            raise


# ─────────────────────────────────────────────
# Singleton (recommended usage)
# ─────────────────────────────────────────────
_encryptor_instance = None


def get_encryptor() -> CredentialEncryption:
    global _encryptor_instance
    if _encryptor_instance is None:
        _encryptor_instance = CredentialEncryption()
    return _encryptor_instance


# ─────────────────────────────────────────────
# Backward compatibility (IMPORTANT)
# ─────────────────────────────────────────────

def encrypt_credentials(data: dict) -> str:
    return get_encryptor().encrypt(data)


def decrypt_credentials(encrypted_str: str) -> dict:
    return get_encryptor().decrypt(encrypted_str)