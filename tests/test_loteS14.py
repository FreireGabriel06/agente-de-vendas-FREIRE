"""Lote S14 (pente fino): CSP com nonce, cookie Secure com HTTPS configurado,
resposta ao comprador conferida de novo na aprovação, containers endurecidos."""
import importlib.util
import re
import shutil
from pathlib import Path

import pytest

from core import aprovacao, conformidade

RAIZ = Path(__file__).resolve().parent.parent
HTML = (RAIZ / "painel" / "painel.html").read_text(encoding="utf-8")


# ------------------------------------------------------------------ CSP

def _nonce_do_cabecalho(resposta) -> str:
    csp = resposta.headers["Content-Security-Policy"]
    script = next(p for p in csp.split(";") if p.strip().startswith("script-src"))
    assert "unsafe-inline" not in script
    return re.search(r"'nonce-([A-Za-z0-9_-]+)'", script).group(1)


def test_login_so_roda_o_script_com_o_nonce_da_resposta(cliente):
    a, b = cliente.get("/login"), cliente.get("/login")
    na, nb = _nonce_do_cabecalho(a), _nonce_do_cabecalho(b)
    assert na != nb and len(na) >= 20
    assert f'<script nonce="{na}">' in a.text
    assert "{{NONCE}}" not in a.text


def test_painel_so_roda_o_script_com_o_nonce_da_resposta(sessao):
    cliente, _ = sessao
    r = cliente.get("/")
    nonce = _nonce_do_cabecalho(r)
    assert r.text.count("<script") == 1 and f'<script nonce="{nonce}">' in r.text


def test_painel_sem_manipulador_embutido():
    assert not re.search(r"\son[a-z]+\s*=", HTML)
    for acao in re.findall(r'data-acao="([A-Za-z]+)"', HTML):
        assert f"  {acao}:" in HTML, acao  # toda ação tem entrada em ACOES


# --------------------------------------------------------------- cookie

@pytest.mark.parametrize("ambiente, seguro", [
    ({}, False),
    ({"PROXY_HTTPS": "true"}, True),
    ({"TLS_CERT": "/x/cert.pem"}, True),
])
def test_cookie_secure_quando_ha_https_configurado(abrir_sessao, monkeypatch, ambiente, seguro):
    for nome in ("PROXY_HTTPS", "TLS_CERT"):
        monkeypatch.delenv(nome, raising=False)
    for nome, valor in ambiente.items():
        monkeypatch.setenv(nome, valor)
    from tests.apoio import SENHA, USUARIO
    cliente, _ = abrir_sessao()
    r = cliente.post("/api/login", json={"usuario": USUARIO, "senha": SENHA})
    cabecalho = r.headers["set-cookie"].lower()
    assert ("secure" in cabecalho) is seguro
    assert "httponly" in cabecalho and "samesite=lax" in cabecalho


# ------------------------------------------------- resposta ao comprador

def _resposta(texto):
    return aprovacao.enfileirar("resposta_cliente", "Responder pergunta 9",
                                {"question_id": 9, "pergunta": "tem azul?", "resposta": texto})


@pytest.mark.parametrize("texto, regra", [
    ("Chama no zap (11) 98765-4321", "CONTATO-EXTERNO"),
    ("Veja em https://loja.exemplo/azul", "CONTATO-EXTERNO"),
    ("Mande para vendas@loja.com.br", "CONTATO-EXTERNO"),
    ("   ", "RESPOSTA-VAZIA"),
    ("a" * 2001, "RESPOSTA-LONGA"),
])
def test_resposta_com_problema_fica_bloqueada_na_aprovacao(texto, regra):
    a = aprovacao.obter_pendente(_resposta(texto))
    r = aprovacao.checar(a)
    assert r.bloqueado and regra in [v.regra for v in r.violacoes]
    with pytest.raises(aprovacao.BloqueadoPelaConformidade):
        aprovacao.aprovar(a.id, {"resposta_cliente": lambda p: pytest.fail("não podia executar")})


def test_resposta_limpa_passa_e_ean_nao_e_telefone():
    a = aprovacao.obter_pendente(_resposta("Temos azul. EAN 7891234567895, envio em 24 h."))
    assert not aprovacao.checar(a).bloqueado


def test_bot_e_conformidade_usam_o_mesmo_padrao():
    from atendimento import bot
    assert bot._CONTATO is conformidade.CONTATO_EXTERNO


# ------------------------------------------------------------- deploy

ENDURECIMENTO = ("cap_drop: [ALL]", 'security_opt: ["no-new-privileges:true"]', "read_only: true",
                 'tmpfs: ["/tmp:size=64m,mode=1777"]', "mem_limit: 768m", "pids_limit: 256")


