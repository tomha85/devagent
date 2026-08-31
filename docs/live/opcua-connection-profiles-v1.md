# DevAgent Live OPC UA Connection Profiles V1

DevAgent Live is a **read-only OPC UA client**. The PLC/server administrator remains responsible for configuring the OPC UA server, endpoint, accounts, certificates, trust lists, firewall/network access, and permissions. DevAgent does not modify the PLC/server to establish access.

An engineer can reuse the same OPC UA connection information already known from SCADA or another OPC UA client and provide the equivalent values to DevAgent.

## Supported user identities

| User identity | DevAgent status | CLI |
| --- | --- | --- |
| Anonymous | `SUPPORTED` | no identity flags |
| UserName / password on `Sign` or `SignAndEncrypt` | `SUPPORTED` | `--username`, `--password-env` |
| UserName / password on `NoSecurity` | `BLOCKED_BY_DEFAULT`; explicit legacy/customer compatibility opt-in available | `--username`, `--password-env`, `--allow-insecure-username-password` |
| X.509 user certificate | `SUPPORTED` | `--user-certificate`, optional separate `--user-private-key`, optional `--user-private-key-password-env` |
| IssuedToken / JWT | `RUNTIME_UNAVAILABLE` with the supported `asyncua>=2,<3` runtime | detected by `devagent live probe`; connection fails closed |

Passwords are accepted only through environment-variable references and are never accepted as literal CLI arguments.

DevAgent does **not** silently downgrade to a NoSecurity username/password profile. If a customer's existing OPC UA server intentionally uses that profile, the operator must opt in explicitly with `--allow-insecure-username-password`. When a supported secure profile is also advertised, `probe` continues to prefer the secure profile.

## Secure-channel policies

| OPC UA security policy | DevAgent status |
| --- | --- |
| `Basic128Rsa15` | `DEPRECATED_COMPATIBILITY` |
| `Basic256` | `DEPRECATED_COMPATIBILITY` |
| `Basic256Sha256` | `SUPPORTED` |
| `Aes128_Sha256_RsaOaep` / `Aes128Sha256RsaOaep` | `SUPPORTED` |
| `Aes256_Sha256_RsaPss` / `Aes256Sha256RsaPss` | `SUPPORTED` |
| `ECC_nistP256` | `RUNTIME_UNAVAILABLE` |
| `ECC_nistP384` | `RUNTIME_UNAVAILABLE` |
| `ECC_brainpoolP256r1` | `RUNTIME_UNAVAILABLE` |
| `ECC_brainpoolP384r1` | `RUNTIME_UNAVAILABLE` |
| `ECC_curve25519` | `RUNTIME_UNAVAILABLE` |

Supported secure-channel modes are `Sign` and `SignAndEncrypt`.

The deprecated Basic128/Basic256 policies are exposed only for compatibility with older installed OPC UA servers. Prefer a modern supported policy when the server offers one.

ECC profiles are recognized so `probe` can explain them accurately, but DevAgent does not claim a connection path that its pinned runtime cannot implement.

## Certificate and key input formats

DevAgent accepts the common certificate formats customers encounter in Ignition, Windows/enterprise PKI, PLC engineering tools, and OPC UA deployments.

| Input | Supported formats | Behavior |
| --- | --- | --- |
| Client application certificate | `.der`, `.pem`, `.cer`, `.crt`, `.pfx`, `.p12` | `.cer/.crt` encoding is detected from content; `.pfx/.p12` may contain both certificate and private key |
| Client application private key | `.der`, `.pem`, PKCS#12 key material | separate key is not needed when the client certificate is a `.pfx/.p12` bundle |
| Pinned server certificate | `.der`, `.pem`, `.cer`, `.crt`, `.pfx`, `.p12` | PKCS#12 password may be supplied through `--server-certificate-password-env` |
| Trust-store certificates / CA roots | `.der`, `.pem`, `.cer`, `.crt`, `.pfx`, `.p12` | `.cer/.crt` PEM-vs-DER is detected by content; PKCS#12 certificates are normalized before validation |
| X.509 user certificate | `.der`, `.pem`, `.cer`, `.crt`, `.pfx`, `.p12` | a user `.pfx/.p12` may contain both certificate and private key |
| CRLs | `.der`, `.pem`, `.crl` | normalized to DER when needed before runtime validation |

