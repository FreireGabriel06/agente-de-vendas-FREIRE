"""Pasta de dados (config.DATA_DIR) e as travas dos próprios testes."""
import json
import os
import socket
import subprocess
import sys
import textwrap
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
        "from core import cofre, privacidade\n"
        "from conectores import mercadolivre, shopee\n"
        "print(json.dumps({\n"
        "    'DATA_DIR': str(config.DATA_DIR), 'BASE_DIR': str(config.BASE_DIR),\n"
        "    'margem': config.config.negocio.margem_minima_pct,\n"
        "    'banco': config.config.db_path, 'env': str(configurar.ARQ_ENV),\n"
        "    'chave': str(privacidade.ARQ_CHAVE), 'chave_cofre': str(cofre.ARQ_CHAVE),\n"
        "    'trava_tokens': str(cofre.ARQ_TRAVA),\n"
        "    'token_ml': str(mercadolivre.ARQ_TOKEN),\n"
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
        "chave_cofre": dados / ".chave_cofre",
        "trava_tokens": dados / ".trava_tokens",
        "token_ml": dados / ".token_ml.json",
        "token_shopee": dados / ".token_shopee.json",
        "ordens": dados / "ordens_de_compra",
    }


# ------------------------------------------ aviso da pasta antes de gravar

class SaidaVigiada:
    """Faz as vezes da saída de erro e anota, na ordem, o que foi escrito."""

    def __init__(self, ordem: list):
        self.ordem = ordem

    def write(self, texto):
        if texto.strip():
            self.ordem.append(("aviso", texto))
        return len(texto)

    def flush(self):
        pass


def test_cli_sem_agente_dados_diz_a_pasta_antes_de_gravar(monkeypatch):
    import cli

    ordem = []
    inicializar = cli.inicializar

    def inicializar_vigiado():  # a primeira gravação do comando
        ordem.append(("gravação", ""))
        inicializar()

    monkeypatch.setattr(cli, "inicializar", inicializar_vigiado)
    monkeypatch.setattr(sys, "stderr", SaidaVigiada(ordem))
    monkeypatch.setattr(sys, "argv", ["cli.py", "cofre", "listar"])
    monkeypatch.delenv("AGENTE_DADOS")

    cli.main()

    assert [tipo for tipo, _ in ordem[:2]] == ["aviso", "gravação"]
    assert f"Pasta de dados: {config.DATA_DIR} (AGENTE_DADOS não definida)" in ordem[0][1]


def test_cli_com_agente_dados_nao_repete_a_pasta(monkeypatch, capsys):
    import cli

    monkeypatch.setattr(sys, "argv", ["cli.py", "cofre", "listar"])
    cli.main()
    assert "Pasta de dados" not in capsys.readouterr().err


def test_worker_sem_agente_dados_diz_a_pasta_antes_do_laco(monkeypatch, capsys):
    import worker

    antes_do_laco = []
    monkeypatch.setattr(worker, "rodar", lambda *a, **k: antes_do_laco.append(capsys.readouterr().err))
    monkeypatch.delenv("AGENTE_DADOS")

    worker.principal()

    assert len(antes_do_laco) == 1
    assert f"Pasta de dados: {config.DATA_DIR} (AGENTE_DADOS não definida)" in antes_do_laco[0]


# ------------------------------------------------------- travas dos testes

def test_trava_dos_testes_recusa_a_pasta_do_repositorio(tmp_path):
    from apoio import pasta_de_teste_recusada

    teste, outra = tmp_path / "dados", tmp_path / "outra"
    casos = [
        (teste, teste / "agente.db", None),
        (RAIZ, RAIZ / "agente.db", "a pasta de dados é a do repositório"),
        (RAIZ, teste / "agente.db", "a pasta de dados é a do repositório"),
        (RAIZ / "tests", teste / "agente.db", "a pasta de dados é a do repositório"),
        # DB_PATH absoluto apontando para o repositório
        (teste, RAIZ / "agente.db", "o banco está na pasta do repositório"),
        (outra, outra / "agente.db", "o projeto não está usando a pasta de teste"),
        (teste, outra / "agente.db", "o projeto não está usando a pasta de teste"),
    ]
    for pasta, banco, motivo in casos:
        recusa = pasta_de_teste_recusada(pasta, banco, teste, RAIZ)
        if motivo is None:
            assert recusa is None
        else:
            assert recusa is not None and recusa.startswith(motivo), (pasta, banco, recusa)


# Importa o conftest de verdade com um config falso no lugar do projeto: nada
# da pasta do repositório é lido nem gravado. "repositorio": o DATA_DIR é a
# raiz do projeto; "teste": segue o AGENTE_DADOS que o conftest define.
CONFTEST_COM_CONFIG_FALSO = textwrap.dedent("""
    import os, shutil, sys, types
    from pathlib import Path

    raiz = Path(sys.argv[1])

    def __getattr__(nome):
        pasta = raiz if sys.argv[2] == "repositorio" else Path(os.environ["AGENTE_DADOS"])
        if nome == "DATA_DIR":
            return pasta
        if nome == "config":
            return types.SimpleNamespace(db_path=str(pasta / "agente.db"))
        raise AttributeError(nome)

    falso = types.ModuleType("config")
    falso.__getattr__ = __getattr__
    sys.modules["config"] = falso
    sys.path.insert(0, str(raiz / "tests"))
    import conftest
    shutil.rmtree(conftest.PASTA_DADOS, ignore_errors=True)
    print("coleta liberada")
""")


@pytest.mark.parametrize("pasta", ["repositorio", "teste"])
def test_conftest_aborta_na_importacao_se_a_pasta_for_a_do_repositorio(pasta):
    ambiente = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}
    feito = subprocess.run([sys.executable, "-c", CONFTEST_COM_CONFIG_FALSO, str(RAIZ), pasta],
                           cwd=RAIZ, env=ambiente, capture_output=True, text=True,
                           encoding="utf-8", timeout=120)
    if pasta == "teste":
        assert feito.returncode == 0 and "coleta liberada" in feito.stdout, feito.stderr
    else:
        assert feito.returncode != 0 and "coleta liberada" not in feito.stdout
        assert ("ABORTADO antes de qualquer teste: a pasta de dados é a do repositório"
                in feito.stderr), feito.stderr


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
