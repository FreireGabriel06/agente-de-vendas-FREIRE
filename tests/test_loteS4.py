"""Lote S4 da auditoria de 06/10/2026: MFA por app autenticador (S-17),
desativar operador (S-19) e HTTPS com cookie Secure e HSTS (S-10, S-25)."""
import base64
import sqlite3
import sys

import pytest

from apoio import SENHA, USUARIO
from config import config
from core import cofre, seguranca
from db import conectar

SEGREDO = base64.b32encode(b"12345678901234567890").decode().rstrip("=")


def codigo_agora(segredo=SEGREDO, desvio=0):
    return seguranca.codigo_totp(segredo, seguranca.passo_totp() + desvio)


@pytest.fixture
def com_mfa(operador):
    seguranca.ativar_mfa(USUARIO, SEGREDO, codigo_agora(desvio=-1))
    return USUARIO


def login(cliente, codigo=None):
    corpo = {"usuario": USUARIO, "senha": SENHA}
    if codigo is not None:
        corpo["codigo"] = codigo
    return cliente.post("/api/login", json=corpo)


# ------------------------------------------------------------ TOTP

def test_totp_bate_com_o_vetor_da_rfc_6238():
    # RFC 6238, apêndice B (SHA1): T=59 s -> 94287082; com 6 dígitos, 287082.
    assert seguranca.codigo_totp(SEGREDO, 59 // 30) == "287082"
    assert seguranca.codigo_totp(SEGREDO, 1111111109 // 30) == "081804"


def test_conferir_aceita_vizinho_e_recusa_reuso_e_lixo():
    agora = 1_800_000_000.0
    passo = seguranca.passo_totp(agora)
    anterior = seguranca.codigo_totp(SEGREDO, passo - 1)
    assert seguranca.conferir_totp(SEGREDO, anterior, None, agora) == passo - 1
    assert seguranca.conferir_totp(SEGREDO, anterior, passo - 1, agora) is None  # já usado
    longe = seguranca.codigo_totp(SEGREDO, passo - 3)
    assert seguranca.conferir_totp(SEGREDO, longe, None, agora) is None
    for lixo in ("", "12345", "abcdef", "1234567"):
        assert seguranca.conferir_totp(SEGREDO, lixo, None, agora) is None


# ------------------------------------------------------------ MFA no login

def test_login_com_mfa_pede_o_codigo_e_entra_com_ele(novo_cliente, com_mfa):
    cliente = novo_cliente()
    r = login(cliente)
    assert r.status_code == 401 and r.json()["mfa"] is True
    assert "agente_sessao" not in r.cookies

    r = login(cliente, codigo_agora(desvio=1))
    assert r.status_code == 200 and r.json()["csrf"]


def test_codigo_nao_serve_duas_vezes(novo_cliente, com_mfa):
    codigo = codigo_agora(desvio=1)
    assert login(novo_cliente(), codigo).status_code == 200
    r = login(novo_cliente(), codigo)
    assert r.status_code == 401 and "já usado" in r.json()["detail"]


def test_dois_logins_ao_mesmo_tempo_com_o_mesmo_codigo_so_um_entra(novo_cliente, com_mfa, monkeypatch):
    # Simula o login paralelo: ele grava o passo do código entre a leitura e a
    # gravação deste login.
    conferir_de_verdade = seguranca.conferir_totp

    def conferir_e_perder_a_corrida(*args, **kwargs):
        passo = conferir_de_verdade(*args, **kwargs)
        with conectar() as conn:
            conn.execute("UPDATE operadores SET totp_ultimo_passo = ?", (passo,))
        return passo
    monkeypatch.setattr(seguranca, "conferir_totp", conferir_e_perder_a_corrida)

    r = login(novo_cliente(), codigo_agora(desvio=1))
    assert r.status_code == 401 and "já usado" in r.json()["detail"]
    with conectar() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessoes").fetchone()[0] == 0


def test_codigo_errado_conta_para_o_bloqueio(novo_cliente, com_mfa):
    por_endereco = seguranca.FALHAS_POR_IP - 1
    for i in range(seguranca.TENTATIVAS_ATE_BLOQUEIO):
        cliente = novo_cliente(client=(f"10.1.{i // por_endereco}.1", 50000))
        assert login(cliente, "000000").status_code == 401
    r = login(novo_cliente(client=("10.1.250.1", 50000)), codigo_agora(desvio=1))
    assert r.status_code == 401 and "bloqueado" in r.json()["detail"]


def test_ativar_com_codigo_errado_nao_liga(operador):
    with pytest.raises(seguranca.FalhaLogin, match="não confere"):
        seguranca.ativar_mfa(USUARIO, SEGREDO, "000000")
    with conectar() as conn:
        assert conn.execute("SELECT totp_ativo FROM operadores").fetchone()[0] == 0


def test_ativar_mfa_derruba_sessoes_e_guarda_o_segredo_so_no_cofre(sessao, isolamento):
    cliente, _ = sessao
    assert cliente.get("/api/pendencias").status_code == 200
    seguranca.ativar_mfa(USUARIO, SEGREDO, codigo_agora(desvio=-1))
    assert cliente.get("/api/pendencias").status_code == 401

    assert cofre.ler("operador", f"totp:{USUARIO}") == SEGREDO
    with sqlite3.connect(config.db_path) as conn:
        assert SEGREDO not in "\n".join(conn.iterdump())


def test_desligar_mfa_volta_ao_login_so_com_senha(novo_cliente, com_mfa):
    seguranca.desativar_mfa(USUARIO)
    assert login(novo_cliente()).status_code == 200
    assert cofre.ler("operador", f"totp:{USUARIO}") is None


# ------------------------------------------------------------ desativar operador

def test_desativar_derruba_a_sessao_na_hora_e_recusa_login(sessao, novo_cliente):
    cliente, _ = sessao
    seguranca.definir_ativo(USUARIO, False)

    assert cliente.get("/api/pendencias").status_code == 401
    r = login(novo_cliente())
    assert r.status_code == 401 and r.json()["detail"] == "Usuário ou senha inválidos."

    seguranca.definir_ativo(USUARIO, True)
    assert login(novo_cliente()).status_code == 200


def test_sessao_de_operador_inativo_nao_vale_mesmo_sem_apagar(sessao):
    cliente, _ = sessao
    with conectar() as conn:
        conn.execute("UPDATE operadores SET ativo = 0")
    assert cliente.get("/api/pendencias").status_code == 401


def test_banco_antigo_ganha_as_colunas_novas(isolamento, monkeypatch):
    banco = isolamento / "antigo.db"
    with sqlite3.connect(banco) as conn:
        conn.execute("CREATE TABLE operadores (id INTEGER PRIMARY KEY, usuario TEXT UNIQUE NOT NULL,"
                     " senha_hash TEXT NOT NULL, criado_em TEXT NOT NULL, atualizado_em TEXT NOT NULL,"
                     " tentativas INTEGER DEFAULT 0, bloqueado_ate TEXT)")
        conn.execute("INSERT INTO operadores (usuario, senha_hash, criado_em, atualizado_em)"
                     " VALUES ('antigo', 'x', 'a', 'a')")
    monkeypatch.setattr(config, "db_path", str(banco))
    seguranca.inicializar_seguranca()
    seguranca.inicializar_seguranca()
    with sqlite3.connect(banco) as conn:
        linha = conn.execute("SELECT ativo, totp_ativo, totp_ultimo_passo FROM operadores").fetchone()
    assert linha == (1, 0, None)


# ------------------------------------------------------------ CLI

def test_cli_liga_mfa_desativa_e_reativa(operador, monkeypatch, capsys):
    import cli

    monkeypatch.setattr(seguranca, "novo_segredo_totp", lambda: SEGREDO)
    monkeypatch.setattr("builtins.input", lambda *_: codigo_agora(desvio=-1))
    for argv in (["--mfa-ativar"], ["--desativar"], ["--reativar"], ["--mfa-desativar"]):
        monkeypatch.setattr(sys, "argv", ["cli.py", "operador", "--usuario", USUARIO, *argv])
        cli.main()
    saida = capsys.readouterr().out
    assert "otpauth://totp/" in saida and "MFA ligado" in saida
    assert "desativado; sessões encerradas" in saida and "reativado" in saida
    assert "MFA desligado" in saida


# ------------------------------------------------------------ HTTPS

def test_opcoes_https(monkeypatch, tmp_path):
    import executar

    for v in ("TLS_CERT", "TLS_CHAVE", "PROXY_HTTPS", "PROXY_IPS"):
        monkeypatch.delenv(v, raising=False)
    assert executar.opcoes_https() == ({}, None)

    monkeypatch.setenv("TLS_CERT", str(tmp_path / "cert.pem"))
    assert "juntos" in executar.opcoes_https()[1]
    monkeypatch.setenv("TLS_CHAVE", str(tmp_path / "chave.pem"))
    assert "não encontrado" in executar.opcoes_https()[1]
    (tmp_path / "cert.pem").write_text("x")
    (tmp_path / "chave.pem").write_text("x")
    assert executar.opcoes_https()[0] == {"ssl_certfile": str(tmp_path / "cert.pem"),
                                          "ssl_keyfile": str(tmp_path / "chave.pem")}

    monkeypatch.delenv("TLS_CERT")
    monkeypatch.delenv("TLS_CHAVE")
    monkeypatch.setenv("PROXY_HTTPS", "true")
    assert executar.opcoes_https()[0] == {"proxy_headers": True, "forwarded_allow_ips": "127.0.0.1"}


def test_painel_fora_do_loopback_nao_sobe_sem_https(operador, monkeypatch, capsys):
    import executar
    import uvicorn

    monkeypatch.setenv("HOST_PAINEL", "0.0.0.0")
    for v in ("TLS_CERT", "TLS_CHAVE", "PROXY_HTTPS"):
        monkeypatch.delenv(v, raising=False)

    def nao_sobe(*_, **__):
        raise AssertionError("não devia subir sem HTTPS")
    monkeypatch.setattr(uvicorn, "run", nao_sobe)

    executar.main()
    assert "sem HTTPS" in capsys.readouterr().out


def test_painel_fora_do_loopback_sobe_atras_de_proxy_https(com_mfa, monkeypatch):
    import executar
    import uvicorn

    monkeypatch.setenv("HOST_PAINEL", "0.0.0.0")
    monkeypatch.setenv("PROXY_HTTPS", "true")
    chamado = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: chamado.update(kw))
    executar.main()
    assert chamado["proxy_headers"] is True and chamado["host"] == "0.0.0.0"


def test_cookie_secure_e_hsts_so_sob_https(novo_cliente, operador):
    https = novo_cliente(base_url="https://testserver")
    r = https.post("/api/login", json={"usuario": USUARIO, "senha": SENHA})
    assert "secure" in r.headers["set-cookie"].lower()
    assert r.headers["strict-transport-security"] == "max-age=31536000"

    http = novo_cliente()
    r = http.post("/api/login", json={"usuario": USUARIO, "senha": SENHA})
    assert "secure" not in r.headers["set-cookie"].lower()
    assert "strict-transport-security" not in r.headers
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"


def test_recusa_e_redirecionamento_tambem_levam_os_cabecalhos(cliente):
    r = cliente.get("/api/pendencias")
    assert r.status_code == 401 and r.headers["x-frame-options"] == "DENY"
    r = cliente.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["x-content-type-options"] == "nosniff"


def test_pagina_de_login_tem_campo_do_codigo(cliente):
    texto = cliente.get("/login").text
    assert 'id="codigo"' in texto and 'autocomplete="one-time-code"' in texto
    # label{display:grid} venceria o atributo hidden sem esta regra, e o campo
    # do código apareceria antes de a senha conferir.
    assert 'id="campo-codigo" hidden' in texto and "[hidden]{display:none}" in texto
