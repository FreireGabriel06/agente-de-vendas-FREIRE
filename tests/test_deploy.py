"""Arquivos de implantação (deploy/): o site é preenchido sem sobrar campo, os
segredos e dados ficam fora da imagem e do git, e o modelo de instância liga o
que o painel exposto exige."""
import importlib.util
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
    assert "AMZ_REDIRECT_URI=https://" in texto
    compose = (DEPLOY / "docker-compose.yml").read_text(encoding="utf-8")
    assert "PROXY_IPS: 172.30.0.2" in compose and "ipv4_address: 172.30.0.2" in compose
    dockerfile = (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
    assert "USER agente" in dockerfile and "PROXY_HTTPS=true" in dockerfile
