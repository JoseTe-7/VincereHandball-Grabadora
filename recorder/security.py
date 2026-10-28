"""Certificado HTTPS local para el modo LAN (certificado autofirmado)."""
from __future__ import annotations
import ipaddress, socket, ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path

def local_addresses() -> list[str]:
    found = {"127.0.0.1"}
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = item[4][0]
            if ipaddress.ip_address(ip).is_private: found.add(ip)
    except OSError: pass
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("192.0.2.1", 80)); ip = sock.getsockname()[0]; sock.close()
        if ipaddress.ip_address(ip).is_private: found.add(ip)
    except OSError: pass
    return sorted(found)

def tls_context() -> ssl.SSLContext:
    """Return a stable local TLS identity, regenerating only when it is stale.

    Previously this routine created a new self-signed certificate on every launch.
    Tablets that had accepted/trusted the previous certificate then saw a new
    fingerprint and could refuse camera access because getUserMedia requires a
    secure context. Keep the same key/certificate while valid and while its SANs
    cover the current local addresses.
    """
    import ipaddress
    from datetime import datetime, timedelta, timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    folder = Path.home() / ".handball_recorder"
    folder.mkdir(parents=True, exist_ok=True)
    try:
        folder.chmod(0o700)
    except OSError:
        pass
    cert_path, key_path = folder / "lan-cert.pem", folder / "lan-key.pem"

    addresses = set()
    for ip in local_addresses():
        try:
            addresses.add(ipaddress.ip_address(ip))
        except ValueError:
            continue
    expected_dns = {"localhost", "extremos.local"}

    # Reuse the cert only if the private key matches and all advertised LAN IPs
    # are covered. On a network change, issue a new cert with updated SAN entries.
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        cert_ips = set(san.get_values_for_type(x509.IPAddress))
        cert_dns = set(san.get_values_for_type(x509.DNSName))
        cert_key = cert.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        private_public = key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        now = datetime.now(timezone.utc)
        not_after = cert.not_valid_after_utc if hasattr(cert, "not_valid_after_utc") else cert.not_valid_after.replace(tzinfo=timezone.utc)
        not_before = cert.not_valid_before_utc if hasattr(cert, "not_valid_before_utc") else cert.not_valid_before.replace(tzinfo=timezone.utc)
        if (addresses.issubset(cert_ips) and expected_dns.issubset(cert_dns)
                and cert_key == private_public and not_before <= now < not_after - timedelta(days=7)):
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(str(cert_path), str(key_path))
            return context
    except Exception:
        pass

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    names = [x509.DNSName("localhost"), x509.DNSName("extremos.local")]
    names.extend(x509.IPAddress(ip) for ip in sorted(addresses, key=lambda value: (value.version, int(value))))
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Extremos local")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    try:
        key_path.chmod(0o600)
        cert_path.chmod(0o600)
    except OSError:
        pass
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(cert_path), str(key_path))
    return context

