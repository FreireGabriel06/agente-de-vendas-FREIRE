# Sales Agent — Marketplace Operations

A Python service that runs the back office of a marketplace seller on the
seller's own computer. It imports orders, estimates the margin of each sale,
prepares purchase orders for suppliers and drafts answers to buyer questions.
Anything that spends money or publishes in the seller's name waits in an
approval queue until a person releases it.

Built with **FastAPI** and **SQLite** and operated from a local web panel.

> **Status (October 2026):** runs locally and has been exercised with
> synthetic data only. **Simulation mode is on by default**
> (`MODO_SIMULACAO=true`): approving an item publishes nothing, changes no
> price and writes the purchase order marked as a test; the order or question
> keeps waiting and returns to the queue when simulation is turned off. The marketplace
> integrations are implemented but have **not been validated against live
> seller accounts**. The panel requires a login, but with a single shared
> operator and no HTTPS by default — keep it on `127.0.0.1`.

The code targets Brazilian marketplaces today, Mercado Livre first. The
[roadmap](#roadmap) moves the product to US marketplaces.

---

## How it works

Each worker cycle (every 5 minutes by default, `INTERVALO_WORKER`):

1. imports paid **Mercado Livre** orders and encrypts buyer data on arrival;
2. estimates the net margin and refuses orders below the configured minimum;
3. builds a purchase order for each viable order and runs the compliance
   rules. An order that breaks a rule moves to `PROBLEMA` with the reason. An
   order the rules cannot judge because data is missing (for example, whether
   the product is in a regulated category) enters the queue **blocked**, with
   what is missing and how to provide it;
4. queues every purchase order for approval. Nothing is bought
   automatically: `TETO_COMPRA_AUTOMATICA` only labels the item `ROTINA` or
   `ACIMA DO TETO`;
5. drafts answers to unanswered buyer questions with the Claude API and
   queues them; anything the draft cannot answer safely is escalated to you.
   Each question goes to the model once: questions already queued or
   escalated are skipped, and only a temporary failure (rate limit, API
   outage, network, missing key) is retried on the next cycle;
6. updates the shipment status of orders marked as confirmed or in transit
   (no step marks a purchase as confirmed yet).

Only one cycle runs at a time. While one is running (the background worker,
a press of `c` in the panel, or `cli.py ciclo` / `cli.py rodar` in another
process on the same database), a new one is skipped and the panel answers
HTTP 409, so no question goes to the model twice and no purchase is queued
twice.

The seller works through the queue in the panel or the CLI. Nothing in the
queue runs before it is approved, and both apply the same compliance check
before running an item. An approved purchase order is written to a text file
in `ordens_de_compra/`; the program does not send it. Sending it to the
supplier, and paying, are done by hand.

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
| Approval queue: purchases, buyer replies, price changes, listings | `core/aprovacao.py` | Tested for purchase orders, buyer replies and price changes (marketplace calls faked) |
| Compliance rules that fail closed: the same check before a purchase order is queued, in the panel queue and at approval from the panel (HTTP 409) or `cli.py aprovar`, with the same reason; missing data blocks the item as "needs confirmation" with the way out | `core/conformidade.py`, `core/aprovacao.py`, `painel/app.py`, `cli.py` | Tested with synthetic Mercado Livre and Amazon orders |
| Purchase order after approval | `worker.py` | Writes a text file to `ordens_de_compra/` in the data folder; nothing is sent to the supplier, in either mode |
| Simulation mode (`MODO_SIMULACAO`, on by default): approved replies and price changes are recorded, not sent; the purchase order file and the result are marked as a test; a simulated approval does not consume the item — the order stays in `AGUARDANDO_APROVACAO`, and the purchase and the reply return to the queue once simulation is off; panel, CLI, `demo.py` and `executar.py` say when it is on | `config.py`, `worker.py`, `atendimento/bot.py`, `core/aprovacao.py`, `painel/` | Tested |
| Buyer data encrypted on arrival (Fernet), masked in API responses, reveal endpoint logs each read; plaintext (demo) or unreadable fields come back as a clean result with a notice, and so does an order whose buyer data is entirely empty or purged | `core/privacidade.py`, `painel/app.py` | Tested |
| Retention purge; data-subject export and deletion | `core/privacidade.py` | Tested; only the purge has a panel button |
| Web panel: queue with keyboard shortcuts (`j`/`k`, `a`, `r`, `c`), orders, niche research, pricing, LGPD log, events, connection setup | `painel/` | Verified locally (page and API functions, not in a browser) |
| CLI | `cli.py` | `aprovar` and `pendencias` tested; `preco` verified locally |
| Mercado Livre: OAuth with PKCE, orders, questions, shipments, price update | `conectores/mercadolivre.py` | Not validated |
| Buyer replies drafted with the Claude API through the official `anthropic` SDK, with a fixed FAQ and escalation triggers; each question is sent to the model once; refusals, truncated drafts and API errors escalate to a person; the connection test uses `models.retrieve` (no tokens) — see [Claude API](#claude-api) | `atendimento/`, `painel/configurar.py` | Tested with a fake client and with the real SDK over a mock transport; not validated against the live API; needs `ANTHROPIC_API_KEY` |
| Shipment tracking for confirmed and in-transit orders, reading the encrypted shipping reference through `core/privacidade.py` (first read per order logged) | `worker.py`, `core/privacidade.py` | Tested with a fake Mercado Livre client |
| Niche research: Google Trends and Mercado Livre search | `inteligencia/tendencias.py` | Not validated |
| Shopee: partner authorization with HMAC-signed calls, order listing | `conectores/shopee.py` | Not validated; used only by connection setup and test, not by the worker |
| Amazon SP-API with Login with Amazon: order listing | `conectores/amazon.py` | Not validated; used only by the connection test, not by the worker |
| Product and supplier registration | — | Planned — manual SQL today |
| Panel login: one operator, server-side session, CSRF token in a header | `core/seguranca.py`, `painel/app.py` | Tested |
| OAuth return: `state` created by the same panel session, single use, valid for 10 minutes; return pages escape their output and show fixed error messages | `core/seguranca.py`, `painel/app.py`, `painel/configurar.py` | Tested with a fake token endpoint |
| Data folder for `.env`, database, keys, legacy token files and purchase orders: `AGENTE_DADOS`, the executable's folder, or the project folder | `config.py` | Tested |
| Panel port and worker interval from `.env` or the environment (`PORTA_PAINEL`, `INTERVALO_WORKER`); a non-numeric or out-of-range value falls back to the default with a warning | `config.py`, `executar.py`, `worker.py` | Tested |
| Marketplace credentials encrypted at rest: a `credenciais` table in SQLite, Fernet/MultiFernet, key in `CHAVE_COFRE` or `.chave_cofre`, separate from the buyer-data key; a wrong or missing key fails closed | `core/cofre.py` | Tested |
| Connectors, panel and reply bot read and write secrets through the vault at use time; legacy `.token_ml.json` / `.token_shopee.json` imported once and left untouched; plaintext secrets already in `.env` keep working | `conectores/`, `painel/`, `config.py`, `atendimento/bot.py` | Tested with fake token endpoints; not validated against live accounts |
| Vault commands: `cofre migrar`, `cofre rotacionar`, `cofre listar`, `cofre apagar` | `cli.py` | Tested |
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
`executar.py`, which starts the panel at `http://127.0.0.1:8777` (the port
comes from `PORTA_PAINEL`), starts the worker and opens the browser. `.env`, `agente.db` and `.chave_lgpd` go to the
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
- **Back up the vault key too** — `.chave_cofre`, created the first time a
  secret or token is saved, or your `CHAVE_COFRE`. Losing it means clearing
  the vault with `python cli.py cofre apagar --tudo`, then reconnecting every
  marketplace and typing the secrets again. See
  [Credential vault](#credential-vault).
- **`demo.py` deletes** the existing orders, order history, approvals, products
  and suppliers before loading synthetic data. Use it only on a test database.
- With Mercado Livre credentials in `.env`, the worker calls the Mercado Livre
  API a few seconds after start-up. Set `WORKER_ATIVO=false` to keep it idle.
- **Simulation mode is on until you turn it off.** Approving then records what
  would happen and sends nothing. With Mercado Livre connected, the orders and
  questions are real: a simulated approval leaves them waiting, and they come
  back to the queue after you turn simulation off. To operate for real, set
  `MODO_SIMULACAO=false` in `.env` and restart. Only an explicit "no" (`false`,
  `0`, `no`, `off`, `nao`) turns it off; an empty or mistyped value keeps it on.
- Margins need the product in the database, and purchase orders need its
  supplier. Today that means SQL on the `produtos` and `fornecedores` tables
  (schema in `db.py`).
- **Purchases stay blocked until you give the confirmations compliance asks
  for:** `EMITE_NOTA_FISCAL=true` (or `false`) in `.env`, then a restart; per
  product, `categoria_regulada` (`nenhuma` or one of the listed categories),
  `habilitacao_confirmada` for a regulated category and
  `reembalagem_confirmada` for Amazon orders; also for Amazon orders, the
  supplier's `canal` in `fornecedores` must be `email` or `whatsapp` (`api`
  or `portal` means the supplier ships directly, which is refused). Each
  blocked item says what is
  missing and how to provide it (for a product field, the SQL to run); an
  item already in the queue is released as soon as the data is filled in.
  The product confirmations take `1` (yes) or `0` (no); `true`/`false`,
  `sim`/`não` and the other words `.env` accepts also work. Anything else,
  including an empty string, counts as not confirmed yet.

---

## Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
```

The suite runs in-process with FastAPI's `TestClient`; no server is started.
Before any project module is imported, `tests/conftest.py` points
`AGENTE_DADOS` at a new temporary folder and sets freshly generated
`CHAVE_LGPD` and `CHAVE_COFRE` keys, fake marketplace credentials and
`WORKER_ATIVO=false`, so the project's own `.env`, database, keys and tokens
are never opened. Each test gets its own database and data files. Proxy
settings are switched off (`NO_PROXY=*`), and any connection or name lookup
outside loopback fails the test.

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
- **Demo flow end to end:** margin, queue, approval, `COMPRA_ENVIADA` with
  simulation off and the confirmations given; with simulation on, the
  approved order stays in `AGUARDANDO_APROVACAO`; without
  `EMITE_NOTA_FISCAL` the demo shows the purchase blocked and why.
- **Buyer data:** encrypted at rest, masked in API responses, each reveal
  logged; the reveal answers with a clean result and a notice for demo rows,
  purged or deleted rows, legacy plaintext and fields the current key cannot
  open, and still logs the read; the data-subject export returns an empty
  address for those rows instead of failing.
- **Compliance:** fails closed — every missing confirmation blocks with a
  reason and a way out; a real violation is a hard block; the worker queues
  "needs confirmation" items blocked and sends hard violations to `PROBLEMA`;
  a confirmation filled in later releases the queued item; the panel API and
  `cli.py aprovar` refuse a blocked item with the same reason; unknown action
  types are blocked; old databases get the new product columns; a typed
  "no" (`'nao'`, `'false'`...) in a confirmation column blocks, and an empty
  or unknown value asks for the confirmation; an unknown supplier channel, a
  product without a supplier, a supplier lead time typed as text (`'6 dias'`)
  and a listing without category or warranty ask for confirmation.
- **Simulation mode:** on by default and off only with an explicit "no";
  approved replies and price changes never reach the marketplace while it is
  on, and do when it is off (fake connector); the purchase order file and
  the result are marked as a test; a simulated purchase leaves the order in
  `AGUARDANDO_APROVACAO`, and with simulation off the purchase and the reply
  return to the queue (the reply without a new model call); the panel says
  the mode is on.
- **Claude API:** the official SDK with a fake client and with the real SDK
  over a mock HTTP transport (no network): model and effort from the
  configuration, `max_tokens` and timeout by effort, the API address fixed
  even with `ANTHROPIC_BASE_URL` set, fallback beta header and
  `fallbacks: "default"`, no sampling or thinking parameters, refusal /
  `max_tokens` / other stop reasons and each API error class (timeout and
  HTTP 400 with their own messages) escalated without a queued answer, an
  answer from the fallback model recorded with that model's name, no key,
  buyer text or model text in logs or messages, a question seen in several
  cycles sent to the model once (a temporary failure retried without a second
  warning, a deleted draft drafted again), an approved reply whose publication
  failed back in the queue with the same text and no new model call (a
  refused one stays out), connection test through `models.retrieve` only.
- **One cycle at a time:** a press of `c` during the background cycle gets
  HTTP 409 and runs nothing, so a question is not sent to the model twice and
  a purchase is not queued twice; a cycle held by another process makes this
  one skip.
- **Shipment tracking:** an encrypted shipping reference is read through the
  privacy module and the order moves to `ENTREGUE` with its tracking code;
  an empty or unreadable reference is skipped without an error; a database
  created by `cli.py init` or the worker, without the panel, has the access
  log table.
- **Settings and texts:** `PORTA_PAINEL` and `INTERVALO_WORKER` read from
  `.env` in a fresh process, with bad values falling back to the defaults;
  `cli.py rodar` without `--intervalo` uses `INTERVALO_WORKER`; the purchase
  confirmation dialog and docstrings describe what really happens; the cap
  only labels the queue item.
- **Credential vault:** round trip; no plaintext value or key anywhere in the
  data folder; wrong, malformed and missing keys fail closed without writing
  or generating a new key; key file created on first write with a backup
  warning; rotation with an old key still readable, then re-encrypted, and a
  failed rotation that changes nothing; legacy token files imported with the
  file byte-identical and a warning; Mercado Livre and Shopee renewal, Shopee
  signing and both code exchanges through the vault (fake HTTP); `gravar_env`
  and `/api/configuracao/salvar` keep secret values out of `.env` and out of
  the response; reading order (system environment, vault, old `.env` value;
  rotated refresh tokens); no secret or key in the `eventos` table; the
  migration command leaves `.env` byte-identical; the worker keeps running
  with a broken vault. Single-use refresh tokens (fake token server that
  rejects a reused token): a locked database or unreadable key during the
  save keeps the new token in memory and saves it later; a wrong key stops
  before the token is sent; two threads, or another process holding the
  lock, never send the same token twice; a delayed legacy import never
  overwrites newer tokens, and a legacy file left on disk is not read again.
  A ciphertext copied to another row, a non-numeric expiry and unexpected
  errors never put a stored value in events or responses; rows from an
  earlier build reopen only after `cofre rotacionar`; `cofre apagar --tudo`
  works without the key.
- **Data folder:** where `.env`, the database, both keys, the legacy token
  files and the purchase orders are read and written. With `AGENTE_DADOS`
  unset, `cli.py` and `worker.py` name the folder before the first write.
  The test run aborts while loading `tests/conftest.py`, before any test is
  collected, if the data folder or the database is inside the project folder.

**CI:** [`.github/workflows/tests.yml`](.github/workflows/tests.yml) runs
`pytest -q` on Python 3.12 and 3.14 for every pull request and every push to
`main`, with read-only repository permissions and every action pinned to a
commit SHA.

---

## Configuration

Settings come from `.env`; `.env.example` is the template. Secrets —
client secrets, the Shopee partner key, refresh tokens, the LWA secret and
the Anthropic API key — belong in the [credential vault](#credential-vault);
the panel saves them there, never in `.env`. Never commit `.env`.

### Data folder

`.env`, the database (which holds the credential vault), `.chave_lgpd`,
`.chave_cofre`, the empty lock files `.trava_tokens.<marketplace>`, any legacy
OAuth token files and `ordens_de_compra/` live in the data folder, chosen in
this order:

1. the folder in the `AGENTE_DADOS` environment variable, created if missing;
2. in a PyInstaller build, the folder of the executable;
3. otherwise, the project folder.

`AGENTE_DADOS` must be a real environment variable, set in the shell or the
system. A line in `.env` has no effect, because `.env` itself is read from the
data folder. `iniciar.bat` and `iniciar.sh` follow the same rule. Files that
ship with the code, such as `.env.example` and `painel.html`, stay in the
project folder. The empty lock file `.trava_ciclo`, which keeps worker cycles
one at a time, sits next to the database (the data folder unless `DB_PATH`
points elsewhere).

When `AGENTE_DADOS` is not set, `python cli.py ...` and `python worker.py`
print the data folder they are about to use (on standard error) before they
write anything, so a command run from the wrong folder is visible at once.

### Credential vault

Marketplace secrets and tokens are stored encrypted in the `credenciais`
table of the SQLite database (`core/cofre.py`), with Fernet from the
`cryptography` package. The key is separate from the buyer-data key and is
never stored in the database or the repository:

1. `CHAVE_COFRE` in the system environment — one Fernet key, or a
   comma-separated list where the first key encrypts and the others only
   decrypt. A `CHAVE_COFRE` line in `.env` is ignored, so the key never sits
   in the file the secrets used to live in.
2. Otherwise `.chave_cofre` in the data folder, created the first time a
   secret or token is saved (one key per line, same order rule). On Linux and
   macOS it is created with mode `600`; on Windows Python cannot restrict it
   and it inherits the data folder's permissions, so keep that folder inside
   your user profile. Creating it records an event asking you to back it up.

**Back up the key.** Without it the vault cannot be opened, and every write
is refused while encrypted entries remain. If the key is lost, run
`python cli.py cofre apagar --tudo`: it deletes every entry without needing
the key (after you type `APAGAR` to confirm). Then reconnect every
marketplace and type the secrets again.

**Each value is tied to its row.** A value is encrypted together with its
provider and name, so a ciphertext copied into another row by someone who
can write to the database, but has no key, is refused without being shown.
Entries written by an earlier build of the vault must be re-tied once with
`cofre rotacionar` (or deleted with `cofre apagar`); until then reading them
fails with a message saying so.

**Single-use refresh tokens.** Mercado Livre and Shopee replace the refresh
token on every renewal. The vault is checked before the old token is sent;
only one renewal per marketplace runs at a time, across threads and across
processes (the panel and a `cli.py rodar` next to it), and whoever waited
re-reads the vault instead of sending a token that was already used. The new
pair is kept in memory before it is written; if the write fails (database
locked by another program, key file being edited), the token keeps working
from memory, a warning event says not to restart the program, and each later
use retries the save.

**Which value is used.** A secret is read when it is needed, not at start-up:
first the system environment (so a server or CI can inject it), then the
vault, then a plaintext value still sitting in `.env`, which keeps working
until you migrate it. Refresh tokens that Mercado Livre and Shopee replace on
every renewal are the exception: the newest one, in the vault, wins, and an
environment value only seeds it.

**Fails closed.** A wrong, malformed or missing key raises a clear error and
never falls back to plaintext; the vault never generates a new key over
existing entries and refuses writes with a key that does not open them. The
worker records the error and keeps running; the Conexões tab shows it.
Neither values nor keys appear in events, errors or API responses.

**Legacy token files.** On first use, each connector imports
`.token_ml.json` / `.token_shopee.json` into the vault if the vault has no
tokens for that marketplace, checks the copy and records a warning. The
import re-checks inside its own transaction and never replaces an existing
entry, so it cannot overwrite a token renewed in the meantime. The file is
never changed or deleted; remove it by hand after checking the connection.

Commands (none of them prints a value or a key):

```bash
python cli.py cofre migrar       # copy .env secrets and legacy token files into the vault
python cli.py cofre rotacionar   # re-encrypt every entry with the current key
python cli.py cofre listar       # provider, name and date of each entry
python cli.py cofre apagar mercadolivre refresh_token   # revoke one entry
python cli.py cofre apagar --tudo   # lost key: delete every entry, asks for confirmation
```

- **Migration** is opt-in. It copies each secret found in `.env` and the
  legacy token files, reads every copy back, and prints the `.env` lines and
  files you may now delete by hand. It does not modify `.env` or the token
  files.
- **Rotation:** generate a key with
  `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`,
  stop the panel, set `CHAVE_COFRE=new,old` (or put the new key on the first
  line of `.chave_cofre`), run `cofre rotacionar`, keep only the new key,
  back it up and start the panel again. If an entry does not open with any
  key, nothing is re-encrypted.
- **Revocation:** revoke the credential in the marketplace portal and remove
  it with `cofre apagar`; this works even without the key.
- **Lost key:** `cofre apagar --tudo` removes every entry without the key;
  the current key, if any, is kept for the next writes. Then reconnect and
  type the secrets again.

The vault protects a copied, shared or backed-up database. It does not
protect against someone who controls the computer, because the key is on the
same machine.

### Claude API

Reply drafts use the official `anthropic` Python SDK
(`atendimento/claude_api.py`); there are no hand-written HTTP calls.

- **Model:** `MODELO_CLAUDE`, default `claude-opus-5-5` — $4 per million
  input tokens and $20 per million output tokens. `claude-sonnet-5-5` costs
  $2 / $10. The program never changes this setting on its own; the choice is
  yours. The one exception is Anthropic's refusal fallback below.
- **Effort:** `ESFORCO_CLAUDE`, default `low`, which suits short drafts
  (`low`, `medium`, `high`, `xhigh`, `max`). It is always sent explicitly as
  `output_config.effort`. On Claude Opus 5.5 thinking is always on, so the
  request carries no temperature, `top_p`, `top_k`, thinking budget, disabled
  thinking, assistant prefill or forced tool choice (each of them is a 400).
  Thinking counts toward `max_tokens` and grows with effort, so `max_tokens`
  and the timeout follow the effort: `low` 4000 tokens / 60 s, `medium`
  8000 / 120 s, `high`, `xhigh` and `max` 16000 / 300 s. Higher effort means
  more cost and longer waits. The system prompt keeps the answer to three
  sentences.
- **Refusal fallback:** always on (there is no setting to turn it off) — beta header
  `server-side-fallback-2026-07-01` with `fallbacks: "default"`, so a policy
  decline is retried server-side on the model Anthropic recommends. That
  answer comes from, and is billed at the price of, the fallback model (for
  example `claude-opus-4-8`, which costs more than Claude Opus 5.5); an event
  names the model that answered.
- **Each question once:** the worker sends a question to the model only once.
  Questions already queued, escalated, or approved in simulation are skipped
  on later cycles, even though Mercado Livre still lists them as unanswered.
  Only a temporary failure (rate limit, 5xx, timeout, network, missing key or
  vault) is retried on the next cycle, without a second warning. The one
  exception is a queued draft that was deleted (`demo.py` wipes the queue):
  that question is drafted again. An approved reply that was not published —
  approved in simulation, or whose publication failed (Mercado Livre error,
  network, token renewal) — goes back to the queue with the same text,
  without a new model call. A draft you refuse in the queue is not redrafted;
  answer that question on Mercado Livre.
- **No draft without a clean finish:** the stop reason is checked before the
  content. A refusal of the whole chain (`refusal`, with its category when
  the API gives one), a draft cut at `max_tokens`, any other stop reason, an
  empty draft, a missing key or an API error never becomes a queued answer;
  the question is escalated to a person, like questions about refunds or
  warranty. Only `text` blocks are read. Timeouts and HTTP 400 have their own
  messages (a 400 names the error type and points to `MODELO_CLAUDE` and the
  fallback beta). The SDK also retries connection errors, timeouts, 429 and
  5xx twice.
- **Key and privacy:** the key is read from the credential vault and passed
  explicitly to the client, whose address is fixed to
  `https://api.anthropic.com`, so an `ANTHROPIC_BASE_URL` in the environment
  cannot send the key elsewhere. Neither the key, the buyer's question nor
  the model's text goes to the event log or to error messages: an escalation
  is logged with the question ID and a fixed reason. This holds for events
  written from this version on; escalation events written by earlier
  versions stored the buyer's question (and the model's escalation text) in
  `eventos.detalhe_json`, and nothing removes those rows automatically.
- **Testar conexão** calls `models.retrieve` for the configured model: it
  checks the key and access to the model without sending a message, so it
  costs no tokens. It does not check billing or access to the fallback beta;
  those show up only when a draft is requested.

### Variables

| Variables | Default | Purpose |
|---|---|---|
| `ML_CLIENT_ID`, `ML_CLIENT_SECRET` | empty | Mercado Livre application; the panel stores the secret in the vault |
| `ML_REFRESH_TOKEN` | empty | Stored in the vault by the panel after authorization; a value here only seeds the vault |
| `ML_SELLER_ID` | empty | Filled by the panel after authorization |
| `ML_SITE_ID` | `MLB` | Mercado Livre site |
| `SHOPEE_PARTNER_ID`, `SHOPEE_PARTNER_KEY`, `SHOPEE_SHOP_ID` | empty | Shopee Open Platform application and shop; the panel stores the partner key in the vault |
| `SHOPEE_REFRESH_TOKEN` | empty | Stored in the vault by the panel after authorization; a value here only seeds the vault |
| `SHOPEE_SANDBOX` | `true` | Use the Shopee test environment |
| `AMZ_LWA_CLIENT_ID`, `AMZ_LWA_CLIENT_SECRET`, `AMZ_REFRESH_TOKEN` | empty | Amazon SP-API credentials; the panel stores the secret and the refresh token in the vault |
| `AMZ_MARKETPLACE_ID`, `AMZ_ENDPOINT` | Brazil marketplace, North America endpoint | Amazon marketplace and region |
| `ANTHROPIC_API_KEY` | empty | Reply drafting, stored in the vault by the panel; without it only fixed FAQ answers are queued and other questions are escalated |
| `MODELO_CLAUDE` | `claude-opus-5-5` | Claude model for reply drafts ($4 / $20 per million input / output tokens); `claude-sonnet-5-5` costs $2 / $10. Your decision; the program never changes it (a refused request may be answered, and billed, by Anthropic's fallback model) |
| `ESFORCO_CLAUDE` | `low` | Effort for reply drafts: `low`, `medium`, `high`, `xhigh` or `max`; it also sets `max_tokens` and the timeout; an unknown value falls back to `low` with a warning |
| `CHAVE_COFRE` | not set | [Credential vault](#credential-vault) key; read only from the real environment, never from `.env`; when not set, `.chave_cofre` is used |
| `MARGEM_MINIMA_PCT` | `18` | Minimum net margin (%) |
| `TETO_COMPRA_AUTOMATICA` | `300` | Only a label: purchases up to this value show as `ROTINA`, above it as `ACIMA DO TETO`. Nothing is bought automatically; every purchase needs approval (the name is historical) |
| `ALIQUOTA_IMPOSTO_PCT` | `4` | Estimated tax on sales (%) |
| `PRAZO_FORNECEDOR_DIAS` | `5` | Default supplier lead time (days) |
| `EMITE_NOTA_FISCAL` | empty | Whether you issue an invoice on every sale: `true` or `false`. Empty blocks purchases in compliance until you answer; `false` is a hard block |
| `AGENTE_DADOS` | not set | [Data folder](#data-folder); read only from the real environment, never from `.env` |
| `DB_PATH` | `agente.db` | SQLite database file; a relative path is resolved against the data folder |
| `CHAVE_LGPD` | empty | Buyer-data encryption key; when empty, `.chave_lgpd` is used |
| `RETENCAO_PII_DIAS` | `1825` | Days before buyer data is purged |
| `HOST_PAINEL` | `127.0.0.1` | Panel address; keep it while there is a single operator and no HTTPS |
| `SESSAO_OCIOSA_MIN`, `SESSAO_MAX_HORAS` | `30`, `8` | Idle timeout and maximum session lifetime |
| `WORKER_ATIVO`, `ABRIR_NAVEGADOR` | `true` | Start the worker; open the browser |
| `PORTA_PAINEL`, `INTERVALO_WORKER` | `8777`, `300` | Panel port (1–65535) and worker interval in seconds (at least 30), from the environment or `.env`; a bad value falls back to the default with a warning |
| `MODO_SIMULACAO` | `true` | On: approving records what would happen and sends nothing — no buyer reply, no price change — and the purchase order file is marked as a test; the order or question keeps waiting and returns to the queue once it is off. Only `false`, `0`, `no`, `off` or `nao` turns it off |

---

## Connecting marketplaces

The **Conexões** tab lists, for each marketplace, the steps, the developer
portal and the redirect URI to register. None of these flows has been validated
with a live account yet.

- **Mercado Livre:** create the app in the DevCenter, register
  `https://localhost:8777/oauth/ml/retorno` (with your `PORTA_PAINEL` if you
  changed it), paste the App ID and Secret Key,
  click authorize, then paste the whole return URL into the panel. The browser
  shows a connection error on that URL because the panel serves plain HTTP; the
  code is read from the pasted URL. The URL is accepted only from the same
  panel session that started the authorization, once, within 10 minutes;
  pasting only the code is refused.
- **Shopee:** paste the Partner ID, Partner Key and Shop ID, then authorize.
  The authorization link expires after 5 minutes.
- **Amazon:** paste the LWA client ID, client secret and a refresh token issued
  in Seller Central.

**Testar conexão** calls the real API; for Claude it is a model lookup that
costs no tokens. Secrets and tokens go to the encrypted
[credential vault](#credential-vault); IDs, the redirect URI and business
settings go to `.env`. The save response and the event log carry key names
only, never values.

---

## Design decisions

**People release irreversible actions.** Purchases, public buyer replies, price
changes and new listings are queue item types, and the worker never calls their
executors. The queue receives the executors by injection, so it does not import
marketplace code.

**Compliance fails closed.** One check (`conformidade.verificar_pendencia`)
runs before a purchase order is queued, when the panel lists the queue, and
inside `aprovacao.aprovar`, which both the panel and `cli.py aprovar` go
through; a blocked item is refused with the same reason in both (HTTP 409 in
the panel). A rule that applies and lacks its data blocks the item as "needs
confirmation", naming the missing fact and where to fill it in; no default
stands in for a confirmation. Product and supplier facts are read from the
database at check time, so a confirmation filled in later releases an item
that is already waiting. A purchase that breaks a rule, such as Amazon's
dropshipping policy, goes to `PROBLEMA` and never reaches the queue. Limits:
buyer replies are checked only by a detection rule that nothing sets yet —
each reply is read by a person before approval, and questions about returns
or warranty are escalated before any draft; the rules target Brazilian
marketplaces.

**Simulation by default.** With `MODO_SIMULACAO` on, each executor records
what it would do and returns a result that starts with `[SIMULAÇÃO]`, stored
with the approval. A simulated purchase or reply approval does not consume
the item: a simulated purchase leaves the order in `AGUARDANDO_APROVACAO`,
and once simulation is off the worker queues that purchase again and puts
the same reply text back in the queue. A simulated price change is only
recorded and is not queued again. Ingestion and drafting run in both modes, so
with Mercado Livre connected the queue holds real orders and questions. Only
an explicit "no" turns simulation off, so a typo stays on the safe side.

**Explicit state.** Orders change state only through `transicionar()`, which
validates the transition and records it in `transicoes`.

**Privacy in storage and on screen.** Buyer name, buyer ID and shipping
reference are encrypted when an order arrives, and the key lives outside the
database. API responses mask the name; the reveal endpoint logs every read.

**Secrets encrypted at rest, with a lifecycle.** Marketplace secrets and
tokens live in the vault under their own key: generated on first use or
injected through `CHAVE_COFRE`, read through one helper at use time, rotated
with MultiFernet, revoked per entry and recovered only from a backup of the
key. Nothing is deleted automatically: migrations copy, verify and tell the
operator what can be removed. Follows the OWASP Cryptographic Storage Cheat
Sheet and the cryptography and secret-management chapters of OWASP ASVS 5.0.

---

## Known limitations

- One shared operator, no roles and no HTTPS by default. Keep the panel on `127.0.0.1` until both exist.
- Only Mercado Livre is automated; Shopee and Amazon stop at the connection test.
- No integration has been validated with a live seller account.
- Purchase orders are text files, with or without simulation mode; nothing reaches suppliers.
- Margins rely on average fees; confirm the real fees of each category.
- Compliance confirmations are set with SQL on `produtos` (`categoria_regulada`, `habilitacao_confirmada`, `reembalagem_confirmada`), on `fornecedores` for Amazon orders (`canal` must be `email` or `whatsapp`; `api` or `portal` means direct shipping and is refused) and with `EMITE_NOTA_FISCAL` in `.env`, which needs a restart; the panel has no form for them yet.
- Reply drafting has not been run against the live Claude API; it is tested with a fake client and with the real SDK over a mock transport.
- The vault key sits on the same machine as the database (environment variable or `.chave_cofre`): it protects copies of the database, not a compromised computer. On Windows `.chave_cofre` inherits the folder's permissions.
- A renewed refresh token that could not be saved lives only in the running process until a later save succeeds; restarting in that window means authorizing the account again. A warning event appears when this happens.
- A refresh token set in the environment only seeds the vault. To replace a stale one, reconnect in the panel or remove it with `cofre apagar`.
- Plaintext secrets left in `.env` still work until you migrate them and delete the lines by hand. The panel warns only when you save a secret that still has a line in `.env`; `cofre migrar` lists them all.
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
                         conformidade (rules), privacidade (LGPD),
                         seguranca (login), cofre (credential vault)
conectores/              mercadolivre, shopee, amazon
inteligencia/            precificacao (margin), tendencias (niche research)
atendimento/             persona, reply bot and Claude API client (official SDK)
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

Python · FastAPI and Uvicorn · SQLite · cryptography (Fernet, MultiFernet) · requests ·
pytrends and pandas (niche research) · Claude API through the official
`anthropic` SDK (optional, reply drafting) ·
plain HTML, CSS and JavaScript for the panel

---

## License

MIT — see [LICENSE](LICENSE).