def test_compose_endurece_cliente_e_caddy():
    compose = (RAIZ / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")
    cliente = compose[compose.index("  cliente1:"):compose.index("volumes:\n  caddy_dados")]
    for trecho in ENDURECIMENTO:
        assert trecho in cliente, trecho
    caddy = compose[compose.index("  caddy:"):compose.index("  cliente1:")]
    assert "cap_drop: [ALL]" in caddy and "cap_add: [NET_BIND_SERVICE]" in caddy
    assert 'security_opt: ["no-new-privileges:true"]' in caddy


def test_cliente_novo_nasce_endurecido(tmp_path):
    spec = importlib.util.spec_from_file_location("novo_cliente", RAIZ / "deploy" / "novo_cliente.py")
    nc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(nc)
    shutil.copy(RAIZ / "deploy" / "instancia.exemplo.env", tmp_path / "instancia.exemplo.env")
    (tmp_path / ".env").write_text("DOMINIO=marca.com\n", encoding="utf-8")
    (tmp_path / "instancias").mkdir()
    nc.main([], tmp_path)
    override = (tmp_path / "docker-compose.override.yml").read_text(encoding="utf-8")
    for trecho in ENDURECIMENTO:
        assert trecho in override, trecho


def test_ci_sobe_a_imagem_endurecida():
    ci = (RAIZ / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    assert "--read-only" in ci and "--cap-drop ALL" in ci and "no-new-privileges:true" in ci


# ------------------------------------------------------- /saude e backups

def test_saude_publica_reaproveita_o_resultado_por_alguns_segundos(cliente, monkeypatch):
    from core import saude
    chamadas = []
    original = saude.verificar
    monkeypatch.setattr(saude, "verificar", lambda *a, **k: chamadas.append(1) or original(*a, **k))
    for _ in range(5):
        assert cliente.get("/saude").status_code in (200, 503)
    assert len(chamadas) == 1


def test_rotina_guarda_no_maximo_30_backups():
    script = (RAIZ / "deploy" / "rotina_diaria.sh").read_text(encoding="utf-8")
    assert '[ "$MANTER" -gt 30 ] && MANTER=30' in script


def _novo_cliente(tmp_path):
    spec = importlib.util.spec_from_file_location("novo_cliente", RAIZ / "deploy" / "novo_cliente.py")
    nc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(nc)
    shutil.copy(RAIZ / "deploy" / "instancia.exemplo.env", tmp_path / "instancia.exemplo.env")
    (tmp_path / ".env").write_text("DOMINIO=marca.com\n", encoding="utf-8")
    (tmp_path / "instancias").mkdir(exist_ok=True)
    return nc


def test_fim_de_contrato_tira_o_cliente_e_mostra_como_apagar(tmp_path, capsys):
    nc = _novo_cliente(tmp_path)
    nc.main([], tmp_path)
    nc.main([], tmp_path)
    capsys.readouterr()
    assert nc.main(["--remover", "cliente2"], tmp_path) == 0
    saida = capsys.readouterr().out
    assert not (tmp_path / "instancias" / "cliente2.env").exists()
    assert not (tmp_path / "clientes-caddy" / "cliente2.caddy").exists()
    override = (tmp_path / "docker-compose.override.yml").read_text(encoding="utf-8")
    assert "cliente2" not in override and "  cliente3:" in override
    for comando in ("docker rm -f agente-cliente2-1", "docker volume rm agente_dados_cliente2",
                    "rm -rf backups/cliente2"):
        assert comando in saida


@pytest.mark.parametrize("argv", [["--remover", "cliente1"], ["--remover", "../x"],
                                  ["--remover", "cliente9"], ["--remover"]])
def test_fim_de_contrato_recusa_o_que_nao_e_cliente_criado(tmp_path, argv):
    nc = _novo_cliente(tmp_path)
    with pytest.raises(SystemExit):
        nc.main(argv, tmp_path)


# ------------------------------------------------------ lote S15: servidor

def test_logs_dos_containers_tem_limite():
    compose = (RAIZ / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")
    assert compose.count('options: {max-size: "10m", max-file: "3"}') == 2  # caddy e cliente1


def test_cliente_novo_nasce_com_log_limitado(tmp_path):
    nc = _novo_cliente(tmp_path)
    nc.main([], tmp_path)
    override = (tmp_path / "docker-compose.override.yml").read_text(encoding="utf-8")
    assert 'options: {max-size: "10m", max-file: "3"}' in override


def test_hsts_do_site_cobre_os_subdominios():
    caddy = (RAIZ / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    assert 'Strict-Transport-Security "max-age=31536000; includeSubDomains"' in caddy


def test_guia_do_servidor_desliga_senha_no_ssh():
    guia = (RAIZ / "deploy" / "README.md").read_text(encoding="utf-8")
    assert "/etc/ssh/sshd_config.d/00-agente.conf" in guia and "PasswordAuthentication no" in guia
    assert "sshd -t" in guia and "unattended-upgrades" in guia
