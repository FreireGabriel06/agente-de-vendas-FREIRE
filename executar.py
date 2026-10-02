#!/usr/bin/env python3
"""
Ponto de entrada único.

    python executar.py

Sobe três coisas no mesmo processo:
  - o banco (cria se não existir)
  - o worker, em thread de fundo, rodando o ciclo no intervalo configurado
  - o painel, e abre o navegador nele

Para gerar o binário (.exe no Windows, executável no Linux/macOS):

    pip install pyinstaller
    pyinstaller agente.spec

O binário sai em dist/. Ele lê o .env, e grava banco, chave e tokens, na pasta
do próprio executável (ou em AGENTE_DADOS, se definida no sistema), então você
distribui o executável e o .env lado a lado e não recompila pra trocar
credencial.
"""
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

# Importar o config carrega o .env da pasta de dados. A porta e o intervalo
# vêm dele (config.porta_painel, config.intervalo_worker), lidos na hora do
# uso: antes, eram lidos do ambiente aqui, na importação, quando o .env ainda
# não tinha sido carregado, e os valores do .env eram ignorados.
from config import config  # noqa: E402


def porta() -> int:
    """PORTA_PAINEL, do ambiente ou do .env (padrão 8777)."""
    return config.porta_painel


def intervalo() -> int:
    """INTERVALO_WORKER em segundos, do ambiente ou do .env (padrão 300)."""
    return config.intervalo_worker


def _laco_worker():
    """Worker em segundo plano. Falha num ciclo não derruba o painel. Com outro
    ciclo em andamento (a tecla 'c' do painel), worker.ciclo pula a passada."""
    from worker import ciclo
    from db import registrar_evento

    # Espera o painel subir antes do primeiro ciclo.
    time.sleep(5)
    while True:
        try:
            ciclo()
        except Exception as e:
            try:
                registrar_evento("erro", "executar", f"Ciclo falhou: {e}")
            except Exception:
                pass
        time.sleep(intervalo())


def _abrir_navegador():
    time.sleep(1.5)
    webbrowser.open(f"http://127.0.0.1:{porta()}")


def main():
    from painel.app import app, preparar
    from core import seguranca

    preparar()

    # Fail closed: sem operador o painel não deixa ninguém entrar, e fora do
    # endereço local ele ficaria exposto antes de existir login utilizável.
    host = os.getenv("HOST_PAINEL", "127.0.0.1")
    local = host in ("127.0.0.1", "localhost", "::1")
    if not seguranca.existe_operador():
        print("Nenhum operador cadastrado. Crie um antes de usar o painel:")
        print("    python cli.py operador --usuario SEU_USUARIO")
        if not local:
            print(f"Recusando iniciar em {host} sem operador.")
            return

    if os.getenv("WORKER_ATIVO", "true").lower() == "true":
        threading.Thread(target=_laco_worker, daemon=True).start()
        print(f"Worker ativo, ciclo a cada {intervalo()}s.")
    else:
        print("Worker desligado (WORKER_ATIVO=false). Use a tecla 'c' no painel "
              "pra rodar um ciclo manual.")

    if config.modo_simulacao:
        print("Modo simulação ligado (MODO_SIMULACAO=true): aprovar não publica "
              "resposta, não muda preço e marca a ordem de compra como teste. O pedido "
              "e a pergunta continuam esperando: voltam para a fila quando você "
              "desligar a simulação.")

    if os.getenv("ABRIR_NAVEGADOR", "true").lower() == "true":
        threading.Thread(target=_abrir_navegador, daemon=True).start()

    print(f"Painel em http://127.0.0.1:{porta()}  (Ctrl+C encerra)")

    import uvicorn
    uvicorn.run(app, host=host, port=porta(), log_level="warning")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nEncerrado.")
