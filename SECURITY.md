# Security

This file covers how to report a vulnerability, the security controls the
software provides, and the incident response plan for anyone who runs an
instance with real marketplace data. It is written against the
[Amazon Selling Partner API Data Protection Policy](https://sellercentral.amazon.com/solution-provider/policy?policyType=DPP&locale=en_US)
(DPP), which Amazon checks before approving a public SP-API developer, and
the Brazilian LGPD.

## Reporting a vulnerability

Report privately through GitHub's private vulnerability reporting on this
repository (**Security → Report a vulnerability**). Do not open a public issue
with details. Include the version (commit), the steps to reproduce and the
impact you expect. Only the latest `main` receives fixes.

## Deployment model

One isolated instance per client: its own data folder (`AGENTE_DADOS`), its
own database, buyer-data key and vault key, and its own panel process. The
panel listens on `127.0.0.1` by default. Beyond loopback it refuses to start
without HTTPS (`TLS_CERT`/`TLS_CHAVE` or a reverse proxy with
`PROXY_HTTPS=true`), without the public host name (`HOSTS_PAINEL`) and while
an active operator has no second factor.

## Controls in the software

| Area | Control | DPP |
|---|---|---|
| Accounts | One account per person, unique names; lock after 10 consecutive errors; 5 errors per address in 15 minutes → HTTP 429; deactivation ends sessions at once; `cli.py operadores` for the quarterly access review | 1.2.1, 1.2.2, 1.2.3 |
| Least privilege | Roles: `admin` and read-only `leitura` (an allowlist of read routes; everything else is refused); new operators after the first start as `leitura` | 1.3 |
| Authentication | TOTP second factor, required whenever the panel is exposed; passwords of 12+ characters with upper and lower case, digit and symbol, no part of the name, none of the last 10, changed at most daily, valid 365 days; scrypt hashes | 1.4.1 |
| Sessions | 256-bit ids stored only as SHA-256; idle 30 minutes, maximum 8 hours; CSRF token in a header; `Secure` cookie and HSTS under HTTPS | 1.4.1, 1.5 |
| Web | Content-Security-Policy, `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy`, `Cache-Control: no-store`; `Host` allowlist; login only as JSON; no API schema exposed | 1.1.1 |
| Credentials | Marketplace secrets and refresh tokens encrypted at rest in a vault (Fernet, key outside the database, rotation with `cli.py cofre rotacionar`); never written to logs, URLs or error messages | 1.4.2, 2.5 |
| In transit | TLS for every marketplace call (certificates verified); panel served over HTTPS when exposed | 1.5 |
| At rest | Buyer data from Mercado Livre encrypted with its own key; backups encrypted with a separate password (scrypt, then Fernet) | 2.4 |
| Amazon data | Read-only. No buyer name, address, e-mail or phone is stored: only order ids, status, amounts, items, listings and stock. `cli.py amazon apagar-dados` deletes it all with the refresh token, overwrites freed pages and compacts the file; restoring an older backup deletes it again | 1.7, 2.1 |
| Logs | Events with timestamp, origin, outcome and actor for logins, lockouts, configuration, approvals, vault and synchronization; no buyer data, secrets or response bodies; insert-only table, hash-chained (`cli.py eventos --verificar`); never purged by the software (kept ≥ 12 months); one warning event when an address reaches the login limit | 2.6.1, 2.6.2 |
| Vulnerabilities | Every pull request runs the tests, `pip-audit` on the pinned versions (`constraints.txt`) and `gitleaks` over the whole history | 2.7.1, 2.5 |

## Operator responsibilities

The software cannot provide these; whoever runs an instance does:

- Network: firewall, a WAF in front of an exposed panel, intrusion detection.
- Endpoints: disk encryption, malware protection, screen lock within 15
  minutes, no unmanaged devices.
- Vulnerability management: run `pip-audit -r constraints.txt` at least every
  30 days; fix critical findings within 7 days and high within 30; a yearly
  penetration test.
- People: data protection training every year; confidentiality clauses; the
  access review every quarter (`python cli.py operadores`), kept as a record.
- Monitoring: point an uptime monitor at `GET /saude` (no login; HTTP 503
  means failure) and run `python cli.py saude --completo` daily; act on
  `atencao` and `falha`.
- Log review at least every two weeks: read the warning events and run
  `python cli.py eventos --verificar`; write the printed last hash in the
  review record, outside the machine, so a rewritten trail is noticed.
- A written risk assessment reviewed every year, and a published privacy
  policy for the sellers.
- Backups (`cli.py backup criar`) stored in a separate location.

## Incident response plan

Review this plan every 6 months and after any major change to the
infrastructure. Record each review (date, who, what changed).

### Roles

- **Incident Management Point of Contact (IMPOC):** the person named in the
  Amazon developer profile for the instance. Reachable during an incident;
  decides on containment and notifications.
- **Operator:** anyone with an account on the instance; reports suspicious
  activity to the IMPOC at once.

### What counts as an incident

Unauthorized access to the panel, the server or the data folder; a leaked
secret (LWA client secret, refresh token, vault key, buyer-data key, backup
password, operator password or TOTP key); Amazon or buyer data found outside
the instance; lost or stolen device holding the data folder or a backup;
malware on the server.

Signals: lockout and "login refused" events, HTTP 429 bursts, operators or
logins nobody recognizes in `cli.py operadores`, "sincronizacao_falhou" or
"conexao_vencida" alerts nobody expected, gitleaks failing in CI.

### Procedure

1. **Detect and record.** Note the time of detection; the clocks below start
   there. Open a written incident record (description, timeline, actions).
2. **Contain.** Stop the panel and worker. Deactivate suspicious operators
   (`cli.py operador --usuario NAME --desativar`). Rotate what may have
   leaked: the LWA client secret in the Solution Provider Portal, then
   authorize again; operator passwords (`--suspeita-de-vazamento`) and TOTP
   keys (`--mfa-ativar`); the vault key (`cli.py cofre rotacionar`).
3. **Preserve evidence.** Before changing data, make an encrypted backup
   (`cli.py backup criar`) and export the events. Keep a chain-of-custody
   note: who copied what, when, where it is stored.
4. **Assess.** Which data (Amazon, Mercado Livre buyer data, credentials),
   which sellers, which period.
5. **Notify.**
   - **Amazon:** e-mail security@amazon.com **within 24 hours of detection**
     when Amazon Information is or may be involved (DPP 1.6.3).
   - **ANPD and data subjects:** within **3 business days** when the incident
     may cause relevant risk or harm to the people whose data is involved
     (LGPD art. 48; Resolução CD/ANPD nº 15/2024, arts. 6 and 9).
   - **Affected sellers:** as soon as the scope is known.
   - Do not speak for Amazon to regulators or customers.
6. **Eradicate and recover.** Fix the cause, update dependencies, restore
   from a backup taken before the incident if needed (a restore reapplies the
   buyer-data deletions and the Amazon data deletion).
7. **Close.** Complete the record: description, remediation and corrective
   controls; keep it, with the evidence, for at least 12 months after the
   end of the Amazon agreement.

### Runbooks

- **Seller revoked the authorization, Amazon asked for deletion, or the
  instance is being retired:** within 30 days, run
  `python cli.py amazon apagar-dados --motivo <reason>`, then destroy backups
  that are no longer needed (DPP 1.7).
- **Secret committed to the repository:** rotate it first, then remove it;
  history rewriting needs the owner's decision.
- **Lost phone with the authenticator:** on the server, run
  `cli.py operador --usuario NAME --mfa-ativar` again; it replaces the key.
