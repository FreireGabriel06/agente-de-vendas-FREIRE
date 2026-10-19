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

## Every day

```bash
docker compose exec cliente1 python cli.py saude --completo
docker compose exec cliente1 python cli.py backup criar --destino /dados/backups
```

Copy the `.agvbkp` files off the server (they are encrypted; keep the backup
password elsewhere). Point an uptime monitor at `https://cliente1.example.com/saude`.

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
