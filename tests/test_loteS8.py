"""Lote S8: conta e senha pelo Data Protection Policy da Amazon (DPP 1.2.2 e
1.4.1), exigido no cadastro de desenvolvedor da SP-API. Bloqueio com até 10
erros, MFA obrigatório com o painel exposto, regra de senha, histórico das
últimas 10, idade mínima de 24 h, validade de 365 dias e lista de operadores
para a revisão de acessos."""
import base64
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest

from apoio import SENHA, USUARIO
from config import config
from core import seguranca
from db import conectar

SEGREDO = base64.b32encode(b"12345678901234567890").decode().rstrip("=")


def _ha(**tempo) -> str:
    return (datetime.now(timezone.utc) - timedelta(**tempo)).isoformat(timespec="seconds")


def envelhecer_senha(**tempo):
    with conectar() as conn:
        conn.execute("UPDATE operadores SET senha_trocada_em = ?", (_ha(**tempo),))


def login(cliente, senha=SENHA, codigo=None):
    corpo = {"usuario": USUARIO, "senha": senha}
    if codigo:
        corpo["codigo"] = codigo
    return cliente.post("/api/login", json=corpo)


def ligar_mfa():
    seguranca.ativar_mfa(USUARIO, SEGREDO,
                         seguranca.codigo_totp(SEGREDO, seguranca.passo_totp() - 1))


# ------------------------------------------------------------ bloqueio

def test_bloqueio_da_conta_cabe_no_limite_do_dpp():
    assert seguranca.TENTATIVAS_ATE_BLOQUEIO <= 10
    assert seguranca.FALHAS_POR_IP < seguranca.TENTATIVAS_ATE_BLOQUEIO


# ------------------------------------------------------------ regra de senha

@pytest.mark.parametrize("senha, motivo", [
    ("Curta-1!", "pelo menos 12"),
    ("sem-maiuscula-123!", "letra maiúscula"),
    ("SEM-MINUSCULA-123!", "letra minúscula"),
    ("Sem-Numero-Nenhum!", "número"),
    ("SemSimbolo12345", "símbolo"),
    ("Sem simbolo 12345 Aa", "símbolo"),
    ("Operador-Forte-123!", "nome do operador"),
    ("Chave-TESTE-forte-9!", "nome do operador"),
])
def test_senha_fora_da_regra_e_recusada(senha, motivo):
    assert motivo in seguranca.problema_na_senha(USUARIO, senha)
    with pytest.raises(seguranca.FalhaLogin, match=motivo):
        seguranca.definir_operador(USUARIO, senha)
    assert not seguranca.existe_operador()


def test_senha_dentro_da_regra():
    assert seguranca.problema_na_senha(USUARIO, SENHA) is None
    assert seguranca.problema_na_senha("op.x", "Ção-Forte-2026#") is None


# ------------------------------------------------------------ histórico e idade

def test_nenhuma_das_ultimas_10_senhas_volta(operador):
    senhas = [SENHA] + [f"Chave-Nova-{i:02d}-ok!" for i in range(1, 11)]
    for nova in senhas[1:]:
        envelhecer_senha(days=2)
        seguranca.definir_operador(USUARIO, nova)

    # A atual (10) e as 9 anteriores (1..9) não voltam; a original, sim.
    for repetida in senhas[1:]:
        envelhecer_senha(days=2)
        with pytest.raises(seguranca.FalhaLogin, match="já foi usada"):
            seguranca.definir_operador(USUARIO, repetida)
    with conectar() as conn:
        assert conn.execute("SELECT COUNT(*) FROM senhas_anteriores").fetchone()[0] == 9
    envelhecer_senha(days=2)
    seguranca.definir_operador(USUARIO, senhas[0])


def test_troca_antes_de_24_horas_so_com_suspeita_de_vazamento(operador):
    with pytest.raises(seguranca.FalhaLogin, match="menos de 24 horas"):
        seguranca.definir_operador(USUARIO, "Outra-Chave-Forte-1!")
    assert seguranca.definir_operador(USUARIO, "Outra-Chave-Forte-1!", troca_imediata=True) \
        == "senha trocada (suspeita de vazamento)"
    with conectar() as conn:
        evento = conn.execute("SELECT nivel, mensagem FROM eventos WHERE origem = 'seguranca'"
                              " ORDER BY id DESC LIMIT 1").fetchone()
    assert tuple(evento) == ("atencao", f"senha trocada (suspeita de vazamento): {USUARIO}")