`.cer` and `.crt` are not treated as fixed encodings. DevAgent inspects their content, so a PEM-encoded `factory-ca.crt` and a DER-encoded `factory-ca.cer` are both valid.

PKCS#12 bundles (`.pfx` / `.p12`) are opened using passwords supplied only through environment-variable references. Decrypted private-key material is passed to the OPC UA runtime in memory; DevAgent does not require the customer to convert the bundle to a permanent unencrypted key file.

## Application certificate, server trust, and user certificate

These are separate OPC UA concepts and DevAgent exposes them separately:

```text
Client application identity / SecureChannel
  --client-certificate
  --client-private-key                  # omit for client .pfx/.p12 bundle
  --private-key-password-env            # also unlocks client .pfx/.p12

Server trust — choose one or both
  --server-certificate                  # exact server certificate pin
  --server-certificate-password-env     # only for encrypted .pfx/.p12 pin
  --trust-store DIR                     # trusted server certificates and/or issuing CA certificates
  --trust-store-password-env            # shared password for .pfx/.p12 files in trust store
  --crl-store DIR                       # optional revocation lists used with --trust-store

X.509 user identity / Session
  --user-certificate
  --user-private-key                    # omit for user .pfx/.p12 bundle
  --user-private-key-password-env       # also unlocks user .pfx/.p12
```

A secure channel requires the DevAgent client application identity plus a server-trust method. The application identity can be a separate certificate/private-key pair or a single `.pfx/.p12` bundle. The server-trust method can be an exact `--server-certificate` pin, a reusable `--trust-store`, or both.

## Server trust modes

### Exact server certificate pin

Use this when the operator wants DevAgent to connect only to one exact server certificate:

```text
--server-certificate ./pki/plc01-server.der
```

A `.cer`, `.crt`, `.pfx`, or `.p12` server certificate can be supplied instead. For a password-protected PKCS#12 bundle:

```bash
export PLC_SERVER_PFX_PASSWORD='...'

--server-certificate ./pki/plc01-server.pfx \
--server-certificate-password-env PLC_SERVER_PFX_PASSWORD
```

This remains the strictest per-server identity mode.

### Reusable trust store / CA trust

Use a directory containing trusted server certificates and/or issuing CA certificates:

```text
./pki/trusted/
├── factory-opcua-root-ca.crt
├── factory-opcua-intermediate-ca.cer
└── legacy-self-signed-server.der
```

Then use:

```text
--trust-store ./pki/trusted
```

If PLC01, PLC02, PLC03, and other OPC UA servers have server certificates issued by that trusted CA chain, DevAgent does **not** need a separate `--server-certificate` argument for every PLC. The server certificate discovered during the OPC UA connection is validated against the trust store before the Session is accepted.

For self-signed OPC UA servers, the individual self-signed server certificate may be placed in the trust store. That avoids a CLI pin but still requires one trusted certificate per self-signed server. For many PLCs, a shared customer/factory CA is the scalable configuration.

If the CA chain contains intermediates, place the required intermediate CA certificates in the same trust-store directory so the runtime can build the certificate chain.

Trust-store scanning is intentionally non-recursive in V1. Put trusted certificate files directly in the directory supplied to `--trust-store`.

If the trust directory contains password-protected `.pfx/.p12` files, a shared bundle password may be supplied:

```bash
export FACTORY_PFX_PASSWORD='...'

--trust-store ./pki/trusted \
--trust-store-password-env FACTORY_PFX_PASSWORD
```

For mixed PKCS#12 bundle passwords, export the required CA/server certificates to separate `.der/.pem/.cer/.crt` files rather than placing differently encrypted bundles in one trust-store directory.

