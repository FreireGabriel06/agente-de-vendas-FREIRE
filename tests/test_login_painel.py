"""Lote 1: login no painel, sessão, CSRF, bloqueio e operador na trilha."""
import base64
import sys
from datetime import datetime, timedelta, timezone

import pytest

from apoio import SENHA, USUARIO, cabecalho
from core import seguranca
from db import conectar


def _ha(**tempo) -> str:
    """Um momento no passado, no formato que o banco guarda."""
    return (datetime.now(timezone.utc) - timedelta(**tempo)).isoformat(timespec="seconds")


def _sessao_no_banco(cliente, **campos):
    """Troca colunas da sessão do cliente direto no banco e devolve a linha."""
    sid = cliente.cookies.get(seguranca.COOKIE_SESSAO)
    with conectar() as conn:
        for coluna, valor in campos.items():
            conn.execute(f"UPDATE sessoes SET {coluna} = ? WHERE id = ?", (valor, sid))
        return conn.execute("SELECT * FROM sessoes WHERE id = ?", (sid,)).fetchone()


# ------------------------------------------------------------------- senha

def test_hash_da_senha_usa_scrypt_com_o_parametro_real():
    guardado = seguranca.gerar_hash(SENHA)
    algoritmo, n, r, p, sal, bruto = guardado.split("$")

    assert (algoritmo, n, r, p) == ("scrypt", str(2 ** 17), "8", "1")
    assert len(base64.b64decode(sal)) == 16
    assert len(base64.b64decode(bruto)) == 32
    assert SENHA not in guardado
    assert seguranca.conferir_hash(SENHA, guardado) is True
    assert seguranca.conferir_hash("outra-senha-comprida", guardado) is False
    assert seguranca.conferir_hash(SENHA, "md5$1$2$3$c2Fs$aGFzaA==") is False
    assert seguranca.conferir_hash(SENHA, "lixo") is False


def test_senha_curta_e_recusada():
    with pytest.raises(seguranca.FalhaLogin):
        seguranca.gerar_hash("curta")


def test_banco_guarda_so_o_hash(operador):
    with conectar() as conn:
        guardado = conn.execute("SELECT senha_hash FROM operadores WHERE usuario = ?",
                                (USUARIO,)).fetchone()["senha_hash"]
    assert guardado.startswith("scrypt$")
    assert SENHA not in guardado


def test_cli_exige_o_nome_do_operador(monkeypatch, capsys):
    import cli

    monkeypatch.setattr(sys, "argv", ["cli.py", "operador"])
    with pytest.raises(SystemExit) as saida:
        cli.main()
    assert saida.value.code == 2
    assert "--usuario" in capsys.readouterr().err


# ------------------------------------------------------------------- login

def test_senha_errada_e_usuario_desconhecido_recebem_401(cliente, operador):
    errada = cliente.post("/api/login", json={"usuario": USUARIO, "senha": "errada-errada"})
    desconhecido = cliente.post("/api/login", json={"usuario": "ninguem", "senha": SENHA})

    assert errada.status_code == 401
    assert desconhecido.status_code == 401
    # Mesma resposta: a tela não revela quais operadores existem.
    assert errada.json() == desconhecido.json()
    assert "set-cookie" not in errada.headers


def test_login_aceito_cria_sessao_com_cookie_protegido(cliente, operador):
    r = cliente.post("/api/login", json={"usuario": USUARIO, "senha": SENHA})

    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(seguranca.COOKIE_SESSAO + "=")
    assert "httponly" in cookie.lower()
    assert "samesite=lax" in cookie.lower()
    csrf = r.json()["csrf"]
    assert len(csrf) >= 32

    sessao = cliente.get("/api/sessao")
    assert sessao.status_code == 200
    assert sessao.json() == {"usuario": USUARIO, "csrf": csrf}
    assert cliente.get("/api/pendencias").status_code == 200
    assert cliente.get("/").status_code == 200


