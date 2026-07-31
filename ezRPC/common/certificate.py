"""Self-signed certificate generation for local/development use.

QUIC always encrypts, so the server always needs a certificate. When none is
supplied, one is generated here for ``localhost``/``127.0.0.1``. This is for
development only — production should pass a real certificate to the Receiver.
"""

import datetime
import ipaddress
import os

from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_RSA_PUBLIC_EXPONENT = 65537                        # the standard RSA exponent (F4)
_RSA_KEY_SIZE = 2048
_VALIDITY = datetime.timedelta(days=3650)           # dev cert: ~10 years
_BACKDATE = datetime.timedelta(days=1)              # tolerate clock skew across machines


def generate_self_signed_cert(cert_path: str, key_path: str, *, force: bool = False) -> None:
    """Write a self-signed certificate and private key to the given paths.

    Skips generation if both files already exist (unless ``force``)."""
    if not force and os.path.exists(cert_path) and os.path.exists(key_path):
        return

    key = rsa.generate_private_key(public_exponent=_RSA_PUBLIC_EXPONENT, key_size=_RSA_KEY_SIZE)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _BACKDATE)
        .not_valid_after(now + _VALIDITY)
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                x509.IPAddress(ipaddress.ip_address("::1")),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    with open(key_path, "wb") as f:
        f.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
