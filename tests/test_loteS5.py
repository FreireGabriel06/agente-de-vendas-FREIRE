"""Lote S5 da auditoria de 06/10/2026: painel exposto à rede. Limite de login
por endereço (S-08), tempo igual para operador inexistente (S-07), sessão só
como hash no banco (S-79), Host validado (S-14), CSP e no-store (S-09), login
só em JSON (S-78), sem /openapi.json (S-24) e entradas com faixa (S-13)."""
import pytest

from apoio import SENHA, USUARIO
from core import seguranca
from db import conectar

ERRADA = {"usuario": USUARIO, "senha": "errada-errada"}
CERTA = {"usuario": USUARIO, "senha": SENHA}


def de(novo_cliente, ip):
    return novo_cliente(client=(ip, 50000))


# ------------------------------------------------------------ limite por endereço

def test_endereco_que_erra_demais_espera_e_o_operador_entra_de_outro(novo_cliente, operador):
    atacante = de(novo_cliente, "203.0.113.7")
    for _ in range(seguranca.FALHAS_POR_IP):
        assert atacante.post("/api/login", json=ERRADA).status_code == 401

    r = atacante.post("/api/login", json=CERTA)  # nem a senha certa passa
    assert r.status_code == 429
    assert 0 < int(r.headers["retry-after"]) <= seguranca.JANELA_IP_MIN * 60 + 1
    assert "set-cookie" not in r.headers

    # O operador legítimo, de outro endereço, não foi trancado.
    assert de(novo_cliente, "198.51.100.2").post("/api/login", json=CERTA).status_code == 200
    with conectar() as conn:
        assert conn.execute("SELECT bloqueado_ate FROM operadores").fetchone()[0] is None


def test_operador_inexistente_tambem_conta_para_o_endereco(novo_cliente, operador):
    cliente = de(novo_cliente, "203.0.113.8")
    for i in range(seguranca.FALHAS_POR_IP):
        r = cliente.post("/api/login", json={"usuario": f"ninguem{i}", "senha": "x" * 12})
        assert r.status_code == 401
    assert cliente.post("/api/login", json=CERTA).status_code == 429


def test_janela_vencida_libera_o_endereco(novo_cliente, operador):
    cliente = de(novo_cliente, "203.0.113.9")
    for _ in range(seguranca.FALHAS_POR_IP):
        cliente.post("/api/login", json=ERRADA)
    assert cliente.post("/api/login", json=CERTA).status_code == 429

    janela = seguranca.JANELA_IP_MIN * 60
    seguranca._falhas_por_ip["203.0.113.9"] = [
        t - janela - 1 for t in seguranca._falhas_por_ip["203.0.113.9"]]
    assert cliente.post("/api/login", json=CERTA).status_code == 200


def test_memoria_do_limite_tem_teto(monkeypatch):
    monkeypatch.setattr(seguranca, "IPS_VIGIADOS_MAX", 3)
    for i in range(10):
        seguranca._anotar_falha_do_ip(f"192.0.2.{i}")
    assert len(seguranca._falhas_por_ip) == 3


# ------------------------------------------------------------ tempo do login

def test_operador_inexistente_custa_um_scrypt_como_senha_errada(operador, monkeypatch):
    chamadas = []
    original = seguranca.conferir_hash
    monkeypatch.setattr(seguranca, "conferir_hash",
                        lambda senha, guardado: chamadas.append(1) or original(senha, guardado))

    for usuario in ("ninguem", USUARIO):
        with pytest.raises(seguranca.FalhaLogin, match="inválidos"):
            seguranca.autenticar(usuario, "errada-errada")
    assert len(chamadas) == 2


# ------------------------------------------------------------ sessão como hash

def test_banco_guarda_so_o_hash_da_sessao(sessao):
    cliente, _ = sessao
    sid = cliente.cookies.get(seguranca.COOKIE_SESSAO)
    with conectar() as conn:
        guardado = conn.execute("SELECT id FROM sessoes").fetchone()[0]
    assert guardado != sid and guardado == seguranca.hash_da_sessao(sid)

    # Quem copiou o banco tem o hash; o hash como cookie não abre nada.
    cliente.cookies.set(seguranca.COOKIE_SESSAO, guardado)
    assert cliente.get("/api/pendencias").status_code == 401


def test_sessoes_antigas_em_claro_saem_na_inicializacao(operador):
    with conectar() as conn:
        conn.execute("INSERT INTO sessoes (id, operador_id, csrf, criada_em, ultimo_uso, expira_em)"
                     " VALUES (?, 1, 'x', 'a', 'a', '2999-01-01T00:00:00+00:00')", ("a" * 43,))
    seguranca.inicializar_seguranca()
    with conectar() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessoes").fetchone()[0] == 0


def test_logout_apaga_a_sessao_pelo_hash(sessao):
    cliente, csrf = sessao
    assert cliente.post("/api/logout", headers={"X-CSRF-Token": csrf}).status_code == 200
    with conectar() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessoes").fetchone()[0] == 0


