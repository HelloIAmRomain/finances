"""Clé de sécurité logicielle pour les tests (soft-webauthn), aux formats JSON du navigateur."""

import json
from typing import Any

from soft_webauthn import SoftWebauthnDevice
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url


def _b64(value: Any) -> str:
    return bytes_to_base64url(value if isinstance(value, bytes) else bytes(value))


class SoftKey:
    """Enveloppe `SoftWebauthnDevice` : options JSON du serveur → réponse JSON du navigateur."""

    def __init__(self) -> None:
        self.device = SoftWebauthnDevice()

    def create(self, options_json: str, origin: str) -> dict[str, Any]:
        options = json.loads(options_json)
        public_key = {
            **options,
            "challenge": base64url_to_bytes(options["challenge"]),
            "user": {**options["user"], "id": base64url_to_bytes(options["user"]["id"])},
        }
        raw = self.device.create({"publicKey": public_key}, origin)
        return {
            "id": _b64(raw["rawId"]),
            "rawId": _b64(raw["rawId"]),
            "type": "public-key",
            "response": {
                "clientDataJSON": _b64(raw["response"]["clientDataJSON"]),
                "attestationObject": _b64(raw["response"]["attestationObject"]),
            },
        }

    def get(self, options_json: str, origin: str) -> dict[str, Any]:
        options = json.loads(options_json)
        public_key = {**options, "challenge": base64url_to_bytes(options["challenge"])}
        raw = self.device.get({"publicKey": public_key}, origin)
        response = {
            "clientDataJSON": _b64(raw["response"]["clientDataJSON"]),
            "authenticatorData": _b64(raw["response"]["authenticatorData"]),
            "signature": _b64(raw["response"]["signature"]),
        }
        if raw["response"].get("userHandle"):
            response["userHandle"] = _b64(raw["response"]["userHandle"])
        return {
            "id": _b64(raw["rawId"]),
            "rawId": _b64(raw["rawId"]),
            "type": "public-key",
            "response": response,
        }
