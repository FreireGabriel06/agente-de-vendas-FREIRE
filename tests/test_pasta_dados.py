"""Pasta de dados (config.DATA_DIR) e as travas dos próprios testes."""
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
import requests

import config

RAIZ = Path(__file__).resolve().parent.parent


# ------------------------------------------------------- escolha da pasta

def test_agente_dados_vem_primeiro_e_a_pasta_e_criada(monkeypatch, tmp_path):
    destino = tmp_path / "dados" / "agente"
    monkeypatch.setenv("AGENTE_DADOS", str(destino))
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    assert config._pasta_dados() == destino.resolve()
    assert destino.is_dir()


def test_binario_usa_a_pasta_do_executavel(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENTE_DADOS")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "dist" / "agente-comercial.exe"))

    assert config._pasta_dados() == (tmp_path / "dist").resolve()


def test_codigo_fonte_usa_a_pasta_do_codigo(monkeypatch):
    monkeypatch.setenv("AGENTE_DADOS", "   ")  # vazia conta como ausente
    monkeypatch.delattr(sys, "frozen", raising=False)

    assert config._pasta_dados() == config.BASE_DIR == RAIZ


def test_db_path_relativo_conta_a_partir_da_pasta_de_dados(monkeypatch, tmp_path):
    monkeypatch.delenv("DB_PATH", raising=False)
    assert config._caminho_banco() == str(config.DATA_DIR / "agente.db")

    monkeypatch.setenv("DB_PATH", "agente.db")  # o valor do .env.example
    assert config._caminho_banco() == str(config.DATA_DIR / "agente.db")

    monkeypatch.setenv("DB_PATH", str(tmp_path / "outro.db"))
    assert config._caminho_banco() == str(tmp_path / "outro.db")


def test_arquivos_de_execucao_ficam_na_pasta_de_dados(tmp_path):
    """Importação limpa, em outro processo: o .env vem de AGENTE_DADOS e todo
    arquivo de execução deriva dela. O código continua em BASE_DIR."""
    dados = tmp_path / "dados"
    dados.mkdir()
    (dados / ".env").write_text("MARGEM_MINIMA_PCT=42\nDB_PATH=banco/teste.db\n", encoding="utf-8")
    script = (
        "import json, config, worker\n"
        "from painel import configurar\n"
        "from core import privacidade\n"
        "from conectores import mercadolivre, shopee\n"
        "print(json.dumps({\n"
        "    'DATA_DIR': str(config.DATA_DIR), 'BASE_DIR': str(config.BASE_DIR),\n"
        "    'margem': config.config.negocio.margem_minima_pct,\n"
        "    'banco': config.config.db_path, 'env': str(configurar.ARQ_ENV),\n"
        "    'chave': str(privacidade.ARQ_CHAVE), 'token_ml': str(mercadolivre.ARQ_TOKEN),\n"
        "    'token_shopee': str(shopee.ARQ_TOKEN), 'ordens': str(worker.PASTA_ORDENS),\n"
        "}))\n"
    )
    ambiente = {**os.environ, "AGENTE_DADOS": str(dados)}
    feito = subprocess.run([sys.executable, "-c", script], cwd=RAIZ, env=ambiente,
                           capture_output=True, text=True, timeout=120)
    assert feito.returncode == 0, feito.stderr
    caminhos = json.loads(feito.stdout)
    dados = dados.resolve()

    assert Path(caminhos.pop("DATA_DIR")) == dados
    assert Path(caminhos.pop("BASE_DIR")) == RAIZ
    assert caminhos.pop("margem") == 42.0  # o .env lido foi o da pasta de dados
    assert Path(caminhos.pop("banco")) == dados / "banco" / "teste.db"
    assert {nome: Path(c) for nome, c in caminhos.items()} == {
        "env": dados / ".env",
        "chave": dados / ".chave_lgpd",
        "token_ml": dados / ".token_ml.json",
        "token_shopee": dados / ".token_shopee.json",
        "ordens": dados / "ordens_de_compra",
    }


# ------------------------------------------------------- travas dos testes

def test_testes_nao_usam_a_pasta_do_projeto(isolamento):
    assert config.DATA_DIR != config.BASE_DIR
    assert Path(config.config.db_path).parent == isolamento
    assert not Path(config.config.db_path).is_relative_to(RAIZ)


def test_rede_externa_e_bloqueada_e_loopback_nao(sem_rede):
    with pytest.raises(RuntimeError, match="conexão externa bloqueada"):
        socket.create_connection(("192.0.2.1", 443), timeout=0.2)  # TEST-NET-1
    with pytest.raises(RuntimeError, match="conexão externa bloqueada"):
        requests.get("https://api.mercadolibre.com/sites/MLB", timeout=0.2)
    assert len(sem_rede) == 2
    sem_rede.clear()  # as duas tentativas eram o objetivo deste teste

    with socket.create_server(("127.0.0.1", 0)) as servidor:
        with socket.create_connection(servidor.getsockname(), timeout=2):
            pass
    assert sem_rede == []


def test_proxy_no_loopback_nao_fura_a_trava(sem_rede, monkeypatch):
    """Um proxy local receberia o pedido no 127.0.0.1 e sairia para a rede sem
    passar pela trava. O conftest desliga o uso de proxy."""
    with socket.create_server(("127.0.0.1", 0)) as proxy:
        proxy.setblocking(False)
        for nome in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            monkeypatch.setenv(nome, "http://%s:%d" % proxy.getsockname())
        assert requests.utils.get_environ_proxies("https://api.mercadolibre.com") == {}
        with pytest.raises(RuntimeError, match="conexão externa bloqueada"):
            requests.get("https://api.mercadolibre.com/sites/MLB", timeout=0.2)
        with pytest.raises(BlockingIOError):
            proxy.accept()  # ninguém bateu no proxy
    assert sem_rede == ["api.mercadolibre.com"]
    sem_rede.clear()
