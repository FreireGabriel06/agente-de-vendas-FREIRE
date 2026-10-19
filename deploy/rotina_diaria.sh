#!/bin/sh
# Daily routine for every client instance: health check, encrypted backup,
# copy of the backups to the host (deploy/backups/<client>/) and rotation.
#
# The backup password is read from deploy/.backup_senha (ignored by git;
# chmod 600) and passed to the container through stdin, never as an argument.
# Keep a copy of the password away from this server.
#
# Run it from cron, for example at 03:30:
#   echo "30 3 * * * root /root/agente/deploy/rotina_diaria.sh" > /etc/cron.d/agente
#
# Exit code: 0 all fine, 1 a health check asked for attention, 2 a failure.
set -u

AQUI=$(cd "$(dirname "$0")" && pwd)
cd "$AQUI" || exit 2
DOCKER=${DOCKER:-docker}
MANTER=${BACKUP_MANTER:-14}
SENHA_ARQUIVO=${BACKUP_SENHA_ARQUIVO:-$AQUI/.backup_senha}
LOG=${ROTINA_LOG:-$AQUI/rotina.log}

pior=0
piorar() { [ "$1" -gt "$pior" ] && pior=$1; return 0; }
registrar() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') $*" >> "$LOG"; }

if [ ! -s "$SENHA_ARQUIVO" ]; then
	registrar "falha: $SENHA_ARQUIVO ausente ou vazio; backup não feito"
	echo "Crie $SENHA_ARQUIVO (chmod 600) com a senha do backup." >&2
	exit 2
fi

clientes=$($DOCKER compose config --services | grep '^cliente')
if [ -z "$clientes" ]; then
	registrar "falha: nenhum serviço cliente* no docker-compose"
	exit 2
fi

for c in $clientes; do
	$DOCKER compose exec -T "$c" python cli.py saude --completo >> "$LOG" 2>&1
	piorar $?
	registrar "$c: saúde verificada"

	if $DOCKER compose exec -T "$c" sh -c \
		'read -r BACKUP_SENHA; export BACKUP_SENHA; exec python cli.py backup criar --destino /dados/backups' \
		< "$SENHA_ARQUIVO" >> "$LOG" 2>&1; then
		registrar "$c: backup criado"
	else
		registrar "falha: $c: backup não criado"
		piorar 2
		continue
	fi

	# Rotation inside the volume: keep the newest $MANTER files.
	$DOCKER compose exec -T "$c" sh -c \
		"ls -1t /dados/backups/*.agvbkp | tail -n +$((MANTER + 1)) | xargs -r rm -f --" >> "$LOG" 2>&1

	mkdir -p "backups/$c" && chmod 700 backups "backups/$c"
	if $DOCKER compose cp "$c:/dados/backups/." "backups/$c/" >> "$LOG" 2>&1; then
		ls -1t "backups/$c"/*.agvbkp 2>/dev/null | tail -n +$((MANTER + 1)) | xargs -r rm -f --
		registrar "$c: backups copiados para deploy/backups/$c"
	else
		registrar "falha: $c: cópia dos backups para o servidor"
		piorar 2
	fi
done

registrar "fim: código $pior"
exit "$pior"