# ------------------------------------------------------------ Host

@pytest.mark.parametrize("host", ["atacante.invalid", "testserver.atacante.invalid", "", "[::1"])
def test_host_desconhecido_recebe_400(novo_cliente, host):
    r = novo_cliente().get("/login", headers={"host": host})
    assert r.status_code == 400 and r.json()["detail"] == "Host não permitido."
    assert r.headers["x-frame-options"] == "DENY"


@pytest.mark.parametrize("host", ["127.0.0.1:8777", "localhost", "LOCALHOST:8777", "[::1]:8777",
                                  "testserver", "testserver."])
def test_hosts_locais_e_o_configurado_passam(novo_cliente, host):
    assert novo_cliente().get("/login", headers={"host": host}).status_code == 200


def test_host_publico_vem_de_hosts_painel(novo_cliente, monkeypatch):
    monkeypatch.setenv("HOSTS_PAINEL", "painel.exemplo.com.br, 10.0.0.5")
    assert novo_cliente().get("/login", headers={"host": "painel.exemplo.com.br"}).status_code == 200
    assert novo_cliente().get("/login", headers={"host": "10.0.0.5:8777"}).status_code == 200
    assert novo_cliente().get("/login", headers={"host": "testserver"}).status_code == 400


def test_retorno_da_shopee_nao_usa_host_de_fora(sessao):
    cliente, _ = sessao
    r = cliente.get("/api/configuracao/url-autorizacao/shopee",
                    headers={"host": "atacante.invalid"})
    assert r.status_code == 400 and "atacante" not in r.text


def test_painel_fora_do_loopback_exige_hosts_painel(operador, monkeypatch, capsys):
    import executar
    import uvicorn

    monkeypatch.setenv("HOST_PAINEL", "0.0.0.0")
    monkeypatch.setenv("PROXY_HTTPS", "true")
    monkeypatch.delenv("HOSTS_PAINEL")
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: pytest.fail("não devia subir"))
    executar.main()
    assert "HOSTS_PAINEL" in capsys.readouterr().out


# ------------------------------------------------------------ cabeçalhos

def test_csp_e_no_store_em_toda_resposta(cliente, sessao):
    logado, _ = sessao
    for r in (cliente.get("/login"), cliente.get("/api/pendencias"), logado.get("/"),
              logado.get("/api/pendencias")):
        csp = r.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in csp and "connect-src 'self'" in csp
        assert "object-src 'none'" in csp and "base-uri 'none'" in csp
        assert r.headers["cache-control"] == "no-store"


# ------------------------------------------------------------ entradas

@pytest.mark.parametrize("tipo, corpo", [
    ("text/plain", '{"usuario": "operador.teste", "senha": "senha-de-teste-12345"}'),
    ("application/x-www-form-urlencoded", "usuario=operador.teste&senha=senha-de-teste-12345"),
    ("", '{"usuario": "operador.teste", "senha": "senha-de-teste-12345"}'),
])
def test_login_so_aceita_json(cliente, operador, tipo, corpo):
    r = cliente.post("/api/login", content=corpo.encode(), headers={"content-type": tipo})
    assert r.status_code == 415 and "set-cookie" not in r.headers


@pytest.mark.parametrize("corpo", ['[1, 2]', '"texto"', '{"usuario": ["a"], "senha": 5}',
                                   '{"usuario": "operador.teste", "senha": "x", "codigo": {}}',
                                   '{quebrado'])
def test_login_com_corpo_estranho_responde_401_sem_erro_500(cliente, operador, corpo):
    r = cliente.post("/api/login", content=corpo.encode(),
                     headers={"content-type": "application/json"})
    assert r.status_code == 401


def test_openapi_nao_aparece_nem_com_sessao(sessao):
    cliente, _ = sessao
    for caminho in ("/openapi.json", "/docs", "/redoc"):
        assert cliente.get(caminho).status_code == 404


@pytest.mark.parametrize("n, status", [(-1, 422), (0, 422), (201, 422), (1, 200), (200, 200)])
def test_eventos_com_faixa(sessao, n, status):
    cliente, _ = sessao
    assert cliente.get(f"/api/eventos?n={n}").status_code == status


@pytest.mark.parametrize("corpo", [{"custo": "nan"}, {"custo": "inf"}, {"custo": -5},
                                   {"custo": "abc"}, {}, {"custo": True}, {"custo": [1]},
                                   {"custo": 30, "margem": 95}, {"custo": 30, "tipo": "outro"},
                                   {"custo": 30, "margem": 85}])
def test_preco_recusa_entrada_invalida_com_422(sessao, corpo):
    cliente, csrf = sessao
    r = cliente.post("/api/preco", json=corpo, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 422, r.text


def test_preco_valido_continua_funcionando(sessao):
    cliente, csrf = sessao
    r = cliente.post("/api/preco", json={"custo": "30,50", "margem": 20, "tipo": "premium"},
                     headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
