"""
Base comum dos testes.

O config.py lê o ambiente na importação e decide ali onde ficam .env, banco,
chave LGPD e tokens. Por isso este arquivo prepara o ambiente ANTES de qualquer
módulo do projeto ser importado:

  - AGENTE_DADOS aponta para uma pasta temporária nova: o .env, o banco, a
    chave e os tokens da pasta do projeto não são lidos nem escritos;
  - CHAVE_LGPD recebe uma chave Fernet gerada agora;
  - as credenciais de marketplace são falsas e o worker fica desligado;
  - proxy desligado (NO_PROXY=*), para a trava de rede valer.

Cada teste ainda ganha a própria pasta (tmp_path), com banco novo, e qualquer
conexão de rede fora do loopback reprova o teste.
"""
import dataclasses
import ipaddress
import os
import shutil
import socket
import tempfile
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from apoio import COMPRADOR, SENHA, USUARIO, MercadoLivreFalso

PASTA_DADOS = Path(tempfile.mkdtemp(prefix="agente-testes-")).resolve()

# Variáveis do projeto que o ambiente de quem roda os testes poderia trazer.
for _nome in (
    "DB_PATH", "ML_REFRESH_TOKEN", "ML_SELLER_ID", "ML_SITE_ID", "ML_REDIRECT_URI",
    "AMZ_REFRESH_TOKEN", "AMZ_MARKETPLACE_ID", "AMZ_ENDPOINT",
    "SHOPEE_REFRESH_TOKEN", "SHOPEE_SANDBOX", "ANTHROPIC_API_KEY",
    "MARGEM_MINIMA_PCT", "TETO_COMPRA_AUTOMATICA", "ALIQUOTA_IMPOSTO_PCT",
    "PRAZO_FORNECEDOR_DIAS", "RETENCAO_PII_DIAS", "SESSAO_OCIOSA_MIN",
    "SESSAO_MAX_HORAS", "PORTA_PAINEL", "HOST_PAINEL", "INTERVALO_WORKER",
    "MODO_SIMULACAO",
):
    os.environ.pop(_nome, None)

# Com proxy, o requests só conecta no proxy; se ele estiver no loopback, a trava
# de rede abaixo deixa passar e o proxy sai para o marketplace. NO_PROXY=*
# desliga também o proxy do sistema, que no Windows vem do registro.
for _nome in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
    os.environ.pop(_nome, None)
    os.environ.pop(_nome.lower(), None)
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "*"

os.environ.update({
    "AGENTE_DADOS": str(PASTA_DADOS),
    "CHAVE_LGPD": Fernet.generate_key().decode(),
    "WORKER_ATIVO": "false",
    "ABRIR_NAVEGADOR": "false",
    "ML_CLIENT_ID": "ml-id-falso",
    "ML_CLIENT_SECRET": "ml-segredo-falso",
    "SHOPEE_PARTNER_ID": "1000001",
    "SHOPEE_PARTNER_KEY": "shopee-chave-falsa",
    "SHOPEE_SHOP_ID": "2000002",
    "AMZ_LWA_CLIENT_ID": "amz-id-falso",
    "AMZ_LWA_CLIENT_SECRET": "amz-segredo-falso",
})


# ------------------------------------------------------------ trava da sessão

@pytest.fixture(scope="session", autouse=True)
def pasta_de_dados_temporaria():
    """Aborta tudo se o projeto não estiver usando a pasta temporária."""
    import config

    if config.DATA_DIR != PASTA_DADOS or Path(config.config.db_path).parent != PASTA_DADOS:
        shutil.rmtree(PASTA_DADOS, ignore_errors=True)
        pytest.exit(f"ABORTADO: o projeto não está usando a pasta de teste ({config.DATA_DIR}).",
                    returncode=3)
    yield PASTA_DADOS
    shutil.rmtree(PASTA_DADOS, ignore_errors=True)


# ------------------------------------------------------------------- rede

def _loopback(host) -> bool:
    if host in (None, "", b"", "localhost", b"localhost"):
        return True
    if isinstance(host, bytes):
        host = host.decode()
    try:
        return ipaddress.ip_address(str(host).split("%")[0]).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def sem_rede(monkeypatch):
    """Conexão fora do loopback levanta erro e reprova o teste, mesmo quando o
    código engole a exceção. Resolução de nome também conta: o DNS já sai
    para a rede."""
    tentativas = []
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex

    def barrar(destino):
        tentativas.append(destino)
        raise RuntimeError(f"conexão externa bloqueada nos testes: {destino!r}")

    def conectar(sock, endereco):
        if not isinstance(endereco, (str, bytes)) and not _loopback(endereco[0]):
            barrar(endereco)
        return connect(sock, endereco)

    def conectar_ex(sock, endereco):
        if not isinstance(endereco, (str, bytes)) and not _loopback(endereco[0]):
            barrar(endereco)
        return connect_ex(sock, endereco)

    def vigiar_resolucao(original):
        def resolver(host, *args, **kwargs):
            if not _loopback(host):
                barrar(host)
            return original(host, *args, **kwargs)
        return resolver

    monkeypatch.setattr(socket.socket, "connect", conectar)
    monkeypatch.setattr(socket.socket, "connect_ex", conectar_ex)
    for nome in ("getaddrinfo", "gethostbyname", "gethostbyname_ex"):
        monkeypatch.setattr(socket, nome, vigiar_resolucao(getattr(socket, nome)))
    yield tentativas
    if tentativas:
        pytest.fail(f"o teste tentou sair para a rede: {tentativas!r}")


