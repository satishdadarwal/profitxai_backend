# apps/brokers/encryption.py
import base64
from cryptography.fernet import Fernet, MultiFernet
from django.conf import settings
from django.db import models


def _get_fernet() -> MultiFernet:
    """
    settings.FERNET_KEYS = ['key1', 'key2', ...]
    Pehli key se encrypt hoga, saari keys se decrypt hoga (key rotation support).
    """
    keys = getattr(settings, "FERNET_KEYS", [])
    if not keys:
        raise ValueError("FERNET_KEYS not set in settings.py")
    return MultiFernet([Fernet(k.encode() if isinstance(k, str) else k) for k in keys])


def encrypt_value(plain: str) -> str:
    if not plain:
        return plain
    return _get_fernet().encrypt(plain.encode()).decode()


def decrypt_value(cipher: str) -> str:
    if not cipher:
        return cipher
    return _get_fernet().decrypt(cipher.encode()).decode()


class EncryptedCharField(models.TextField):
    """
    Transparent Fernet encryption — DB mein ciphertext, Python mein plaintext.
    Django 4.x / 5.x compatible.
    """

    def from_db_value(self, value, expression, connection):
        if value is None or value == "":
            return value
        try:
            return decrypt_value(value)
        except Exception:
            return value  # already plain (migration ke baad pehli baar)

    def get_prep_value(self, value):
        if value is None or value == "":
            return value
        # Pehle se encrypted hai toh dobara encrypt mat karo
        try:
            decrypt_value(value)
            return value  # already encrypted
        except Exception:
            return encrypt_value(value)