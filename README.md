# Sales Agent — Marketplace Automation

A Python service that automates day-to-day selling operations across
Mercado Livre, Shopee and Amazon: pricing, order handling, policy
compliance and data protection — with a human approval gate in front of
every action that spends money.

Built with **FastAPI** and **SQLite**. Runs locally, operated from a
keyboard-driven web panel.

---

## The problem

Marketplace sellers lose margin in three predictable ways:

1. **Fee drift.** Marketplace commission tables change. A price that was
   profitable last quarter quietly becomes a loss.
2. **Policy strikes.** An automated listing or message that violates a
   marketplace rule gets the account penalized or suspended.
3. **Runaway automation.** A bot that can spend money without supervision
   is a liability, not an asset.

This project treats all three as design constraints rather than
afterthoughts.

---

## What it does

| Component | Responsibility |
|---|---|
| **Marketplace connectors** | OAuth-based integration layer for Mercado Livre, Shopee and Amazon. |
| **Margin calculator** | Computes true net margin per item using the Mercado Livre 2026 fee tables. |
| **Order state machine** | Models an order's lifecycle as explicit states and legal transitions, so an order can never land in an undefined status. |
| **Approval queue** | Any action that spends money is queued for explicit human approval before it executes. Nothing financial happens unattended. |
| **Compliance engine** | Validates outgoing actions against marketplace policy rules and blocks violations before they reach the API. |
| **LGPD module** | Encrypts personally identifiable information at rest, in line with Brazil's General Data Protection Law (LGPD). |
| **Web panel** | Local operations dashboard, fully keyboard-operable — no mouse required for routine work. |

---

## Design decisions

**Human-in-the-loop by default.** The approval queue is not a feature
flag. Actions with financial consequences are separated from actions
without them at the architecture level, and only the former require a
human to release them.

**Fail closed on compliance.** The compliance engine rejects an action it
cannot verify as policy-safe. A blocked legitimate action costs a few
seconds; an account suspension costs the business.

**Explicit state over implicit status.** Order status is a state machine
with enumerated transitions, not a free-text column updated from several
places.

**Privacy as storage policy.** PII is encrypted at rest rather than
filtered at display time, so a database copy is not a data leak.

---

## Quick start

Requires Python and the dependencies listed in `requirements.txt`.

**Windows**

```bat
iniciar.bat
```

**Linux / macOS**

```bash
./iniciar.sh
```

Then open the panel:

```
http://127.0.0.1:8777
```

---

## Project status

This is working software, run locally. It is honest about what has and
has not been verified:

| Area | Status |
|---|---|
| Core engine, margin calculation, order state machine | Working |
| Approval queue, compliance engine, LGPD encryption | Working |
| Web panel | Working |
| Marketplace connectors (OAuth) | Implemented, **not yet verified against live marketplace APIs** |
| Product & supplier registration | Currently via direct SQL — UI in progress |
| Panel authentication | **Not yet implemented — run locally only, do not expose to a network** |

### Roadmap

- [ ] Product and supplier registration screens (replacing manual SQL)
- [ ] Authentication for the web panel — required before any deployment
- [ ] Live integration testing against each marketplace API

---

## Tech stack

- **Python** — application and business logic
- **FastAPI** — HTTP layer and web panel
- **SQLite** — embedded persistence
- **OAuth 2.0** — marketplace authentication

---

## Author

**Gabriel Freire** — back-end developer (Java, Python, SQL)
São Luís, Maranhão, Brazil

---

## License

MIT — see [LICENSE](LICENSE).
