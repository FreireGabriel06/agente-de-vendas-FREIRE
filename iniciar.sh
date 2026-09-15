#!/usr/bin/env bash
# Instala na primeira vez, só executa nas seguintes.
#
#   chmod +x iniciar.sh && ./iniciar.sh
#
set -euo pipefail
cd "$(dirname "$0")"

VERDE='\033[0;32m'; AMARELO='\033[0;33m'; VERMELHO='\033[0;31m'; FIM='\033[0m'
ok(){ echo -e "${VERDE}✓${FIM} $1"; }
aviso(){ echo -e "${AMARELO}!${FIM} $1"; }
erro(){ echo -e "${VERMELHO}✗${FIM} $1" >&2; }

echo
echo "  Agente Comercial"
echo "  ────────────────"
echo

# ---------- Python ----------
if command -v python3 >/dev/null 2>&1; then PY=python3
elif command -v python >/dev/null 2>&1; then PY=python
else
  erro "Python não encontrado."
  echo "  Instale em https://python.org/downloads (versão 3.10 ou maior)."
  exit 1
fi

VERSAO=$($PY -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
MENOR=$($PY -c 'import sys; print(1 if sys.version_info < (3,10) else 0)')
if [ "$MENOR" = "1" ]; then
  erro "Python $VERSAO é antigo demais. O projeto precisa de 3.10 ou maior."
  exit 1
fi
ok "Python $VERSAO"

# ---------- ambiente virtual ----------
if [ ! -d ".venv" ]; then
  echo "  Criando ambiente virtual (só nesta primeira vez)..."
  $PY -m venv .venv || {
    erro "Falha ao criar o venv."
    echo "  No Debian/Ubuntu: sudo apt install python3-venv"
    exit 1
  }
  ok "Ambiente criado"
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# ---------- dependências ----------
if [ ! -f ".venv/.instalado" ]; then
  echo "  Instalando dependências (leva 1 a 3 minutos)..."
  pip install --quiet --upgrade pip
  pip install --quiet -r requirements.txt || { erro "Falha ao instalar dependências."; exit 1; }
  touch .venv/.instalado
  ok "Dependências instaladas"
else
  ok "Dependências já instaladas"
fi

# ---------- configuração ----------
if [ ! -f ".env" ]; then
  cp .env.example .env
  aviso "Criado .env a partir do modelo — ainda SEM credenciais."
  echo "    O painel sobe e funciona, mas não vai buscar pedido nenhum"
  echo "    até você preencher as chaves do marketplace no arquivo .env."
  echo
fi

# ---------- estrutura ----------
if [ ! -f "core/privacidade.py" ]; then
  aviso "Arquivos fora de lugar (pasta plana). Organizando..."
  $PY organizar.py || { erro "Não consegui organizar. Baixe o .zip completo."; exit 1; }
  echo
fi

# ---------- banco ----------
if [ ! -f "agente.db" ]; then
  if $PY -c "
import sys; sys.path.insert(0,'.')
from db import inicializar
from core.privacidade import inicializar_lgpd
inicializar(); inicializar_lgpd()
"; then
    ok "Banco criado e chave LGPD gerada"
    aviso "Faça backup do arquivo .chave_lgpd — sem ele os dados de comprador ficam ilegíveis."
    echo
  else
    erro "Falha ao criar o banco. Veja a mensagem acima."
    exit 1
  fi
fi

# ---------- dados de exemplo (só na primeira execução, e só com terminal) ----------
if [ ! -f ".primeira_execucao" ]; then
  touch .primeira_execucao
  if [ -t 0 ]; then
    echo "  Carregar dados de exemplo pra você ver o painel funcionando?"
    read -r -p "  [S/n] " resp
    if [[ ! "$resp" =~ ^[Nn] ]]; then
      $PY demo.py >/dev/null 2>&1 && ok "Dados de exemplo carregados"
    fi
    echo
  fi
fi

echo "  Abrindo o painel em http://127.0.0.1:8777"
echo "  Ctrl+C encerra."
echo
exec $PY executar.py
