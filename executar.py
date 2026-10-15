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


# Pedido de parada do worker: o laço termina o ciclo em andamento e não
# começa outro. Ctrl+C no painel aciona (main, depois de o uvicorn parar).
PARAR = threading.Event()
PARADA_MAXIMA_S = 30


def _laco_worker(parar: threading.Event = PARAR):
    """Worker em segundo plano. Falha num ciclo não derruba o painel. Com outro
    ciclo em andamento (a tecla 'c' do painel), worker.ciclo pula a passada."""
    from core.erros import mensagem_segura, pilha_sem_mensagem
    from db import definir_ator, registrar_evento
    from worker import ciclo

    definir_ator("worker")
    # Espera o painel subir antes do primeiro ciclo.
    if parar.wait(5):
        return
    while not parar.is_set():
        try:
            ciclo()
        except Exception as e:
            try:
                # Só o tipo e a pilha: a mensagem pode repetir um valor decifrado.
                registrar_evento("erro", "executar", f"Ciclo falhou: {mensagem_segura(e)}",
                                 {"traceback": pilha_sem_mensagem(e)})
            except Exception:
                pass
        parar.wait(intervalo())


def _abrir_navegador():
    time.sleep(1.5)
    webbrowser.open(f"http://127.0.0.1:{porta()}")


def opcoes_https() -> tuple[dict, str | None]:
    """Como servir HTTPS (lote S4; achados S-10 e S-25). Devolve as opções do
    uvicorn e um erro, se a configuração estiver pela metade.

    - TLS_CERT e TLS_CHAVE: o próprio painel serve HTTPS com esse certificado.
    - PROXY_HTTPS=true: um proxy na frente (Caddy, nginx) faz o HTTPS e manda
      X-Forwarded-Proto; o painel só confia nesse cabeçalho vindo de PROXY_IPS
      (padrão 127.0.0.1)."""
    cert, chave = os.getenv("TLS_CERT", "").strip(), os.getenv("TLS_CHAVE", "").strip()
    if bool(cert) != bool(chave):
        return {}, "Defina TLS_CERT e TLS_CHAVE juntos (certificado e chave privada)."
    if cert:
        for caminho in (cert, chave):
            if not os.path.isfile(caminho):
                return {}, f"Arquivo de TLS não encontrado: {caminho}"
        return {"ssl_certfile": cert, "ssl_keyfile": chave}, None
    if os.getenv("PROXY_HTTPS", "").strip().lower() in ("1", "true", "sim", "yes"):
        return {"proxy_headers": True,
                "forwarded_allow_ips": os.getenv("PROXY_IPS", "127.0.0.1")}, None
    return {}, None


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

    https, erro = opcoes_https()
    if erro:
        print(f"Recusando iniciar: {erro}")
        return
    # Fora do endereço local, só com HTTPS: senha, sessão e dado de comprador
    # não podem passar em texto pela rede.
    if not local and not https:
        print(f"Recusando iniciar em {host} sem HTTPS. Defina TLS_CERT e TLS_CHAVE, ou "
              "PROXY_HTTPS=true com um proxy HTTPS na frente.")
        return
    # O painel só atende pelos nomes de HOSTS_PAINEL (e pelos locais); sem
    # eles, toda chamada de fora levaria 400.
    if not local and not os.getenv("HOSTS_PAINEL", "").strip():
        print(f"Recusando iniciar em {host} sem HOSTS_PAINEL: defina o nome (ou IP) pelo "
              "qual o painel é acessado, por exemplo HOSTS_PAINEL=painel.exemplo.com.br")
        return
    # Exposto, todo operador ativo entra com o app autenticador (DPP 1.4.1).
    sem_mfa = seguranca.operadores_sem_mfa() if not local else []
    if sem_mfa:
        print(f"Recusando iniciar em {host}: operador(es) sem MFA: {', '.join(sem_mfa)}. "
              "Ative com: python cli.py operador --usuario NOME --mfa-ativar")
        return

    worker_thread = None
    if os.getenv("WORKER_ATIVO", "true").lower() == "true":
        worker_thread = threading.Thread(target=_laco_worker, daemon=True, name="worker")
        worker_thread.start()
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

    esquema = "https" if "ssl_certfile" in https else "http"
    print(f"Painel em {esquema}://127.0.0.1:{porta()}  (Ctrl+C encerra)")

    import uvicorn
    try:
        uvicorn.run(app, host=host, port=porta(), log_level="warning", **https)
    finally:
        # Deixa o ciclo em andamento terminar (o banco fica coerente) antes de
        # o processo sair; um ciclo travado não segura a saída por mais que isto.
        PARAR.set()
        if worker_thread is not None:
            worker_thread.join(timeout=PARADA_MAXIMA_S)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nEncerrado.")