def test_bloqueio_depois_de_cinco_tentativas(cliente, operador):
    for _ in range(5):
        r = cliente.post("/api/login", json={"usuario": USUARIO, "senha": "errada-errada"})
        assert r.status_code == 401

    r = cliente.post("/api/login", json={"usuario": USUARIO, "senha": SENHA})
    assert r.status_code == 401
    assert "bloqueado" in r.json()["detail"].lower()

    with conectar() as conn:
        assert conn.execute("SELECT bloqueado_ate FROM operadores").fetchone()[0]
        evento = conn.execute("SELECT mensagem FROM eventos WHERE nivel = 'atencao'"
                              " AND origem = 'seguranca'").fetchone()
    assert "bloqueado" in evento["mensagem"]

    # O bloqueio é temporário: vencido o prazo, a senha certa volta a valer.
    with conectar() as conn:
        conn.execute("UPDATE operadores SET bloqueado_ate = '2000-01-01T00:00:00+00:00'")
    assert cliente.post("/api/login", json={"usuario": USUARIO, "senha": SENHA}).status_code == 200


def test_quatro_erros_ainda_nao_bloqueiam(cliente, operador):
    for _ in range(4):
        cliente.post("/api/login", json={"usuario": USUARIO, "senha": "errada-errada"})
    assert cliente.post("/api/login", json={"usuario": USUARIO, "senha": SENHA}).status_code == 200


def test_login_certo_zera_as_tentativas(cliente, operador):
    errada = {"usuario": USUARIO, "senha": "errada-errada"}
    certa = {"usuario": USUARIO, "senha": SENHA}
    for _ in range(4):
        assert cliente.post("/api/login", json=errada).status_code == 401
    assert cliente.post("/api/login", json=certa).status_code == 200
    # Sem zerar, este erro seria o quinto e bloquearia o operador.
    assert cliente.post("/api/login", json=errada).status_code == 401
    assert cliente.post("/api/login", json=certa).status_code == 200


# ---------------------------------------------------------------- sem sessão

@pytest.mark.parametrize("metodo, caminho", [
    ("GET", "/api/pendencias"),
    ("GET", "/api/pedidos"),
    ("GET", "/api/sessao"),
    ("GET", "/api/lgpd"),
    ("GET", "/api/configuracao"),
    ("POST", "/api/aprovar/1"),
    ("POST", "/api/configuracao/salvar"),
    ("POST", "/api/logout"),
])
def test_api_sem_sessao_responde_401(cliente, metodo, caminho):
    r = cliente.request(metodo, caminho)
    assert r.status_code == 401
    assert r.json() == {"detail": "Sessão necessária."}


def test_cookie_inventado_nao_abre_sessao(novo_cliente, operador):
    cliente = novo_cliente(cookies={seguranca.COOKIE_SESSAO: "inventado-" + "x" * 40})
    assert cliente.get("/api/pendencias").status_code == 401


@pytest.mark.parametrize("caminho", ["/", "/oauth/ml/retorno?code=x&state=y",
                                     "/oauth/shopee/retorno?code=x"])
