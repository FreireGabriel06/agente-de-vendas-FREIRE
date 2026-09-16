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

O binário sai em dist/. Ele carrega o .env da pasta onde for executado, então
você distribui o executável e o .env lado a lado e não recompila pra trocar
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

PORTA = int(os.getenv("PORTA_PAINEL", "8777"))
INTERVALO = int(os.getenv("INTERVALO_WORKER", "300"))


def _laco_worker():
    """Worker em segundo plano. Falha num ciclo não derruba o painel."""
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
        time.sleep(INTERVALO)


def _abrir_navegador():
    time.sleep(1.5)
    webbrowser.open(f"http://127.0.0.1:{PORTA}")


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
        print(f"Worker ativo, ciclo a cada {INTERVALO}s.")
    else:
        print("Worker desligado (WORKER_ATIVO=false). Use a tecla 'c' no painel "
              "pra rodar um ciclo manual.")

    if os.getenv("ABRIR_NAVEGADOR", "true").lower() == "true":
        threading.Thread(target=_abrir_navegador, daemon=True).start()

    print(f"Painel em http://127.0.0.1:{PORTA}  (Ctrl+C encerra)")

    import uvicorn
    uvicorn.run(app, host=host, port=PORTA, log_level="warning")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nEncerrado.")