### Optional CRL validation

A CRL directory may be supplied with the trust store:

```text
./pki/crl/
└── factory-opcua-ca.crl
```

```text
--trust-store ./pki/trusted \
--crl-store ./pki/crl
```

`--crl-store` is invalid without `--trust-store`. DevAgent fails closed for missing/empty configured trust or CRL directories.

### Pin + trust store

Both can be supplied together:

```text
--server-certificate ./pki/plc01-server.der \
--trust-store ./pki/trusted
```

In this mode the exact server certificate pin is used for SecureChannel setup and the presented server certificate must also pass trust-store validation.

## Discover the server profile

```bash
devagent live probe opc.tcp://192.168.10.20:4840/
```

`probe` reports the advertised endpoint URL, security mode, security policy, user-token types, DevAgent compatibility, certificate requirements, and a preferred supported profile. Discovery does not alter the OPC UA server.

## Examples

### Anonymous / NoSecurity

```bash
devagent live assist \
  --project-folder ./Line1 \
  --primary-project Line1.L5X \
  --endpoint opc.tcp://192.168.10.20:4840/
```

### Username/password + secure channel + exact pin

```bash
export PLC_OPCUA_PASSWORD='...'

devagent live assist \
  --project-folder ./Line1 \
  --primary-project Line1.L5X \
  --endpoint opc.tcp://192.168.10.20:4840/ \
  --security-policy Aes256_Sha256_RsaPss \
  --security-mode SignAndEncrypt \
  --client-certificate ./pki/devagent-app.der \
  --client-private-key ./pki/devagent-app-key.pem \
  --server-certificate ./pki/plc-server.der \
  --username devagent_reader \
  --password-env PLC_OPCUA_PASSWORD
```

### Username/password + secure channel + reusable trust store

```bash
export PLC_OPCUA_PASSWORD='...'

devagent live assist \
  --project-folder ./Line1 \
  --primary-project Line1.L5X \
  --endpoint opc.tcp://192.168.10.20:4840/ \
  --security-policy Basic256Sha256 \
  --security-mode SignAndEncrypt \
  --client-certificate ./pki/devagent-app.der \
  --client-private-key ./pki/devagent-app-key.pem \
  --trust-store ./pki/trusted \
  --username devagent_reader \
  --password-env PLC_OPCUA_PASSWORD
```

No `--server-certificate` is needed in this example. The server certificate is discovered and must chain to a certificate trusted by `./pki/trusted`.

`Sign` is also accepted when that is the customer's configured secure endpoint.

### Password-protected client `.pfx` + reusable trust store

```bash
export DEVAGENT_PFX_PASSWORD='...'
export PLC_OPCUA_PASSWORD='...'

devagent live assist \
  --project-folder ./Line1 \
  --primary-project Line1.L5X \
  --endpoint opc.tcp://192.168.10.20:4840/ \
  --security-policy Basic256Sha256 \
  --security-mode SignAndEncrypt \
  --client-certificate ./pki/devagent-client.pfx \
  --private-key-password-env DEVAGENT_PFX_PASSWORD \
  --trust-store ./pki/trusted \
  --username devagent_reader \
  --password-env PLC_OPCUA_PASSWORD
```

There is intentionally no `--client-private-key` in this example; the private key is inside the PKCS#12 bundle.

### Secure Anonymous + reusable trust store

```bash
devagent live assist \
  --project-folder ./Line1 \
  --primary-project Line1.L5X \
  --endpoint opc.tcp://192.168.10.20:4840/ \
  --security-policy Aes256_Sha256_RsaPss \
  --security-mode SignAndEncrypt \
  --client-certificate ./pki/devagent-app.der \
  --client-private-key ./pki/devagent-app-key.pem \
  --trust-store ./pki/trusted
```

### Existing NoSecurity username/password profile

Use this only when the customer's existing OPC UA server is intentionally configured that way:

```bash
export PLC_OPCUA_PASSWORD='...'

devagent live assist \
  --project-folder ./Line1 \
  --primary-project Line1.L5X \
  --endpoint opc.tcp://192.168.10.20:4840/ \
  --username devagent_reader \
  --password-env PLC_OPCUA_PASSWORD \
  --allow-insecure-username-password
```

The explicit opt-in prevents DevAgent from accidentally sending username/password over a NoSecurity endpoint because of a missing security flag.

### X.509 user identity + trust store

Separate certificate/key:

```bash
devagent live assist \
  --project-folder ./Line1 \
  --primary-project Line1.L5X \
  --endpoint opc.tcp://192.168.10.20:4840/ \
  --security-policy Basic256Sha256 \
  --security-mode SignAndEncrypt \
  --client-certificate ./pki/devagent-app.der \
  --client-private-key ./pki/devagent-app-key.pem \
  --trust-store ./pki/trusted \
  --user-certificate ./pki/devagent-user.der \
  --user-private-key ./pki/devagent-user-key.pem
```

PKCS#12 user bundle:

```bash
export OPCUA_USER_PFX_PASSWORD='...'

devagent live assist \
  --project-folder ./Line1 \
  --primary-project Line1.L5X \
  --endpoint opc.tcp://192.168.10.20:4840/ \
  --security-policy Basic256Sha256 \
  --security-mode SignAndEncrypt \
  --client-certificate ./pki/devagent-app.der \
  --client-private-key ./pki/devagent-app-key.pem \
  --trust-store ./pki/trusted \
  --user-certificate ./pki/devagent-user.p12 \
  --user-private-key-password-env OPCUA_USER_PFX_PASSWORD
```

## Multi-PLC commissioning JSON

The same security surface is accepted by `devagent live commission`, `vendor-qualify`, and other workflows that load `devagent-live-commission-v1` JSON.

Example reusable trust store:

```json
{
  "security": {
    "security_policy": "Basic256Sha256",
    "security_mode": "SignAndEncrypt",
    "client_certificate": "./pki/devagent-app.der",
    "client_private_key": "./pki/devagent-app-key.pem",
    "trust_store": "./pki/trusted",
    "crl_store": "./pki/crl",
    "username": "devagent_reader",
    "password_env": "PLC_OPCUA_PASSWORD"
  }
}
```

Example PKCS#12 application identity and PKCS#12 trust material:

```json
{
  "security": {
    "security_policy": "Basic256Sha256",
    "security_mode": "SignAndEncrypt",
    "client_certificate": "./pki/devagent-client.pfx",
    "private_key_password_env": "DEVAGENT_PFX_PASSWORD",
    "trust_store": "./pki/trusted",
    "trust_store_password_env": "FACTORY_PFX_PASSWORD",
    "username": "devagent_reader",
    "password_env": "PLC_OPCUA_PASSWORD"
  }
}
```

Example X.509 user identity with exact pin:

```json
{
  "security": {
    "security_policy": "Basic256Sha256",
    "security_mode": "SignAndEncrypt",
    "client_certificate": "./pki/devagent-app.der",
    "client_private_key": "./pki/devagent-app-key.pem",
    "server_certificate": "./pki/plc-server.der",
    "user_certificate": "./pki/devagent-user.der",
    "user_private_key": "./pki/devagent-user-key.pem",
    "user_private_key_password_env": "PLC_USER_KEY_PASSWORD"
  }
}
```

Example explicit NoSecurity username compatibility:

```json
{
  "security": {
    "username": "devagent_reader",
    "password_env": "PLC_OPCUA_PASSWORD",
    "allow_insecure_username_password": true
  }
}
```

Secret values themselves are forbidden in commissioning JSON; use environment-variable names such as `private_key_password_env`, `server_certificate_password_env`, `trust_store_password_env`, and `user_private_key_password_env`.

## Safety boundary

All of these profiles change only how the read-only OPC UA session authenticates and protects communication. They do not add PLC write, force, reset, method-call, download, mode-change, start, or stop capability to DevAgent Live.