def test_pagina_sem_sessao_vai_para_o_login(cliente, caminho):
    r = cliente.get(caminho, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_tela_de_login_abre_sem_sessao(cliente):
    r = cliente.get("/login")
    assert r.status_code == 200
    assert "<form" in r.text


# --------------------------------------------------------------------- CSRF

def test_chamada_que_muda_estado_exige_csrf(sessao):
    cliente, csrf = sessao
    corpo = {"custo": 30}

    assert cliente.post("/api/preco", json=corpo).status_code == 403
    assert cliente.post("/api/preco", json=corpo, headers=cabecalho("token-errado")).status_code == 403
    certo = cliente.post("/api/preco", json=corpo, headers=cabecalho(csrf))
    assert certo.status_code == 200
    assert certo.json()["preco_venda"] > 30


def test_csrf_de_outra_sessao_nao_vale(abrir_sessao):
    cliente_a, _ = abrir_sessao()
    _, csrf_b = abrir_sessao()
    assert cliente_a.post("/api/preco", json={"custo": 30}, headers=cabecalho(csrf_b)).status_code == 403


# ------------------------------------------------------------------- logout

def test_logout_encerra_a_sessao_no_servidor(sessao, novo_cliente):
    cliente, csrf = sessao
    sid = cliente.cookies.get(seguranca.COOKIE_SESSAO)

    assert cliente.post("/api/logout").status_code == 403  # sem CSRF não sai
    assert cliente.get("/api/pendencias").status_code == 200

    assert cliente.post("/api/logout", headers=cabecalho(csrf)).status_code == 200
    assert cliente.get("/api/pendencias").status_code == 401
    # O cookie antigo, reenviado, também não vale mais.
    reenviado = novo_cliente(cookies={seguranca.COOKIE_SESSAO: sid})
    assert reenviado.get("/api/pendencias").status_code == 401


# ------------------------------------------------------- validade da sessão
# Os limites vão escritos por extenso (29/31 min, 8 h) para que uma troca do
# valor padrão em core/seguranca.py também reprove o teste.

def test_sessao_ociosa_por_30_minutos_expira(sessao):
    cliente, _ = sessao
    _sessao_no_banco(cliente, ultimo_uso=_ha(minutes=29))
    assert cliente.get("/api/sessao").status_code == 200

    _sessao_no_banco(cliente, ultimo_uso=_ha(minutes=31))
    assert cliente.get("/api/sessao").status_code == 401
    with conectar() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessoes").fetchone()[0] == 0


def test_sessao_vence_em_8_horas_mesmo_em_uso(sessao):
    cliente, _ = sessao
    linha = _sessao_no_banco(cliente)
    duracao = datetime.fromisoformat(linha["expira_em"]) - datetime.fromisoformat(linha["criada_em"])
    assert duracao == timedelta(hours=8)

    # ultimo_uso segue recente: só o limite absoluto pode recusar.
    _sessao_no_banco(cliente, expira_em=_ha(seconds=1))
    assert cliente.get("/api/sessao").status_code == 401


def test_troca_de_senha_derruba_as_sessoes_abertas(sessao):
    cliente, _ = sessao
    assert cliente.get("/api/sessao").status_code == 200
    seguranca.definir_operador(USUARIO, "outra-senha-de-teste-678")
    assert cliente.get("/api/sessao").status_code == 401


# ------------------------------------------------------ operador na trilha

def test_revelar_comprador_registra_o_operador(sessao, pedido_cifrado):
    cliente, _ = sessao
    r = cliente.get(f"/api/pedido/{pedido_cifrado}/comprador")

    assert r.status_code == 200
    assert r.json()["nome"] == "Comprador Sintetico"
    with conectar() as conn:
        acesso = conn.execute("SELECT pedido_id, operacao, ator FROM acessos_pii"
                              " ORDER BY id DESC LIMIT 1").fetchone()
    assert tuple(acesso) == (pedido_cifrado, "leitura", USUARIO)


def test_aprovacao_e_recusa_registram_o_operador(sessao, fila_de_compras):
    cliente, csrf = sessao
    aprovar, recusar = fila_de_compras

    r = cliente.post(f"/api/aprovar/{aprovar}", headers=cabecalho(csrf))
    assert r.status_code == 200
    assert r.json()["ok"] is True

    r = cliente.post(f"/api/recusar/{recusar}", json={"motivo": "custo subiu"},
                     headers=cabecalho(csrf))
    assert r.status_code == 200

    with conectar() as conn:
        mensagens = [m[0] for m in conn.execute("SELECT mensagem FROM eventos")]
        resultado = conn.execute("SELECT status, resultado FROM aprovacoes WHERE id = ?",
                                 (recusar,)).fetchone()
    assert f"Ação {aprovar} liberada por {USUARIO}" in mensagens
    assert resultado["status"] == "recusada"
    assert resultado["resultado"] == f"custo subiu (por {USUARIO})"
