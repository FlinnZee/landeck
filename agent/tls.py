from __future__ import annotations

import datetime as dt
import ipaddress
import json
import plistlib
import uuid
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

# iOS rejects leaf certs that live longer than 825 days
LEAF_DAYS = 800
CA_DAYS = 3650
RENEW_BEFORE = dt.timedelta(days=30)

_PRIVATE_NETS = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16"]


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _fp(cert: x509.Certificate) -> str:
    return cert.fingerprint(hashes.SHA256()).hex(":").upper()


class CertManager:
    """A small local CA plus a server cert it signs.

    The CA is name-constrained to .local names, this hostname and private IP
    ranges, so trusting it on a phone can't be abused to impersonate real sites.
    """

    def __init__(self, folder: Path, hostname: str):
        self.folder = folder
        self.hostname = hostname.lower()
        folder.mkdir(parents=True, exist_ok=True)
        self.ca_cert_path = folder / "ca.pem"
        self.ca_key_path = folder / "ca.key"
        self.cert_path = folder / "server.pem"
        self.key_path = folder / "server.key"
        self.meta_path = folder / "meta.json"

    # --- public -----------------------------------------------------------

    def ensure(self, ips: list[str]) -> bool:
        """Make sure a valid cert covering these IPs exists. Returns True if it was (re)issued."""
        names = sorted({"localhost", self.hostname, f"{self.hostname}.local", "landeck.local"})
        ip_set = sorted(set(ips) | {"127.0.0.1"})
        ca_cert, ca_key = self._ensure_ca()

        meta = self._read_meta()
        if (
            self.cert_path.exists()
            and self.key_path.exists()
            and meta.get("names") == names
            and meta.get("ips") == ip_set
            and meta.get("ca") == _fp(ca_cert)
        ):
            cert = x509.load_pem_x509_certificate(self.cert_path.read_bytes())
            if cert.not_valid_after_utc - _utcnow() > RENEW_BEFORE:
                return False

        self._issue(ca_cert, ca_key, names, ip_set)
        self.meta_path.write_text(json.dumps({"names": names, "ips": ip_set, "ca": _fp(ca_cert)}), "utf-8")
        return True

    def fingerprint(self) -> str:
        return _fp(x509.load_pem_x509_certificate(self.cert_path.read_bytes()))

    def ca_fingerprint(self) -> str:
        return _fp(x509.load_pem_x509_certificate(self.ca_cert_path.read_bytes()))

    def ca_der(self) -> bytes:
        cert = x509.load_pem_x509_certificate(self.ca_cert_path.read_bytes())
        return cert.public_bytes(serialization.Encoding.DER)

    def sans(self) -> dict[str, list[str]]:
        meta = self._read_meta()
        return {"names": meta.get("names", []), "ips": meta.get("ips", [])}

    def mobileconfig(self, display: str) -> bytes:
        """Apple configuration profile that installs the local CA."""
        fp = self.ca_fingerprint()
        inner_id = uuid.uuid5(uuid.NAMESPACE_URL, "landeck-ca:" + fp)
        outer_id = uuid.uuid5(uuid.NAMESPACE_URL, "landeck-profile:" + fp)
        profile = {
            "PayloadContent": [{
                "PayloadCertificateFileName": "landeck-ca.cer",
                "PayloadContent": self.ca_der(),
                "PayloadDescription": "Lets this iPhone trust the LanDeck agent on your PC.",
                "PayloadDisplayName": f"LanDeck CA ({self.hostname})",
                "PayloadIdentifier": f"local.landeck.ca.{inner_id}",
                "PayloadType": "com.apple.security.root",
                "PayloadUUID": str(inner_id).upper(),
                "PayloadVersion": 1,
            }],
            "PayloadDisplayName": display,
            "PayloadDescription": "Installs the local certificate authority used by LanDeck. "
                                  "It can only vouch for .local names and private network addresses.",
            "PayloadIdentifier": f"local.landeck.profile.{outer_id}",
            "PayloadOrganization": "LanDeck",
            "PayloadRemovalDisallowed": False,
            "PayloadType": "Configuration",
            "PayloadUUID": str(outer_id).upper(),
            "PayloadVersion": 1,
        }
        return plistlib.dumps(profile)

    # --- internals --------------------------------------------------------

    def _read_meta(self) -> dict:
        try:
            return json.loads(self.meta_path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def _ensure_ca(self) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
        if self.ca_cert_path.exists() and self.ca_key_path.exists():
            cert = x509.load_pem_x509_certificate(self.ca_cert_path.read_bytes())
            key = serialization.load_pem_private_key(self.ca_key_path.read_bytes(), password=None)
            if cert.not_valid_after_utc - _utcnow() > RENEW_BEFORE and self._ca_covers_host(cert):
                return cert, key  # type: ignore[return-value]

        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, f"LanDeck Local CA ({self.hostname})"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "LanDeck"),
        ])
        permitted: list[x509.GeneralName] = [
            x509.DNSName("local"),
            x509.DNSName("localhost"),
            x509.DNSName(self.hostname),
        ]
        permitted += [x509.IPAddress(ipaddress.ip_network(n)) for n in _PRIVATE_NETS]
        now = _utcnow()
        ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=CA_DAYS))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(
                digital_signature=True, key_cert_sign=True, crl_sign=True,
                content_commitment=False, key_encipherment=False, data_encipherment=False,
                key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.NameConstraints(permitted_subtrees=permitted, excluded_subtrees=None),
                           critical=True)
            .add_extension(ski, critical=False)
            .sign(key, hashes.SHA256())
        )
        self.ca_key_path.write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        self.ca_cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        # a new CA invalidates the old leaf
        self.meta_path.unlink(missing_ok=True)
        return cert, key

    def _ca_covers_host(self, cert: x509.Certificate) -> bool:
        try:
            nc = cert.extensions.get_extension_for_class(x509.NameConstraints).value
        except x509.ExtensionNotFound:
            return False
        dns = {n.value for n in (nc.permitted_subtrees or []) if isinstance(n, x509.DNSName)}
        return self.hostname in dns

    def _issue(self, ca_cert: x509.Certificate, ca_key: ec.EllipticCurvePrivateKey,
               names: list[str], ips: list[str]) -> None:
        key = ec.generate_private_key(ec.SECP256R1())
        now = _utcnow()
        alt: list[x509.GeneralName] = [x509.DNSName(n) for n in names]
        alt += [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, self.hostname)]))
            .issuer_name(ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=LEAF_DAYS))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(
                digital_signature=True, key_cert_sign=False, crl_sign=False,
                content_commitment=False, key_encipherment=False, data_encipherment=False,
                key_agreement=True, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                           critical=False)
            .sign(ca_key, hashes.SHA256())
        )
        self.key_path.write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        # full chain so clients that trust the CA can build the path
        self.cert_path.write_bytes(
            cert.public_bytes(serialization.Encoding.PEM) + ca_cert.public_bytes(serialization.Encoding.PEM))
