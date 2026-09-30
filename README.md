# Sales Agent — Marketplace Operations

A Python service that runs the back office of a marketplace seller on the
seller's own computer. It imports orders, estimates the margin of each sale,
prepares purchase orders for suppliers and drafts answers to buyer questions.
Anything that spends money or publishes in the seller's name waits in an
approval queue until a person releases it.

Built with **FastAPI** and **SQLite** and operated from a local web panel.

> **Status (September 2026):** runs locally and has been exercised with
> synthetic data only. The marketplace integrations are implemented but have
> **not been validated against live seller accounts**. The panel now requires
> a login, but with a single shared operator and no HTTPS by default — keep it
> on `127.0.0.1`.

The code targets Brazilian marketplaces today, Mercado Livre first. The
[roadmap](#roadmap) moves the product to US marketplaces.

---

## How it works

Each worker cycle (every 5 minutes by default):

1. imports paid **Mercado Livre** orders and encrypts buyer data on arrival;
2. estimates the net margin and refuses orders below the configured minimum;
3. builds a purchase order for each viable order and runs the compliance rules;
   a blocked order moves to `PROBLEMA` with the reason;
4. queues the purchase orders for approval;
5. drafts answers to unanswered buyer questions and queues them;
6. updates the shipment status of orders marked as confirmed or in transit
   (no step marks a purchase as confirmed yet).

The seller works through the queue in the panel or the CLI. Nothing in the
queue runs before it is approved.

---

## Current status

| Label | Meaning |
|---|---|
| **Tested** | Covered by the automated suite in `tests/`: synthetic data, a fake token endpoint, no network; runs in CI |
| **Verified locally** | Exercised outside the automated suite, with synthetic data on a temporary database: `demo.py`, in-process calls to the panel API, CLI commands |
| **Not validated** | Implemented, never run against the real external service |
| **Simulated** | Placeholder behaviour instead of the real action |
| **Planned** | Does not exist yet — see the roadmap |

What the automated suite covers, and how to run it, is under [Tests](#tests).

| Area | Code | Status |
|---|---|---|
| Order state machine: validated transitions, history table | `core/estados.py` | Tested through the demo flow and one refused transition |
| Margin estimate with the Mercado Livre fee model: average commission (13% classic, 17% premium), reference unit-cost and shipping curves, R$ 79 threshold | `inteligencia/precificacao.py` | Verified locally — an estimate, not the real fee of each category |
| Approval queue: purchases, buyer replies, price changes, listings | `core/aprovacao.py` | Tested for purchase orders |
| Compliance rules before a purchase order is queued; approving a blocked item returns HTTP 409 | `core/conformidade.py`, `painel/app.py` | Verified locally with synthetic Amazon orders; with today's Mercado Livre data no rule can trigger |
| Sending a purchase order to the supplier | `worker.py` | Simulated — writes a text file to `ordens_de_compra/` in the data folder |
| Buyer data encrypted on arrival (Fernet), masked in API responses, reveal endpoint logs each read | `core/privacidade.py`, `painel/app.py` | Tested |
| Retention purge; data-subject export and deletion | `core/privacidade.py` | Implemented, not exercised; only the purge has a panel button |
| Web panel: queue with keyboard shortcuts (`j`/`k`, `a`, `r`, `c`), orders, niche research, pricing, LGPD log, events, connection setup | `painel/` | Verified locally (page and API functions, not in a browser) |
| CLI | `cli.py` | Verified locally (`pendencias`, `preco`) |
| Mercado Livre: OAuth with PKCE, orders, questions, shipments, price update | `conectores/mercadolivre.py` | Not validated |
| Buyer replies drafted with the Claude API, with a fixed FAQ and escalation triggers | `atendimento/` | Not validated; needs `ANTHROPIC_API_KEY` |
| Niche research: Google Trends and Mercado Livre search | `inteligencia/tendencias.py` | Not validated |
| Shopee: partner authorization with HMAC-signed calls, order listing | `conectores/shopee.py` | Not validated; used only by connection setup and test, not by the worker |
| Amazon SP-API with Login with Amazon: order listing | `conectores/amazon.py` | Not validated; used only by the connection test, not by the worker |
| Product and supplier registration | — | Planned — manual SQL today |
| Panel login: one operator, server-side session, CSRF token in a header | `core/seguranca.py`, `painel/app.py` | Tested |
| OAuth return: `state` created by the same panel session, single use, valid for 10 minutes; return pages escape their output and show fixed error messages | `core/seguranca.py`, `painel/app.py`, `painel/configurar.py` | Tested with a fake token endpoint |
| Data folder for `.env`, database, key, tokens and purchase orders: `AGENTE_DADOS`, the executable's folder, or the project folder | `config.py` | Tested |
| Standalone executable | `agente.spec` | Not verified — no build has been run; the rule that keeps data next to the executable is tested without one |

---

## Quick start

Requires **Python 3.10 or newer** (checked with Python 3.14; CI runs the tests
on 3.12 and 3.14).

**Windows:** double-click `iniciar.bat`.

**Linux / macOS:**

```bash
./iniciar.sh
```

On the first run the script checks Python, creates `.venv`, installs
`requirements.txt`, copies `.env.example` to `.env`, creates `agente.db`,
generates the encryption key `.chave_lgpd` and offers demo data. It then runs
`executar.py`, which starts the panel at `http://127.0.0.1:8777`, starts the
worker and opens the browser. `.env`, `agente.db` and `.chave_lgpd` go to the
[data folder](#data-folder), which is the project folder unless
`AGENTE_DADOS` is set.

Manual start:

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # Windows: copy .env.example .env
python executar.py
```

Before you rely on it:

- **Create the operator before the first login:**
  `python cli.py operador --usuario your_user`. The panel answers 401 to every
  request without a session, so nobody gets in until this runs.
- **Back up `.chave_lgpd`.** Without it the stored buyer data cannot be
  decrypted. Git ignores it; keep it that way.
- **`demo.py` deletes** the existing orders, order history, approvals, products
  and suppliers before loading synthetic data. Use it only on a test database.
- With Mercado Livre credentials in `.env`, the worker calls the Mercado Livre
  API a few seconds after start-up. Set `WORKER_ATIVO=false` to keep it idle.
- Margins need the product in the database, and purchase orders need its
  supplier. Today that means SQL on the `produtos` and `fornecedores` tables
  (schema in `db.py`).

---

## Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
```

The suite runs in-process with FastAPI's `TestClient`; no server is started.
Before any project module is imported, `tests/conftest.py` points
`AGENTE_DADOS` at a new temporary folder and sets a freshly generated
`CHAVE_LGPD`, fake marketplace credentials and `WORKER_ATIVO=false`, so the
project's own `.env`, database, key and tokens are never opened. Each test gets
its own database and data files. Proxy settings are switched off
(`NO_PROXY=*`), and any connection or name lookup outside loopback fails the
test.

What it covers:

- **Panel login:** password hashing, wrong password, lockout after five
  attempts and the counter reset by a successful login, 401 for the API and a
  redirect to `/login` for pages without a session, the 30-minute idle and
  8-hour session limits, sessions ended by a password change, CSRF on
  state-changing calls, logout, and the operator recorded when buyer data is
  revealed and when an item is approved or refused.
- **OAuth `state`:** valid, reused, missing, forged, from another session,
  without a session, expired after 10 minutes and issued for another
  provider; the cap on pending authorizations; HTML and marketplace responses
  never echoed in messages; refusals logged without the `state` value; escaped
  return pages; a single `/oauth/ml/retorno` route; the HTTP flow with two
  sessions.
- **Demo flow end to end:** margin, queue, approval, `COMPRA_ENVIADA`.
- **Buyer data:** encrypted at rest, masked in API responses, each reveal logged.
- **Data folder:** where `.env`, the database, the key, the tokens and the
  purchase orders are read and written.

**CI:** [`.github/workflows/tests.yml`](.github/workflows/tests.yml) runs
`pytest -q` on Python 3.12 and 3.14 for every pull request and every push to
`main`, with read-only repository permissions and every action pinned to a
commit SHA.

---

## Configuration

Settings come from `.env`; `.env.example` is the template. Fill credentials
locally and never commit `.env`.

### Data folder

`.env`, the database, `.chave_lgpd`, the OAuth token files and
`ordens_de_compra/` live in the data folder, chosen in this order:

1. the folder in the `AGENTE_DADOS` environment variable, created if missing;
2. in a PyInstaller build, the folder of the executable;
3. otherwise, the project folder.

`AGENTE_DADOS` must be a real environment variable, set in the shell or the
system. A line in `.env` has no effect, because `.env` itself is read from the
data folder. `iniciar.bat` and `iniciar.sh` follow the same rule. Files that
ship with the code, such as `.env.example` and `painel.html`, stay in the
project folder.

### Variables

| Variables | Default | Purpose |
|---|---|---|
| `ML_CLIENT_ID`, `ML_CLIENT_SECRET` | empty | Mercado Livre application |
| `ML_REFRESH_TOKEN`, `ML_SELLER_ID` | empty | Filled by the panel after authorization |
| `ML_SITE_ID` | `MLB` | Mercado Livre site |
| `SHOPEE_PARTNER_ID`, `SHOPEE_PARTNER_KEY`, `SHOPEE_SHOP_ID` | empty | Shopee Open Platform application and shop |
| `SHOPEE_REFRESH_TOKEN` | empty | Filled by the panel after authorization |
| `SHOPEE_SANDBOX` | `true` | Use the Shopee test environment |
| `AMZ_LWA_CLIENT_ID`, `AMZ_LWA_CLIENT_SECRET`, `AMZ_REFRESH_TOKEN` | empty | Amazon SP-API credentials |
| `AMZ_MARKETPLACE_ID`, `AMZ_ENDPOINT` | Brazil marketplace, North America endpoint | Amazon marketplace and region |
| `ANTHROPIC_API_KEY` | empty | Reply drafting; without it only fixed FAQ answers are queued and other questions are escalated |
| `MARGEM_MINIMA_PCT` | `18` | Minimum net margin (%) |
| `TETO_COMPRA_AUTOMATICA` | `300` | Purchases above this value are labelled `ACIMA DO TETO`; every purchase still needs approval |
| `ALIQUOTA_IMPOSTO_PCT` | `4` | Estimated tax on sales (%) |
| `PRAZO_FORNECEDOR_DIAS` | `5` | Default supplier lead time (days) |
| `AGENTE_DADOS` | not set | [Data folder](#data-folder); read only from the real environment, never from `.env` |
| `DB_PATH` | `agente.db` | SQLite database file; a relative path is resolved against the data folder |
| `CHAVE_LGPD` | empty | Encryption key; when empty, `.chave_lgpd` is used |
| `RETENCAO_PII_DIAS` | `1825` | Days before buyer data is purged |
| `HOST_PAINEL` | `127.0.0.1` | Panel address; keep it while there is a single operator and no HTTPS |
| `SESSAO_OCIOSA_MIN`, `SESSAO_MAX_HORAS` | `30`, `8` | Idle timeout and maximum session lifetime |
| `WORKER_ATIVO`, `ABRIR_NAVEGADOR` | `true` | Start the worker; open the browser |
| `PORTA_PAINEL`, `INTERVALO_WORKER` | `8777`, `300` | Panel port and worker interval; today only honoured as real environment variables, not from `.env` |
| `MODO_SIMULACAO` | `true` | Not implemented: the code does not read it |

---

## Connecting marketplaces

The **Conexões** tab lists, for each marketplace, the steps, the developer
portal and the redirect URI to register. None of these flows has been validated
with a live account yet.

- **Mercado Livre:** create the app in the DevCenter, register
  `https://localhost:8777/oauth/ml/retorno`, paste the App ID and Secret Key,
  click authorize, then paste the whole return URL into the panel. The browser
  shows a connection error on that URL because the panel serves plain HTTP; the
  code is read from the pasted URL. The URL is accepted only from the same
  panel session that started the authorization, once, within 10 minutes;
  pasting only the code is refused.
- **Shopee:** paste the Partner ID, Partner Key and Shop ID, then authorize.
  The authorization link expires after 5 minutes.
- **Amazon:** paste the LWA client ID, client secret and a refresh token issued
  in Seller Central.

**Testar conexão** calls the real API. Credentials and tokens are written to
`.env`, `.token_ml.json` and `.token_shopee.json` in the data folder; git
ignores all three.

---

## Design decisions

**People release irreversible actions.** Purchases, public buyer replies, price
changes and new listings are queue item types, and the worker never calls their
executors. The queue receives the executors by injection, so it does not import
marketplace code.

**Compliance before the queue.** A purchase order that breaks a rule, such as
Amazon's dropshipping policy, never reaches the queue, and the approval
endpoint checks again. Limits today: rules only block what they can see —
missing data is not treated as a violation — buyer replies and prices are not
checked, and `cli.py aprovar` does not repeat the check.

**Explicit state.** Orders change state only through `transicionar()`, which
validates the transition and records it in `transicoes`.

**Privacy in storage and on screen.** Buyer name, buyer ID and shipping
reference are encrypted when an order arrives, and the key lives outside the
database. API responses mask the name; the reveal endpoint logs every read.

---

## Known limitations

- One shared operator, no roles and no HTTPS by default. Keep the panel on `127.0.0.1` until both exist.
- Only Mercado Livre is automated; Shopee and Amazon stop at the connection test.
- No integration has been validated with a live seller account.
- Purchase orders are text files; nothing reaches suppliers.
- Margins rely on average fees; confirm the real fees of each category.
- The reveal endpoint fails for rows whose address field is not encrypted (demo rows and purged rows).
- `PORTA_PAINEL` and `INTERVALO_WORKER` are ignored in `.env`; `MODO_SIMULACAO` does nothing.
- The tests use synthetic data and a fake token endpoint; nothing is tested against a live marketplace or in a browser.

---

## Project layout

```
executar.py              entry point: panel and worker in one process
worker.py                the cycle: import, margin, purchase orders, replies, tracking
cli.py                   terminal commands
config.py                settings loaded from .env; data folder
db.py                    SQLite schema and connection
demo.py                  synthetic demo data (wipes existing data)
core/                    estados (state machine), aprovacao (queue),
                         conformidade (rules), privacidade (LGPD)
conectores/              mercadolivre, shopee, amazon
inteligencia/            precificacao (margin), tendencias (niche research)
atendimento/             persona and reply bot
painel/                  app.py (FastAPI routes), configurar.py (connections), painel.html
iniciar.bat, iniciar.sh  first-run setup and start
organizar.py             rebuilds the folder layout when files were downloaded one by one
agente.spec              PyInstaller build spec (not verified)
tests/                   pytest suite; conftest.py isolates data and blocks the network
requirements-dev.txt     test dependencies (pytest, httpx)
pytest.ini               test discovery and import path
.github/workflows/       CI: tests.yml
```

---

## Roadmap

Planned, in this order. None of it exists yet.

1. **US marketplace authorization:** Amazon Selling Partner API as an
   application for third-party sellers, and eBay, starting in the Sandbox —
   based on the official documentation.
2. **Panel login**, then one end-to-end integration: seller consent,
   authenticated reads, storage, re-runs without duplicates and failure
   handling.
3. **Pilot MVP:** products and suppliers through the API; paginated,
   incremental sync of listings, stock and orders; alerts for stale data,
   expired connections and low stock; price and stock updates with dry run,
   limits and an audit log.
4. **Operations:** one isolated instance per client, backups with restore
   tests, health monitoring; interface screens after the backend is stable.

Mercado Livre and Shopee receive fixes only in this cycle. Walmart Marketplace
is a candidate after the first pilot.

---

## Tech stack

Python · FastAPI and Uvicorn · SQLite · cryptography (Fernet) · requests ·
pytrends and pandas (niche research) · Claude API (optional, reply drafting) ·
plain HTML, CSS and JavaScript for the panel

---

## License

MIT — see [LICENSE](LICENSE).
