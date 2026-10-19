"""Lote S13: código por requisição (auditoria, S-21) e falha inesperada sem a
mensagem da exceção na tela, no evento ou no log do servidor."""
import re

import pytest

from db import conectar

SEGREDO = "valor-decifrado-do-cofre-s13"


@pytest.fixture
def rota_que_quebra():
    from painel.app import app

    def quebrar():
        raise RuntimeError(SEGREDO)

    app.add_api_route("/api/teste-s13-quebra", quebrar, methods=["GET"])
    rota = app.router.routes[-1]
    yield "/api/teste-s13-quebra"
    app.router.routes.remove(rota)


def test_toda_resposta_leva_um_codigo_proprio(cliente):
    a = cliente.get("/login")
    b = cliente.get("/login")
    codigos = [a.headers["X-Request-ID"], b.headers["X-Request-ID"]]
    assert all(re.fullmatch(r"[0-9a-f]{12}", c) for c in codigos)
    assert codigos[0] != codigos[1]


def test_recusa_de_sessao_tambem_leva_codigo(cliente):
    r = cliente.get("/api/pendencias")
    assert r.status_code == 401 and "X-Request-ID" in r.headers


def test_falha_inesperada_mostra_so_tipo_e_codigo(sessao, rota_que_quebra, capfd):
    cliente, _ = sessao
    r = cliente.get(rota_que_quebra)
    assert r.status_code == 500
    codigo = r.headers["X-Request-ID"]
    detalhe = r.json()["detail"]
    assert "RuntimeError" in detalhe and codigo in detalhe
    assert SEGREDO not in r.text
    # Os cabeçalhos de segurança valem também para o 500.
    assert r.headers["Cache-Control"] == "no-store" and "Content-Security-Policy" in r.headers
    # Nada da exceção vazou para a saída do processo (o log do container).
    saida = capfd.readouterr()
    assert SEGREDO not in saida.out + saida.err

    with conectar() as conn:
        evento = conn.execute("SELECT mensagem, detalhe_json FROM eventos WHERE origem = 'painel'"
                              " AND nivel = 'erro' ORDER BY id DESC LIMIT 1").fetchone()
    assert codigo in evento["mensagem"] and "/api/teste-s13-quebra" in evento["mensagem"]
    assert SEGREDO not in evento["mensagem"] + evento["detalhe_json"]
    assert "test_loteS13.py" in evento["detalhe_json"]  # a pilha fica, sem a mensagem


def test_falha_sem_banco_ainda_responde_com_codigo(sessao, rota_que_quebra, monkeypatch):
    from painel import protecao

    def sem_banco(*a, **k):
        raise OSError("banco fora")

    cliente, _ = sessao
    monkeypatch.setattr(protecao, "registrar_evento", sem_banco)
    r = cliente.get(rota_que_quebra)
    assert r.status_code == 500 and r.headers["X-Request-ID"] in r.json()["detail"]
