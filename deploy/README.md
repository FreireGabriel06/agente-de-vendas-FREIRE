# Deploy

One server runs the public website and one isolated instance per client,
behind [Caddy](https://caddyserver.com/), which obtains and renews the HTTPS
certificates by itself. Each instance has its own volume with its database,
keys and backups. Tested in CI by building the image and starting it.

## What you need

- A Linux server with Docker and Docker Compose (any small VPS works; 1 vCPU
  and 1 GB of RAM are enough for a few clients).
- A domain whose `A` records point to the server: `example.com`,
  `www.example.com` and one name per client (`cliente1.example.com`).

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

Copy the `cliente1` service and its volume in `docker-compose.yml`, add a
`cliente2.{$DOMINIO}` block to the `Caddyfile`, create
`instancias/cliente2.env`, register the new redirect URI in the Amazon app and
run `docker compose up -d --build`.

## Updating

```bash
git pull && docker compose up -d --build
```

Data stays in the volumes. Take a backup first.