def test_senha_vencida_nao_entra_e_nao_conta_como_erro(cliente, operador):
    envelhecer_senha(days=seguranca.SENHA_VALIDADE_DIAS + 1)
    r = login(cliente)
    assert r.status_code == 401 and "Senha vencida" in r.json()["detail"]
    with conectar() as conn:
        assert conn.execute("SELECT tentativas FROM operadores").fetchone()[0] == 0

    seguranca.definir_operador(USUARIO, "Chave-Renovada-2026!")
    assert login(cliente, "Chave-Renovada-2026!").status_code == 200


def test_senha_errada_vencida_continua_sendo_so_senha_errada(cliente, operador):
    envelhecer_senha(days=seguranca.SENHA_VALIDADE_DIAS + 1)
    r = login(cliente, "Errada-Errada-123!")
    assert r.json()["detail"] == "Usuário ou senha inválidos."


# ------------------------------------------------------------ MFA obrigatório

def test_mfa_obrigatorio_recusa_operador_sem_mfa(cliente, operador, monkeypatch):
    assert login(cliente).status_code == 200  # local, sem exigência

    monkeypatch.setenv("MFA_OBRIGATORIO", "true")
    r = login(cliente)
    assert r.status_code == 401 and "--mfa-ativar" in r.json()["detail"]
    with conectar() as conn:
        assert conn.execute("SELECT tentativas FROM operadores").fetchone()[0] == 0

    ligar_mfa()
    r = login(cliente, codigo=seguranca.codigo_totp(SEGREDO, seguranca.passo_totp() + 1))
    assert r.status_code == 200


def test_painel_exposto_exige_mfa_mesmo_sem_a_variavel(monkeypatch):
    monkeypatch.setenv("HOST_PAINEL", "0.0.0.0")
    assert seguranca.painel_exposto() and seguranca.mfa_obrigatorio()
    monkeypatch.setenv("HOST_PAINEL", "localhost")
    assert not seguranca.mfa_obrigatorio()


def test_painel_exposto_nao_sobe_com_operador_sem_mfa(operador, monkeypatch, capsys):
    import executar
    import uvicorn

    monkeypatch.setenv("HOST_PAINEL", "0.0.0.0")
    monkeypatch.setenv("PROXY_HTTPS", "true")
    chamado = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: chamado.update(kw))
    executar.main()
    assert chamado == {} and "sem MFA: operador.teste" in capsys.readouterr().out

    ligar_mfa()
    executar.main()
    assert chamado["host"] == "0.0.0.0"


# ------------------------------------------------------------ revisão de acessos

def test_operadores_lista_para_revisao_sem_segredo(cliente, operador, monkeypatch, capsys):
    import cli

    assert login(cliente).status_code == 200
    monkeypatch.setattr(sys, "argv", ["cli.py", "operadores"])
    cli.main()
    saida = capsys.readouterr().out
    assert USUARIO in saida and "último login" in saida and "scrypt" not in saida
    with conectar() as conn:
        assert conn.execute("SELECT ultimo_login_em FROM operadores").fetchone()[0]


def test_cli_troca_com_suspeita_de_vazamento(operador, monkeypatch, capsys):
    import cli

    monkeypatch.setattr("getpass.getpass", lambda *_: "Vazou-Troquei-2026!")
    monkeypatch.setattr(sys, "argv", ["cli.py", "operador", "--usuario", USUARIO,
                                      "--suspeita-de-vazamento"])
    cli.main()
    assert "suspeita de vazamento" in capsys.readouterr().out


# ------------------------------------------------------------ banco antigo

def test_banco_antigo_ganha_validade_a_partir_da_ultima_alteracao(isolamento, monkeypatch):
    banco = isolamento / "antigo.db"
    with sqlite3.connect(banco) as conn:
        conn.execute("CREATE TABLE operadores (id INTEGER PRIMARY KEY, usuario TEXT UNIQUE NOT NULL,"
                     " senha_hash TEXT NOT NULL, criado_em TEXT NOT NULL, atualizado_em TEXT NOT NULL,"
                     " tentativas INTEGER DEFAULT 0, bloqueado_ate TEXT)")
        conn.execute("INSERT INTO operadores (usuario, senha_hash, criado_em, atualizado_em)"
                     " VALUES ('antigo', 'x', '2026-01-01T00:00:00+00:00', '2026-03-01T00:00:00+00:00')")
    monkeypatch.setattr(config, "db_path", str(banco))
    seguranca.inicializar_seguranca()
    with sqlite3.connect(banco) as conn:
        linha = conn.execute("SELECT senha_trocada_em, ultimo_login_em FROM operadores").fetchone()
    assert linha == ("2026-03-01T00:00:00+00:00", None)
