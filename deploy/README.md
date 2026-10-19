# Deploy

One server runs the public website and one isolated instance per client,
behind [Caddy](https://caddyserver.com/), which obtains and renews the HTTPS
certificates by itself. Each instance has its own volume with its database,
keys and backups. Tested in CI by building the image and starting it.

## What you need

- A Linux server with Docker and Docker Compose (any small VPS works; 1 vCPU
  and 1 GB of RAM are enough for a few clients).
- A domain whose `A` records point to the server: `example.com` and a
  wildcard `*.example.com` (covers `www` and every client).

## First start

```bash
git clone https://github.com/FreireGabriel06/agente-de-vendas-FREIRE.git agente
cd agente/deploy
echo "DOMINIO=example.com" > .env
python3 preencher_site.py                      # business details for the website
mkdir -p instancias
cp instancia.exemplo.env instancias/cliente1.env   # edit: HOSTS_PAINEL, AMZ_REDIRECT_URI...

# The panel refuses to start exposed without an operator who has MFA:
docker compose run --rm cliente1 python cli.py operador --usuario someone
docker compose run --rm -it cliente1 python cli.py operador --usuario someone --mfa-ativar

docker compose up -d --build
```

Open `https://cliente1.example.com`, sign in, save the Amazon LWA client ID
and secret under Connections and authorize the seller account.

## Every day (automatic)

`rotina_diaria.sh` checks the health of every client instance, creates an
encrypted backup, copies the backups to `deploy/backups/<client>/` and keeps
the newest 14. Set it up once:

```bash
cd ~/agente/deploy
openssl rand -base64 24 > .backup_senha && chmod 600 .backup_senha
cat .backup_senha        # store this password away from the server
./rotina_diaria.sh && tail rotina.log
echo "30 3 * * * root $PWD/rotina_diaria.sh" > /etc/cron.d/agente
```

Copy `deploy/backups/` off the server from time to time
(`scp -r root@server:agente/deploy/backups .`). Point an uptime monitor at
`https://cliente1.example.com/saude` (it answers 503 when something needs
attention).

## Before the Amazon registration

From any computer with Python:

```bash
python deploy/verificar_site.py example.com
```

It checks HTTPS, the redirect from HTTP, the privacy policy and terms (no
unfilled field) and the client panel.

## Another client

```bash
python3 novo_cliente.py        # creates cliente2 (or: python3 novo_cliente.py cliente-name)
```

It writes `instancias/cliente2.env`, `docker-compose.override.yml` and
`clientes-caddy/cliente2.caddy`, then prints the commands to create the
operator, start the instance and reload Caddy, and the redirect URIs to add to
the Amazon and Mercado Livre apps. A wildcard DNS record (`*.example.com`)
avoids a DNS change per client.

## Updating

```bash
git pull && docker compose up -d --build
```

Data stays in the volumes. Take a backup first (`./rotina_diaria.sh`). The
database schema is versioned: an update migrates it on start, all at once or
not at all, and an older image refuses to start on a database a newer one
migrated. To go back, restore the backup taken before the update.
