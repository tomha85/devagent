# DevAgent Live OPC UA Connection Profiles V1

DevAgent Live is a **read-only OPC UA client**. The PLC/server administrator remains responsible for configuring the OPC UA server, endpoint, accounts, certificates, trust lists, firewall/network access, and permissions. DevAgent does not modify the PLC/server to establish access.

An engineer can reuse the same OPC UA connection information already known from SCADA or another OPC UA client and provide the equivalent values to DevAgent.

## Supported user identities

| User identity | DevAgent status | CLI |
| --- | --- | --- |
| Anonymous | `SUPPORTED` | no identity flags |
| UserName / password | `SUPPORTED` on a secure OPC UA channel | `--username`, `--password-env` |
| X.509 user certificate | `SUPPORTED` | `--user-certificate`, `--user-private-key`, optional `--user-private-key-password-env` |
| IssuedToken / JWT | `RUNTIME_UNAVAILABLE` with the supported `asyncua>=2,<3` runtime | detected by `devagent live probe`; connection fails closed |

DevAgent intentionally blocks username/password on a completely `NoSecurity` channel. Passwords are accepted only through environment-variable references and are never accepted as literal CLI arguments.

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

## Application certificate vs user certificate

These are separate OPC UA identities and DevAgent exposes them separately:

```text
Client application identity / SecureChannel
  --client-certificate
  --client-private-key
  --private-key-password-env

Server certificate pin
  --server-certificate

X.509 user identity / Session
  --user-certificate
  --user-private-key
  --user-private-key-password-env
```

A secure channel requires the client application certificate/private key and a pinned server certificate. An X.509 user identity additionally requires the user certificate/private key when that is the server's configured user-token type.

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

### Username/password + secure channel

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

### X.509 user identity

```bash
devagent live assist \
  --project-folder ./Line1 \
  --primary-project Line1.L5X \
  --endpoint opc.tcp://192.168.10.20:4840/ \
  --security-policy Basic256Sha256 \
  --security-mode SignAndEncrypt \
  --client-certificate ./pki/devagent-app.der \
  --client-private-key ./pki/devagent-app-key.pem \
  --server-certificate ./pki/plc-server.der \
  --user-certificate ./pki/devagent-user.der \
  --user-private-key ./pki/devagent-user-key.pem
```

## Safety boundary

All of these profiles change only how the read-only OPC UA session authenticates and protects communication. They do not add PLC write, force, reset, method-call, download, mode-change, start, or stop capability to DevAgent Live.
