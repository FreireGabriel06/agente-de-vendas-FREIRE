"""Arquivos de implantação (deploy/): o site é preenchido sem sobrar campo, os
segredos e dados ficam fora da imagem e do git, e o modelo de instância liga o
que o painel exposto exige."""
import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
DEPLOY = RAIZ / "deploy"


def carregar_preencher():
    spec = importlib.util.spec_from_file_location("preencher_site", DEPLOY / "preencher_site.py")
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def test_site_preenchido_sem_campo_sobrando(tmp_path):
    preencher = carregar_preencher()
    dados = {"MARCA": "Marca Teste", "EMPRESA": "Empresa Teste Ltda", "ENDERECO": "Cidade, País",
             "EMAIL": "contato@example.com", "PAIS": "Brasil", "ANO": "2026", "DATA": "Oct 17, 2026"}
    destino = preencher.preencher(dados, destino=tmp_path / "pronto")
    paginas = {p.name: p.read_text(encoding="utf-8") for p in destino.glob("*.html")}
    assert set(paginas) == {"index.html", "privacy.html", "terms.html"}
    for nome, texto in paginas.items():
        assert "{{" not in texto, nome
        assert "Empresa Teste Ltda" in texto and "contato@example.com" in texto
    assert 'href="privacy.html"' in paginas["index.html"] and 'href="terms.html"' in paginas["index.html"]
    assert (destino / "estilo.css").exists()


def test_campo_faltando_e_recusado(tmp_path):
    preencher = carregar_preencher()
    with pytest.raises(SystemExit, match="Campo sem valor"):
        preencher.preencher({"MARCA": "x"}, destino=tmp_path / "pronto")


def test_politica_cobre_o_que_a_amazon_pede():
    texto = (DEPLOY / "site" / "privacy.html").read_text(encoding="utf-8")
    for trecho in ("30 days", "do not request\n    or store buyer names", "Anthropic", "HTTPS",
                   "authenticator code", "GDPR", "LGPD", "notify"):
        assert trecho in texto, trecho


@pytest.mark.parametrize("caminho", ["deploy/instancias/cliente1.env", "deploy/site-pronto/index.html",
                                     "deploy/.env"])
def test_arquivos_reais_de_implantacao_ficam_fora_do_git(caminho):
    r = subprocess.run(["git", "check-ignore", "-q", caminho], cwd=RAIZ)
    assert r.returncode == 0, caminho


def test_dockerignore_barra_dados_e_segredos():
    linhas = set((RAIZ / ".dockerignore").read_text(encoding="utf-8").split())
    for padrao in (".env", "*.db", ".chave_*", ".token_*", "*.agvbkp", "ordens_de_compra/",
                   "deploy/instancias/", ".git", ".venv"):
        assert padrao in linhas, padrao


