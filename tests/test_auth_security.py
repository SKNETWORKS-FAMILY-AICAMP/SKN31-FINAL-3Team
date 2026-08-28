import json
import os
import unittest


os.environ.setdefault("JWT_SECRET", "test-secret-that-is-at-least-thirty-two-characters")
os.environ.setdefault("SESSION_ENCRYPTION_KEY", "separate-test-session-key")

from auth_service.config import get_auth_settings
from auth_service.security import (
    create_access_token,
    decode_access_token,
    decrypt_erp_sid,
    encrypt_erp_sid,
    generate_refresh_token,
    hash_refresh_token,
)


class AuthSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        get_auth_settings.cache_clear()

    def test_access_token_contains_session_id_but_not_erp_sid(self):
        user = {
            "id": "764fef76-40f8-42cf-abed-ceb4d9655831",
            "erp_user_id": "Administrator",
            "email": "",
            "username": "Administrator",
            "full_name": "Administrator",
            "user_type": "System User",
        }
        encoded, expires_in = create_access_token(
            user,
            "7ec86068-18cf-46e6-8602-d20aec808a75",
        )
        payload = decode_access_token(encoded)

        self.assertEqual(payload["erp_user_id"], "Administrator")
        self.assertEqual(payload["token_type"], "access_token")
        self.assertEqual(
            payload["session_id"],
            "7ec86068-18cf-46e6-8602-d20aec808a75",
        )
        self.assertNotIn("erp_sid", json.dumps(payload))
        self.assertGreater(expires_in, 0)

    def test_refresh_tokens_are_opaque_and_hashable(self):
        first = generate_refresh_token()
        second = generate_refresh_token()

        self.assertNotEqual(first, second)
        self.assertEqual(len(hash_refresh_token(first)), 64)
        self.assertNotEqual(hash_refresh_token(first), hash_refresh_token(second))

    def test_erp_sid_encryption_round_trip(self):
        sid = "example-erp-session-id"
        ciphertext = encrypt_erp_sid(sid)

        self.assertNotEqual(ciphertext, sid)
        self.assertEqual(decrypt_erp_sid(ciphertext), sid)


if __name__ == "__main__":
    unittest.main()