# ------------------------------------------------------------ isolamento

@pytest.fixture(autouse=True)
def isolamento(tmp_path, monkeypatch):
    """Banco novo e todos os arquivos de execução na pasta deste teste."""
    import config
    import worker
    from conectores import mercadolivre, shopee
    from core import privacidade, seguranca
    from painel import configurar
    from painel.app import preparar

    cfg = config.config
    monkeypatch.setattr(cfg, "db_path", str(tmp_path / "agente.db"))
    # Cópias: a troca de código do OAuth altera config.ml em memória.
    for nome in ("ml", "amazon", "negocio"):
        monkeypatch.setattr(cfg, nome, dataclasses.replace(getattr(cfg, nome)))
    monkeypatch.setattr(configurar, "ARQ_ENV", tmp_path / ".env")
    monkeypatch.setattr(privacidade, "ARQ_CHAVE", tmp_path / ".chave_lgpd")
    monkeypatch.setattr(mercadolivre, "ARQ_TOKEN", tmp_path / ".token_ml.json")
    monkeypatch.setattr(shopee, "ARQ_TOKEN", tmp_path / ".token_shopee.json")
    monkeypatch.setattr(worker, "PASTA_ORDENS", tmp_path / "ordens_de_compra")
    monkeypatch.setattr(seguranca, "_states", {})

    ambiente = dict(os.environ)
    preparar()
    yield tmp_path

    # gravar_env escreve direto em os.environ; nada disso passa para o próximo teste.
    for chave in set(os.environ) - set(ambiente):
        del os.environ[chave]
    for chave, valor in ambiente.items():
        if os.environ.get(chave) != valor:
            os.environ[chave] = valor


# --------------------------------------------------------------- painel

@pytest.fixture
def operador(monkeypatch):
    """Cria o operador de teste. O custo do scrypt cai só para o teste andar
    rápido: o hash guarda os próprios parâmetros, então o login segue o mesmo
    caminho. O parâmetro real é conferido em test_login_painel.py."""
    from core import seguranca

    monkeypatch.setattr(seguranca, "SCRYPT_N", 2 ** 14)
    seguranca.definir_operador(USUARIO, SENHA)
    return USUARIO


@pytest.fixture
def novo_cliente():
    """Fábrica de clientes HTTP do painel; cada um tem os próprios cookies."""
    from fastapi.testclient import TestClient
    from painel.app import app

    abertos = []

    def criar(**kwargs):
        cliente = TestClient(app, **kwargs)
        abertos.append(cliente)
        return cliente

    yield criar
    for cliente in abertos:
        cliente.close()


@pytest.fixture
def cliente(novo_cliente):
    """Cliente sem sessão."""
    return novo_cliente()


@pytest.fixture
def abrir_sessao(novo_cliente, operador):
    """Entra no painel e devolve (cliente, token CSRF)."""
    def entrar(usuario=USUARIO, senha=SENHA):
        cliente = novo_cliente()
        r = cliente.post("/api/login", json={"usuario": usuario, "senha": senha})
        assert r.status_code == 200, r.text
        return cliente, r.json()["csrf"]

    return entrar


@pytest.fixture
def sessao(abrir_sessao):
    return abrir_sessao()


# ---------------------------------------------------------------- dados

@pytest.fixture
def pedido_cifrado():
    """Um pedido que entra pelo caminho real do worker, com PII cifrado."""
    import worker
    from db import conectar

    bruto = {
        "id": 970001,
        "order_items": [{"item": {"title": "Sintético", "seller_sku": "ORG-001"}, "quantity": 1}],
        "total_amount": 60.0,
        "buyer": dict(COMPRADOR),
        "shipping": {"id": 777},
    }
    assert worker.ingerir_mercadolivre(MercadoLivreFalso([bruto])) == 1
    with conectar() as conn:
        return conn.execute("SELECT id FROM pedidos WHERE id_externo = '970001'").fetchone()["id"]


@pytest.fixture
def fila_de_compras():
    """Dois pedidos com margem boa, analisados e enfileirados pelo worker.
    Devolve os ids das pendências, na ordem da fila."""
    import worker
    from core import aprovacao
    from core.estados import Estado
    from db import agora, conectar

    with conectar() as c:
        c.execute("INSERT INTO fornecedores (id, nome, canal, contato, prazo_dias)"
                  " VALUES (1, 'Fornecedor Teste', 'email', 'pedidos@fornecedor.example', 4)")
        c.execute("INSERT INTO produtos (id, sku, titulo, custo_fornecedor, peso_kg,"
                  " fornecedor_id, criado_em) VALUES (1, 'ORG-001', 'Organizador', 18.50, 0.4, 1, ?)",
                  (agora(),))
        for id_externo in ("3000000001", "3000000002"):
            c.execute("INSERT INTO pedidos (marketplace, id_externo, produto_id, quantidade,"
                      " valor_bruto, estado, criado_em, atualizado_em)"
                      " VALUES ('mercadolivre', ?, 1, 1, 54.90, ?, ?, ?)",
                      (id_externo, Estado.NOVO.value, agora(), agora()))
    assert worker.analisar_novos() == 2
    assert worker.montar_ordens_de_compra() == 2
    return [a.id for a in aprovacao.pendentes()]