def test_modelo_de_instancia_liga_o_que_o_painel_exposto_exige():
    texto = (DEPLOY / "instancia.exemplo.env").read_text(encoding="utf-8")
    assert "HOSTS_PAINEL=" in texto and "MFA_OBRIGATORIO=true" in texto
    assert "AMZ_APP_VERSAO=beta" in texto
    assert "AMZ_REDIRECT_URI=https://" in texto and "ML_REDIRECT_URI=https://" in texto
    compose = (DEPLOY / "docker-compose.yml").read_text(encoding="utf-8")
    assert "PROXY_IPS: 172.30.0.2" in compose and "ipv4_address: 172.30.0.2" in compose
    dockerfile = (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
    assert "USER agente" in dockerfile and "PROXY_HTTPS=true" in dockerfile


def carregar_verificador():
    spec = importlib.util.spec_from_file_location("verificar_site", DEPLOY / "verificar_site.py")
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def _site_falso(respostas):
    def buscar(url, seguir=True):
        r = respostas.get(url)
        if r is None:
            raise ConnectionRefusedError(url)
        return r
    return buscar


SITE_PRONTO = {
    "https://ex.com/": (200, {}, "<title>Home</title>"),
    "https://ex.com/privacy.html": (200, {}, "Privacy Policy of Marca"),
    "https://ex.com/terms.html": (200, {}, "Terms of Service of Marca"),
    "http://ex.com/": (308, {"Location": "https://ex.com/"}, ""),
    "https://cliente1.ex.com/saude": (200, {}, "{}"),
}


def test_verificador_aprova_site_pronto():
    v = carregar_verificador()
    resultados = v.verificar("ex.com", buscar=_site_falso(SITE_PRONTO))
    assert resultados and all(ok for ok, _ in resultados), resultados


@pytest.mark.parametrize("mudanca, trecho", [
    ({"https://ex.com/privacy.html": (200, {}, "Privacy Policy of {{EMPRESA}}")}, "sem campo por preencher"),
    ({"https://ex.com/terms.html": (404, {}, "")}, "terms.html: HTTP 404"),
    ({"http://ex.com/": (200, {}, "site sem https")}, "redireciona para HTTPS"),
    ({"https://cliente1.ex.com/saude": None}, "cliente1.ex.com/saude: sem resposta"),
])
def test_verificador_aponta_o_que_falta(mudanca, trecho):
    v = carregar_verificador()
    respostas = {**SITE_PRONTO, **mudanca}
    respostas = {k: r for k, r in respostas.items() if r is not None}
    falhas = [t for ok, t in v.verificar("ex.com", buscar=_site_falso(respostas)) if not ok]
    assert any(trecho in t for t in falhas), falhas


def test_verificador_aceita_dominio_com_esquema(capsys, monkeypatch):
    v = carregar_verificador()
    vistos = []
    monkeypatch.setattr(v, "verificar", lambda d, c: vistos.append((d, c)) or [(True, "ok")])
    assert v.main(["https://ex.com/"]) == 0
    assert vistos == [("ex.com", "cliente1")]
    assert v.main([]) == 1


BASH = shutil.which("bash")
DOCKER_FALSO = """#!/bin/sh
echo "$*" >> "$REGISTRO"
case "$*" in
  "compose config --services") printf 'caddy\ncliente1\ncliente2\n' ;;
  *"saude --completo"*) [ "$SAUDE_CODIGO" = 0 ] || exit "$SAUDE_CODIGO" ;;
  *"backup criar"*) cat > "$SENHA_VISTA" ;;
  "compose cp "*) touch "$DESTINO_CP/agente-backup-1.agvbkp" ;;
esac
exit 0
"""


def _rodar_rotina(tmp_path, saude="0", com_senha=True):
    falso = tmp_path / "docker"
    falso.write_text(DOCKER_FALSO, encoding="utf-8", newline="\n")
    pasta = tmp_path / "deploy"
    pasta.mkdir()
    script = pasta / "rotina_diaria.sh"
    script.write_bytes((DEPLOY / "rotina_diaria.sh").read_bytes().replace(b"\r\n", b"\n"))
    if com_senha:
        (pasta / ".backup_senha").write_text("senha-do-teste\n", encoding="utf-8", newline="\n")
    (pasta / "backups" / "cliente1").mkdir(parents=True)
    env = {**os.environ, "DOCKER": f"sh {falso.as_posix()}", "REGISTRO": str(tmp_path / "chamadas"),
           "SAUDE_CODIGO": saude, "SENHA_VISTA": str(tmp_path / "senha_vista"),
           "DESTINO_CP": str(pasta / "backups" / "cliente1")}
    r = subprocess.run([BASH, script.as_posix()], env=env, capture_output=True, text=True)
    chamadas = (tmp_path / "chamadas").read_text() if (tmp_path / "chamadas").exists() else ""
    return r, chamadas, pasta


@pytest.mark.skipif(BASH is None, reason="sem bash")
def test_rotina_faz_saude_e_backup_de_cada_cliente(tmp_path):
    r, chamadas, pasta = _rodar_rotina(tmp_path)
    assert r.returncode == 0, r.stderr
    for c in ("cliente1", "cliente2"):
        assert f"compose exec -T {c} python cli.py saude --completo" in chamadas
        assert f"compose cp {c}:/dados/backups/. backups/{c}/" in chamadas
    assert "caddy python" not in chamadas
    # A senha vai pela entrada padrão, nunca na linha de comando.
    assert (tmp_path / "senha_vista").read_text().strip() == "senha-do-teste"
    assert "senha-do-teste" not in chamadas
    assert "fim: código 0" in (pasta / "rotina.log").read_text(encoding="utf-8")


@pytest.mark.skipif(BASH is None, reason="sem bash")
def test_rotina_repassa_atencao_da_saude(tmp_path):
    r, _, _ = _rodar_rotina(tmp_path, saude="1")
    assert r.returncode == 1


@pytest.mark.skipif(BASH is None, reason="sem bash")
def test_rotina_sem_senha_nao_roda(tmp_path):
    r, chamadas, _ = _rodar_rotina(tmp_path, com_senha=False)
    assert r.returncode == 2 and ".backup_senha" in r.stderr
    assert chamadas == ""


def test_senha_e_backups_do_servidor_ficam_fora_do_git():
    for caminho in ("deploy/.backup_senha", "deploy/backups/cliente1/x.agvbkp", "deploy/rotina.log"):
        assert subprocess.run(["git", "check-ignore", "-q", caminho], cwd=RAIZ).returncode == 0, caminho
