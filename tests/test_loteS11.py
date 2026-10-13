"""Lote S11: papéis de acesso pelo Data Protection Policy da Amazon (1.3,
menor privilégio, negar por padrão). "admin" faz tudo; "leitura" só
consulta. O primeiro operador nasce admin; os seguintes, só de leitura."""
import sqlite3
import sys

import pytest

from apoio import SENHA, USUARIO, cabecalho
from config import config
from core import seguranca
from db import conectar

LEITOR = "leitor.beta"
SENHA_LEITOR = "Cavalo-Bateria-31!"


@pytest.fixture
def leitor(operador):
    assert seguranca.definir_operador(LEITOR, SENHA_LEITOR) == "operador criado (leitura)"
    return LEITOR


def entrar(novo_cliente, usuario, senha):
    cliente = novo_cliente()
    r = cliente.post("/api/login", json={"usuario": usuario, "senha": senha})
    assert r.status_code == 200, r.text
    return cliente, r.json()["csrf"]


def papel_de(usuario):
    with conectar() as conn:
        return conn.execute("SELECT papel FROM operadores WHERE usuario = ?", (usuario,)).fetchone()[0]


def test_primeiro_nasce_admin_e_os_outros_so_de_leitura(leitor):
    assert papel_de(USUARIO) == "admin" and papel_de(LEITOR) == "leitura"


@pytest.mark.parametrize("caminho", ["/", "/api/sessao", "/api/pendencias", "/api/pedidos",
                                     "/api/alertas", "/api/produtos", "/api/fornecedores",
                                     "/api/amazon/pedidos", "/api/amazon/anuncios",
                                     "/api/amazon/sincronizacao"])
def test_leitura_consulta(novo_cliente, leitor, caminho):
    cliente, _ = entrar(novo_cliente, LEITOR, SENHA_LEITOR)
    assert cliente.get(caminho).status_code == 200


@pytest.mark.parametrize("metodo, caminho", [
    ("GET", "/api/pedido/1/comprador"),
    ("GET", "/api/eventos"),
    ("GET", "/api/lgpd"),
    ("GET", "/api/configuracao"),
    ("GET", "/api/configuracao/url-autorizacao/amazon"),
    ("GET", "/oauth/amazon/login?amazon_callback_uri=x&amazon_state=y&selling_partner_id=z"),
    ("POST", "/api/aprovar/1"),
    ("POST", "/api/recusar/1"),
    ("POST", "/api/ciclo"),
    ("POST", "/api/nichos"),
    ("POST", "/api/lgpd/expurgar"),
    ("POST", "/api/configuracao/salvar"),
    ("POST", "/api/configuracao/testar/amazon"),
    ("POST", "/api/produtos"),
    ("PATCH", "/api/produtos/1"),
    ("POST", "/api/pedidos/1/reanalisar"),
])
def test_leitura_nao_age_nem_ve_o_que_e_sensivel(novo_cliente, leitor, metodo, caminho):
    cliente, csrf = entrar(novo_cliente, LEITOR, SENHA_LEITOR)
    r = cliente.request(metodo, caminho, headers=cabecalho(csrf), json={})
    assert r.status_code == 403 and r.json()["detail"] == "Seu acesso é só de leitura."


def test_leitura_usa_a_calculadora_e_sai(novo_cliente, leitor):
    cliente, csrf = entrar(novo_cliente, LEITOR, SENHA_LEITOR)
    assert cliente.post("/api/preco", json={"custo": 30}, headers=cabecalho(csrf)).status_code == 200
    assert cliente.post("/api/logout", headers=cabecalho(csrf)).status_code == 200


def test_admin_continua_podendo_tudo(sessao):
    cliente, csrf = sessao
    assert cliente.get("/api/eventos").status_code == 200
    assert cliente.get("/api/configuracao").status_code == 200


def test_promover_vale_na_proxima_chamada(novo_cliente, leitor):
    cliente, _ = entrar(novo_cliente, LEITOR, SENHA_LEITOR)
    assert cliente.get("/api/eventos").status_code == 403
    seguranca.definir_papel(LEITOR, "admin")
    assert cliente.get("/api/eventos").status_code == 200
    assert cliente.get("/api/sessao").json()["papel"] == "admin"


def test_ultimo_admin_nao_perde_o_papel(leitor):
    with pytest.raises(seguranca.FalhaLogin, match="último administrador"):
        seguranca.definir_papel(USUARIO, "leitura")
    seguranca.definir_papel(LEITOR, "admin")
    seguranca.definir_papel(USUARIO, "leitura")
    assert papel_de(USUARIO) == "leitura"
    with pytest.raises(seguranca.FalhaLogin, match="Papel desconhecido"):
        seguranca.definir_papel(USUARIO, "dono")


def test_cli_troca_e_lista_o_papel(leitor, monkeypatch, capsys):
    import cli
    monkeypatch.setattr(sys, "argv", ["cli.py", "operador", "--usuario", LEITOR, "--papel", "admin"])
    cli.main()
    monkeypatch.setattr(sys, "argv", ["cli.py", "operadores"])
    cli.main()
    saida = capsys.readouterr().out
    assert "papel admin" in saida and "papel" in saida.splitlines()[1]
    assert papel_de(LEITOR) == "admin"
    with conectar() as conn:
        evento = conn.execute("SELECT mensagem FROM eventos WHERE origem = 'seguranca'"
                              " ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert evento == f"Papel de {LEITOR}: leitura -> admin"


def test_banco_antigo_mantem_quem_existia_como_admin(isolamento, monkeypatch):
    banco = isolamento / "antigo.db"
    with sqlite3.connect(banco) as conn:
        conn.execute("CREATE TABLE operadores (id INTEGER PRIMARY KEY, usuario TEXT UNIQUE NOT NULL,"
                     " senha_hash TEXT NOT NULL, criado_em TEXT NOT NULL, atualizado_em TEXT NOT NULL,"
                     " tentativas INTEGER DEFAULT 0, bloqueado_ate TEXT)")
        conn.execute("INSERT INTO operadores (usuario, senha_hash, criado_em, atualizado_em)"
                     " VALUES ('a', 'x', 'a', 'a'), ('b', 'y', 'a', 'a')")
    monkeypatch.setattr(config, "db_path", str(banco))
    seguranca.inicializar_seguranca()
    with sqlite3.connect(banco) as conn:
        assert conn.execute("SELECT papel FROM operadores ORDER BY id").fetchall() == [("admin",), ("admin",)]
