#!/usr/bin/env python3
"""Gera uma CA doméstica e certificado HTTPS para o Nacho."""

import argparse
import ipaddress
import os
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def discover_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def write_private(path, key):
    path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    os.chmod(path, 0o600)


def main():
    parser = argparse.ArgumentParser(description="Configura HTTPS local do Nacho")
    parser.add_argument("--ip", default=None, help="IP LAN do servidor")
    parser.add_argument("--out", default="certs")
    args = parser.parse_args()
    ip = ipaddress.ip_address(args.ip or discover_ip())
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)

    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                                             "Sentinela Nacho Home CA")])
    ca_cert = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
               .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now - timedelta(minutes=5))
               .not_valid_after(now + timedelta(days=3650))
               .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
               .add_extension(x509.KeyUsage(
                   digital_signature=True, key_encipherment=False, key_cert_sign=True,
                   key_agreement=False, content_commitment=False, data_encipherment=False,
                   encipher_only=False, decipher_only=False, crl_sign=True), critical=True)
               .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
                              critical=False)
               .sign(ca_key, hashes.SHA256()))

    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Nacho")])
    sans = [x509.IPAddress(ip), x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            x509.DNSName("localhost"), x509.DNSName("nacho.local"),
            x509.DNSName(socket.gethostname())]
    server_cert = (x509.CertificateBuilder().subject_name(server_name).issuer_name(ca_name)
                   .public_key(server_key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(minutes=5))
                   .not_valid_after(now + timedelta(days=825))
                   .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                   .add_extension(x509.SubjectAlternativeName(sans), critical=False)
                   .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                                  critical=False)
                   .add_extension(x509.KeyUsage(
                       digital_signature=True, key_encipherment=True, key_cert_sign=False,
                       key_agreement=False, content_commitment=False, data_encipherment=False,
                       encipher_only=False, decipher_only=False, crl_sign=False), critical=True)
                   .sign(ca_key, hashes.SHA256()))

    write_private(out / "nacho-ca.key", ca_key)
    write_private(out / "nacho-server.key", server_key)
    (out / "nacho-ca.crt").write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    (out / "nacho-server.crt").write_bytes(
        server_cert.public_bytes(serialization.Encoding.PEM))
    print(f"[ok] Certificados criados em {out.resolve()}")
    print(f"[ok] Nacho: https://{ip}:8002")
    print(f"[ok] CA para o iPhone: http://{ip}:8001/nacho-ca.crt")


if __name__ == "__main__":
    main()
