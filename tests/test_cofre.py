"""Lote 4: credenciais de marketplace cifradas em repouso, no cofre.

A chave do cofre vem de CHAVE_COFRE (o conftest gera uma por sessão) ou do
arquivo .chave_cofre da pasta de dados do teste. Toda chamada HTTP dos
conectores é trocada por um dublê; a trava de rede do conftest continua valendo.
"""
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
import subprocess
import sys
import textwrap
import threading
import time
import types
import urllib.parse
from pathlib import Path

import pytest
from cryptography.fernet import Fernet, InvalidToken

import config
from apoio import cabecalho
from core import cofre
from db import conectar

RAIZ = Path(__file__).resolve().parent.parent
SEGREDO = "segredo-bem-especifico-0123456789"

# Um valor distinto para cada segredo que o painel grava.
SEGREDOS_DO_PAINEL = {
    "ML_CLIENT_SECRET": "valor-ml-client-secret-111",
    "ML_REFRESH_TOKEN": "valor-ml-refresh-222",
    "SHOPEE_PARTNER_KEY": "valor-shopee-partner-key-333",
    "SHOPEE_REFRESH_TOKEN": "valor-shopee-refresh-444",
    "AMZ_LWA_CLIENT_SECRET": "valor-amz-lwa-secret-555",
    "AMZ_REFRESH_TOKEN": "valor-amz-refresh-666",
    "ANTHROPIC_API_KEY": "valor-anthropic-key-777",
}


# ------------------------------------------------------------------ apoio

class Resposta:
    def __init__(self, status, corpo):
        self.status_code = status
        self.text = json.dumps(corpo)

    def json(self):
        return json.loads(self.text)


class HttpFalso:
    """Faz as vezes do módulo requests nos conectores: guarda o que foi
    enviado e devolve a resposta escolhida pelo teste."""

    def __init__(self, token: dict, chamada: dict | None = None):
        self.token = token
        self.chamada = chamada if chamada is not None else {"results": []}
        self.posts = []
        self.requests = []

    def post(self, url, data=None, json=None, params=None, timeout=None, **_):
        self.posts.append({"url": url, "data": data, "json": json, "params": params})
        return Resposta(200, self.token)

    def request(self, metodo, url, headers=None, params=None, json=None, timeout=None, **_):
        self.requests.append({"metodo": metodo, "url": url,
                              "headers": dict(headers or {}), "params": params})
        return Resposta(200, self.chamada)


def sem_http():
    def recusar(*_, **__):
        raise AssertionError("o conector não devia chamar o marketplace aqui")
    return types.SimpleNamespace(post=recusar, request=recusar)


def nova_chave() -> str:
    return Fernet.generate_key().decode()


def eventos() -> list[str]:
    with conectar() as conn:
        return [f"{l['nivel']} {l['origem']} {l['mensagem']} {l['detalhe_json'] or ''}"
                for l in conn.execute("SELECT * FROM eventos ORDER BY id")]


def cifrados() -> list[tuple]:
    with conectar() as conn:
        return [tuple(l) for l in conn.execute(
            "SELECT provedor, nome, valor_cifrado, atualizado_em FROM credenciais ORDER BY 1, 2")]


def escrever_legado(caminho: Path, **dados) -> tuple[bytes, int]:
    """Um .token_*.json antigo, como os conectores gravavam antes do cofre."""
    caminho.write_text(json.dumps(dados, indent=2), encoding="utf-8")
    return caminho.read_bytes(), caminho.stat().st_mtime_ns


def impressao(caminho: Path) -> tuple[bytes, int]:
    return caminho.read_bytes(), caminho.stat().st_mtime_ns


def nenhum_arquivo_contem(pasta: Path, *valores: str):
    for arquivo in pasta.rglob("*"):
        if arquivo.is_file():
            bruto = arquivo.read_bytes()
            for valor in valores:
                assert valor.encode() not in bruto, f"{valor!r} em claro em {arquivo.name}"


def no_banco(provedor: str, nome: str) -> str | None:
    """O que está gravado no banco, decifrado aqui, sem passar pela memória
    do processo."""
    with conectar() as conn:
        linha = conn.execute("SELECT valor_cifrado FROM credenciais WHERE provedor = ? AND nome = ?",
                             (provedor, nome)).fetchone()
    if linha is None:
        return None
    envelope = json.loads(Fernet(os.environ["CHAVE_COFRE"].encode()).decrypt(linha[0].encode()))
    assert (envelope["provedor"], envelope["nome"]) == (provedor, nome)
    return envelope["valor"]


def copiar_cifrado(origem: tuple[str, str], destino: tuple[str, str]):
    """Quem escreve no banco, sem a chave, copia o cifrado de uma linha para outra."""
    with conectar() as conn:
        conn.execute("UPDATE credenciais SET valor_cifrado = (SELECT valor_cifrado FROM credenciais"
                     " WHERE provedor = ? AND nome = ?) WHERE provedor = ? AND nome = ?",
                     (*origem, *destino))


class ServidorDeTokens:
    """Servidor de token falso com refresh de uso único, como o do ML e o da
    Shopee: cada refresh aceito devolve um novo, e o antigo deixa de valer."""

    def __init__(self, validos, prefixo="", demora=0.0):
        self.validos = set(validos)
        self.recebidos = []
        self.prefixo = prefixo
        self.demora = demora
        self._emitidos = 1
        self._trava = threading.Lock()

    def post(self, url, data=None, json=None, params=None, timeout=None, **_):
        refresh = (data or json or {})["refresh_token"]
        self.recebidos.append(refresh)
        time.sleep(self.demora)
        with self._trava:
            if refresh not in self.validos:
                return Resposta(400, {"error": "invalid_grant"})
            self.validos.discard(refresh)
            self._emitidos += 1
            n = self._emitidos
            self.validos.add(f"{self.prefixo}rt-{n}")
        return Resposta(200, {"access_token": f"{self.prefixo}at-{n}",
                              "refresh_token": f"{self.prefixo}rt-{n}",
                              "expires_in": 21600, "expire_in": 14400})

    def request(self, metodo, url, headers=None, params=None, json=None, timeout=None, **_):
        return Resposta(200, {"results": [], "response": {"order_list": []}})


@pytest.fixture
def sem_espera(monkeypatch):
    """Novas tentativas de gravação rápidas, sem os 30 s por tentativa."""
    monkeypatch.setattr(cofre, "ESPERA_BANCO_TOKEN", 0.2)
    monkeypatch.setattr(cofre, "PAUSA_TOKEN", 0.0)
    monkeypatch.setattr(cofre, "INTERVALO_REGRAVAR", 0.0)


# ------------------------------------------------------------- ida e volta

def test_guarda_le_troca_lista_e_apaga():
    assert cofre.ler("mercadolivre", "client_secret") is None

    cofre.guardar("mercadolivre", "client_secret", SEGREDO)
    assert cofre.ler("mercadolivre", "client_secret") == SEGREDO

    cofre.guardar("mercadolivre", "client_secret", "valor-trocado")
    assert cofre.ler("mercadolivre", "client_secret") == "valor-trocado"
    assert [(i["provedor"], i["nome"]) for i in cofre.listar()] == [("mercadolivre", "client_secret")]
    assert "valor-trocado" not in json.dumps(cofre.listar())

    assert cofre.apagar("mercadolivre", "client_secret") is True
    assert cofre.ler("mercadolivre", "client_secret") is None
    assert cofre.apagar("mercadolivre", "client_secret") is False


def test_valor_vazio_e_recusado():
    with pytest.raises(ValueError):
        cofre.guardar("amazon", "refresh_token", "")
    assert cifrados() == []


def test_banco_nunca_guarda_o_valor_em_claro(isolamento):
    valores = {"access_token": "APP_USR-acesso-" + "a" * 30,
               "refresh_token": "TG-refresh-" + "b" * 30}
    cofre.guardar_lote("mercadolivre", valores)

    chave = os.environ["CHAVE_COFRE"]
    for provedor, nome, cifrado, _ in cifrados():
        assert cifrado != valores[nome]
        # O valor é cifrado junto com a linha a que pertence.
        assert json.loads(Fernet(chave.encode()).decrypt(cifrado.encode())) == {
            "provedor": provedor, "nome": nome, "valor": valores[nome]}
        # A chave do cofre não é a chave do dado de comprador.
        with pytest.raises(InvalidToken):
            Fernet(os.environ["CHAVE_LGPD"].encode()).decrypt(cifrado.encode())

    # Nem o valor nem a chave aparecem em lugar nenhum da pasta de dados.
    nenhum_arquivo_contem(isolamento, *valores.values(), chave)


# ------------------------------------------------------------ falha fechada

def test_chave_errada_falha_fechado_sem_vazar_nada(monkeypatch):
    cofre.guardar("shopee", "partner_key", SEGREDO)
    antes = cifrados()
    errada = nova_chave()
    monkeypatch.setenv("CHAVE_COFRE", errada)

    with pytest.raises(cofre.ErroCofre) as erro:
        cofre.ler("shopee", "partner_key")
    mensagem = str(erro.value)
    assert "shopee/partner_key" in mensagem and "CHAVE_COFRE" in mensagem
    assert SEGREDO not in mensagem and errada not in mensagem
    assert erro.value.__suppress_context__ and erro.value.__cause__ is None

    # Gravar com a chave errada misturaria duas chaves na tabela: recusado.
    with pytest.raises(cofre.ErroCofre):
        cofre.guardar("shopee", "refresh_token", "rt-qualquer")
    assert cifrados() == antes

    # E não há volta para o valor antigo em texto puro do .env.
    monkeypatch.setattr(config, "DO_ARQUIVO_ENV", {"SHOPEE_PARTNER_KEY"})
    monkeypatch.setenv("SHOPEE_PARTNER_KEY", "antigo-em-texto-puro")
    with pytest.raises(cofre.ErroCofre):
        cofre.segredo("SHOPEE_PARTNER_KEY")


def test_chave_malformada_falha_fechado_e_nao_gera_outra(monkeypatch):
    monkeypatch.setenv("CHAVE_COFRE", "chave-malformada-xyz")
    for tentativa in (lambda: cofre.guardar("anthropic", "api_key", SEGREDO),
                      lambda: cofre.ler("anthropic", "api_key")):
        with pytest.raises(cofre.ErroCofre, match="CHAVE_COFRE inválida") as erro:
            tentativa()
        assert "chave-malformada-xyz" not in str(erro.value)
    assert not cofre.ARQ_CHAVE.exists()
    assert cifrados() == []


def test_sem_chave_e_com_dados_falha_sem_gerar_chave_nova(monkeypatch):
    cofre.guardar("amazon", "refresh_token", SEGREDO)
    monkeypatch.delenv("CHAVE_COFRE")

    for tentativa in (lambda: cofre.ler("amazon", "refresh_token"),
                      lambda: cofre.ler("amazon", "outro_nome"),
                      lambda: cofre.guardar("amazon", "lwa_client_secret", "x"),
                      cofre.rotacionar):
        with pytest.raises(cofre.ErroCofre, match="Chave do cofre ausente"):
            tentativa()
    assert not cofre.ARQ_CHAVE.exists()


def test_arquivo_de_chave_nasce_na_primeira_gravacao_e_pede_backup(monkeypatch):
    monkeypatch.delenv("CHAVE_COFRE")

    # Ler um cofre vazio não cria chave à toa.
    assert cofre.ler("mercadolivre", "refresh_token") is None
    assert not cofre.ARQ_CHAVE.exists()

    cofre.guardar("mercadolivre", "refresh_token", SEGREDO)
    chave = cofre.ARQ_CHAVE.read_bytes()
    Fernet(chave)  # chave válida
    assert cofre.ler("mercadolivre", "refresh_token") == SEGREDO
    if os.name == "posix":
        assert stat.S_IMODE(cofre.ARQ_CHAVE.stat().st_mode) == 0o600

    avisos = [e for e in eventos() if "backup" in e]
    assert len(avisos) == 1
    assert avisos[0].startswith("atencao cofre") and ".chave_cofre" in avisos[0]
    assert not any(chave.decode() in e or SEGREDO in e for e in eventos())

    # A segunda gravação usa a mesma chave, sem aviso novo.
    cofre.guardar("mercadolivre", "access_token", "at-qualquer")
    assert cofre.ARQ_CHAVE.read_bytes() == chave
    assert len([e for e in eventos() if "backup" in e]) == 1


def test_pasta_de_dados_sem_escrita_falha_fechado(monkeypatch, tmp_path):
    monkeypatch.delenv("CHAVE_COFRE")
    monkeypatch.setattr(cofre, "ARQ_CHAVE", tmp_path / "nao-existe" / ".chave_cofre")

    with pytest.raises(cofre.ErroCofre, match="Não foi possível criar .chave_cofre"):
        cofre.guardar("anthropic", "api_key", SEGREDO)
    assert cifrados() == []
    assert not any(SEGREDO in e for e in eventos())


def test_chave_cofre_no_arquivo_env_e_ignorada(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("CHAVE_COFRE")
    monkeypatch.delenv("VARIAVEL_TESTE_COFRE", raising=False)
    arquivo = tmp_path / "outro.env"
    arquivo.write_text(f"CHAVE_COFRE={nova_chave()}\nVARIAVEL_TESTE_COFRE=7\n", encoding="utf-8")

    config._carregar_env(arquivo)

    assert "CHAVE_COFRE" not in os.environ
    assert os.environ["VARIAVEL_TESTE_COFRE"] == "7"
    assert "VARIAVEL_TESTE_COFRE" in config.DO_ARQUIVO_ENV
    assert "CHAVE_COFRE no .env é ignorada" in capsys.readouterr().err


# ------------------------------------------------------------------ rotação

def test_rotacao_le_com_a_chave_antiga_e_recifra_com_a_nova(monkeypatch):
    antiga = os.environ["CHAVE_COFRE"]
    cofre.guardar_lote("mercadolivre", {"access_token": "at-" + "1" * 20,
                                        "refresh_token": "rt-" + "2" * 20})
    cofre.guardar("anthropic", "api_key", SEGREDO)

    nova = nova_chave()
    monkeypatch.setenv("CHAVE_COFRE", f"{nova},{antiga}")
    # Antes de rotacionar tudo continua legível, pela chave de trás.
    assert cofre.ler("mercadolivre", "refresh_token") == "rt-" + "2" * 20
    # O que é gravado agora já sai com a chave da frente.
    cofre.guardar("shopee", "partner_key", "pk-nova")
    Fernet(nova.encode()).decrypt(dict(((p, n), c) for p, n, c, _ in cifrados())[
        ("shopee", "partner_key")].encode())

    assert cofre.rotacionar() == 4
    for _, _, cifrado, _ in cifrados():
        Fernet(nova.encode()).decrypt(cifrado.encode())
        with pytest.raises(InvalidToken):
            Fernet(antiga.encode()).decrypt(cifrado.encode())

    monkeypatch.setenv("CHAVE_COFRE", nova)  # a antiga já pode sair
    assert cofre.ler("anthropic", "api_key") == SEGREDO
    assert cofre.ler("mercadolivre", "access_token") == "at-" + "1" * 20
    monkeypatch.setenv("CHAVE_COFRE", antiga)
    with pytest.raises(cofre.ErroCofre):
        cofre.ler("anthropic", "api_key")
    assert any("recifrado" in e for e in eventos())


def test_rotacao_pelo_arquivo_de_chave(monkeypatch):
    monkeypatch.delenv("CHAVE_COFRE")
    cofre.guardar("amazon", "lwa_client_secret", SEGREDO)
    antiga = cofre.ARQ_CHAVE.read_text(encoding="ascii")
    nova = nova_chave()
    cofre.ARQ_CHAVE.write_text(f"{nova}\n{antiga}\n", encoding="ascii")

    assert cofre.rotacionar() == 1
    cofre.ARQ_CHAVE.write_text(nova, encoding="ascii")
    assert cofre.ler("amazon", "lwa_client_secret") == SEGREDO


def test_rotacao_sem_a_chave_antiga_nao_altera_nada(monkeypatch):
    cofre.guardar("shopee", "partner_key", SEGREDO)
    cofre.guardar("shopee", "refresh_token", "rt-shopee")
    antes = cifrados()
    monkeypatch.setenv("CHAVE_COFRE", nova_chave())

    with pytest.raises(cofre.ErroCofre, match="Nada foi recifrado"):
        cofre.rotacionar()
    assert cifrados() == antes


# ----------------------------------------------------------- arquivo legado

def test_arquivo_legado_do_ml_e_importado_sem_ser_alterado(monkeypatch):
    from conectores import mercadolivre

    tokens = {"access_token": "APP_USR-legado-" + "c" * 20,
              "refresh_token": "TG-legado-" + "d" * 20,
              "expira_em": int(time.time()) + 3600}
    antes = escrever_legado(mercadolivre.ARQ_TOKEN, **tokens)
    monkeypatch.setattr(mercadolivre, "requests", sem_http())

    assert mercadolivre.MercadoLivre().token == tokens["access_token"]  # sem renovar

    assert impressao(mercadolivre.ARQ_TOKEN) == antes
    assert cofre.ler("mercadolivre", "refresh_token") == tokens["refresh_token"]
    assert cofre.ler("mercadolivre", "expira_em") == str(tokens["expira_em"])
    avisos = [e for e in eventos() if ".token_ml.json" in e]
    assert len(avisos) == 1
    assert avisos[0].startswith("atencao cofre") and "à mão" in avisos[0]
    assert not any(tokens["access_token"] in e or tokens["refresh_token"] in e for e in eventos())

    # Outra instância lê do cofre e não importa de novo.
    assert mercadolivre.MercadoLivre().token == tokens["access_token"]
    assert len([e for e in eventos() if ".token_ml.json" in e]) == 1
    assert impressao(mercadolivre.ARQ_TOKEN) == antes


def test_arquivo_legado_nao_sobrescreve_o_cofre():
    from conectores import shopee

    cofre.guardar_lote("shopee", {"access_token": "at-novo", "refresh_token": "rt-novo"})
    antes = escrever_legado(shopee.ARQ_TOKEN, access_token="at-velho",
                            refresh_token="rt-velho", expira_em=0)

    assert cofre.importar_legado("shopee", shopee.ARQ_TOKEN) is False
    assert cofre.ler("shopee", "refresh_token") == "rt-novo"
    assert impressao(shopee.ARQ_TOKEN) == antes
    assert not any(".token_shopee.json" in e for e in eventos())


def test_arquivo_legado_ilegivel_nao_quebra_e_nao_e_alterado():
    from conectores import mercadolivre

    mercadolivre.ARQ_TOKEN.write_text("{isto não é json", encoding="utf-8")
    antes = impressao(mercadolivre.ARQ_TOKEN)

    assert cofre.importar_legado("mercadolivre", mercadolivre.ARQ_TOKEN) is False
    assert impressao(mercadolivre.ARQ_TOKEN) == antes
    assert cifrados() == []
    assert any("ilegível" in e for e in eventos())


# --------------------------------------------------------------- conectores

def test_mercado_livre_renova_e_guarda_pelo_cofre(monkeypatch, isolamento):
    from conectores import mercadolivre

    http = HttpFalso({"access_token": "at-novo-ml", "refresh_token": "rt-novo-ml",
                      "expires_in": 21600})
    monkeypatch.setattr(mercadolivre, "requests", http)
    monkeypatch.delenv("ML_CLIENT_SECRET")
    cofre.guardar_segredo("ML_CLIENT_SECRET", "cs-do-cofre")
    cofre.guardar("mercadolivre", "refresh_token", "rt-velho-ml")

    ml = mercadolivre.MercadoLivre()
    assert ml.pedidos_recentes() == []

    renovacao = http.posts[0]["data"]
    assert (renovacao["client_secret"], renovacao["refresh_token"]) == ("cs-do-cofre", "rt-velho-ml")
    assert http.requests[0]["headers"]["Authorization"] == "Bearer at-novo-ml"
    assert cofre.ler("mercadolivre", "refresh_token") == "rt-novo-ml"
    assert cofre.ler("mercadolivre", "access_token") == "at-novo-ml"
    assert int(cofre.ler("mercadolivre", "expira_em")) > time.time()

    # Outra instância usa o token guardado, sem renovar.
    assert mercadolivre.MercadoLivre().token == "at-novo-ml"
    assert len(http.posts) == 1

    # O segredo é lido na hora do uso: trocado no cofre, a próxima renovação já usa o novo.
    cofre.guardar_segredo("ML_CLIENT_SECRET", "cs-trocado")
    ml._pelo_cofre(ml._renovar)
    assert http.posts[1]["data"]["client_secret"] == "cs-trocado"
    assert http.posts[1]["data"]["refresh_token"] == "rt-novo-ml"

    # Nada de arquivo de token, nem valor em claro em arquivo algum.
    assert not mercadolivre.ARQ_TOKEN.exists()
    nenhum_arquivo_contem(isolamento, "at-novo-ml", "rt-novo-ml", "cs-do-cofre", "cs-trocado")


def test_shopee_importa_renova_e_assina_pelo_cofre(monkeypatch, isolamento):
    from conectores import shopee

    http = HttpFalso({"access_token": "at-novo-sp", "refresh_token": "rt-novo-sp",
                      "expire_in": 14400},
                     chamada={"response": {"order_list": [{"order_sn": "SN-1"}]}})
    monkeypatch.setattr(shopee, "requests", http)
    monkeypatch.delenv("SHOPEE_PARTNER_KEY")
    cofre.guardar_segredo("SHOPEE_PARTNER_KEY", "pk-do-cofre")
    antes = escrever_legado(shopee.ARQ_TOKEN, access_token="at-velho-sp",
                            refresh_token="rt-velho-sp", expira_em=0)

    s = shopee.Shopee()
    assert s.pedidos_recentes() == [{"order_sn": "SN-1"}]

    renovacao = http.posts[0]
    assert renovacao["json"]["refresh_token"] == "rt-velho-sp"  # veio do arquivo legado
    base = f"{s.partner_id}/api/v2/auth/access_token/get{renovacao['params']['timestamp']}"
    assert renovacao["params"]["sign"] == hmac.new(b"pk-do-cofre", base.encode(),
                                                   hashlib.sha256).hexdigest()
    assert http.requests[0]["params"]["access_token"] == "at-novo-sp"
    assert cofre.ler("shopee", "refresh_token") == "rt-novo-sp"
    assert impressao(shopee.ARQ_TOKEN) == antes
    nenhum_arquivo_contem(isolamento, "at-novo-sp", "rt-novo-sp", "pk-do-cofre")


def test_troca_do_codigo_da_shopee_guarda_tokens_no_cofre(monkeypatch, isolamento):
    from conectores import shopee
    from painel import configurar

    monkeypatch.setattr(shopee, "requests", HttpFalso(
        {"access_token": "at-sp-troca", "refresh_token": "rt-sp-troca", "expire_in": 14400}))

    configurar.shopee_trocar_code("CODIGO-TESTE", "2000002")

    assert cofre.ler("shopee", "refresh_token") == "rt-sp-troca"
    env = (isolamento / ".env").read_text(encoding="utf-8")
    assert "SHOPEE_SHOP_ID=2000002" in env
    assert "rt-sp-troca" not in env and "at-sp-troca" not in env
    assert configurar.status()["shopee"]["autorizado"] is True


def test_troca_do_codigo_do_ml_nao_gasta_o_codigo_com_o_cofre_quebrado(monkeypatch):
    from painel import configurar

    chamadas = []
    monkeypatch.setattr(configurar, "requests",
                        types.SimpleNamespace(post=lambda *a, **k: chamadas.append(a)))
    cofre.guardar("mercadolivre", "refresh_token", "rt-antigo")
    sid = "sessao-" + "c" * 40
    url = configurar.ml_url_autorizacao(sid)
    state = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["state"][0]
    monkeypatch.setenv("CHAVE_COFRE", nova_chave())

    with pytest.raises(cofre.ErroCofre):
        configurar.ml_trocar_code(
            "https://localhost:8777/oauth/ml/retorno?code=C1&state=" + state, sid)
    assert chamadas == []


def test_cofre_com_chave_errada_nao_derruba_o_worker(monkeypatch):
    import worker

    cofre.guardar("mercadolivre", "refresh_token", "rt-guardado")
    monkeypatch.setenv("CHAVE_COFRE", nova_chave())

    resumo = worker.ciclo()

    assert resumo["ingeridos"] == 0 and resumo["perguntas"] == 0
    assert resumo["analisados"] == 0 and resumo["ordens_montadas"] == 0
    falhas = [e for e in eventos() if "Ingestão ML falhou" in e]
    assert len(falhas) == 1 and "não abre com a chave atual do cofre" in falhas[0]
    assert not any("rt-guardado" in e for e in eventos())


def test_amazon_le_pelo_cofre_e_falha_como_erro_do_conector(monkeypatch):
    from conectores import amazon

    http = HttpFalso({"access_token": "at-amz", "expires_in": 3600})
    monkeypatch.setattr(amazon, "requests", http)
    monkeypatch.delenv("AMZ_LWA_CLIENT_SECRET")
    cofre.guardar_segredo("AMZ_LWA_CLIENT_SECRET", "amz-cs-cofre")
    cofre.guardar_segredo("AMZ_REFRESH_TOKEN", "amz-rt-cofre")

    assert amazon.Amazon().token == "at-amz"
    enviado = http.posts[0]["data"]
    assert (enviado["client_secret"], enviado["refresh_token"]) == ("amz-cs-cofre", "amz-rt-cofre")

    monkeypatch.setenv("CHAVE_COFRE", nova_chave())
    with pytest.raises(amazon.ErroAmazon, match="não abre com a chave atual do cofre") as erro:
        amazon.Amazon().token
    assert "amz-cs-cofre" not in str(erro.value) and "amz-rt-cofre" not in str(erro.value)


def test_bot_le_a_chave_da_api_pelo_cofre(monkeypatch):
    from apoio import AnthropicFalso, resposta_claude
    from atendimento import bot, claude_api

    falso = AnthropicFalso(resposta_claude("Resposta pronta."))
    monkeypatch.setattr(claude_api.anthropic, "Anthropic", falso)

    assert bot.redigir("tem garantia?", {}).startswith("ESCALAR: ANTHROPIC_API_KEY")
    assert falso.criados == []  # sem chave, nem cliente é criado
    cofre.guardar_segredo("ANTHROPIC_API_KEY", "sk-do-cofre")
    assert bot.redigir("tem garantia?", {}) == "Resposta pronta."
    assert falso.criados[0]["api_key"] == "sk-do-cofre"  # passada explícita ao SDK


# ----------------------------------------------------------- ordem de leitura

def test_ambiente_do_sistema_vence_o_cofre(monkeypatch):
    cofre.guardar_segredo("ML_CLIENT_SECRET", "do-cofre")
    monkeypatch.setenv("ML_CLIENT_SECRET", "do-sistema")
    assert cofre.segredo("ML_CLIENT_SECRET") == "do-sistema"
    assert config.config.ml.client_secret == "do-sistema"

    cofre.guardar_segredo("AMZ_LWA_CLIENT_SECRET", "amz-do-cofre")
    assert config.config.amazon.lwa_client_secret == "amz-segredo-falso"  # do conftest
    monkeypatch.delenv("AMZ_LWA_CLIENT_SECRET")
    assert config.config.amazon.lwa_client_secret == "amz-do-cofre"


def test_valor_antigo_do_env_continua_valendo_ate_haver_um_no_cofre(monkeypatch):
    # Como o config.py carrega um segredo que estava em texto puro no .env.
    monkeypatch.setattr(config, "DO_ARQUIVO_ENV", {"ML_CLIENT_SECRET"})
    monkeypatch.setenv("ML_CLIENT_SECRET", "antigo-do-arquivo")
    assert config.config.ml.client_secret == "antigo-do-arquivo"

    # Gravado pelo painel, o do cofre passa a valer sobre o do arquivo.
    cofre.guardar_segredo("ML_CLIENT_SECRET", "novo-do-painel")
    assert config.config.ml.client_secret == "novo-do-painel"


def test_segredo_e_lido_na_hora_do_uso_e_nao_na_importacao(monkeypatch):
    monkeypatch.delenv("ML_CLIENT_SECRET")
    assert config.config.ml.client_secret == ""
    assert config.config.ml.configurado is False
    cofre.guardar_segredo("ML_CLIENT_SECRET", "gravado-depois")
    cofre.guardar_segredo("ML_REFRESH_TOKEN", "rt-gravado-depois")
    assert config.config.ml.client_secret == "gravado-depois"
    assert config.config.ml.configurado is True


def test_refresh_token_renovado_no_cofre_vence_o_do_ambiente(monkeypatch):
    # O ML e a Shopee trocam o refresh token a cada renovação: o do ambiente é
    # só o ponto de partida, e depois da primeira renovação já foi gasto.
    monkeypatch.setenv("ML_REFRESH_TOKEN", "rt-inicial-do-ambiente")
    assert config.config.ml.refresh_token == "rt-inicial-do-ambiente"
    cofre.guardar("mercadolivre", "refresh_token", "rt-renovado")
    assert config.config.ml.refresh_token == "rt-renovado"


# ------------------------------------------------------------------- painel

def test_gravar_env_manda_segredo_para_o_cofre_e_o_resto_para_o_env(isolamento):
    from painel import configurar

    assert set(SEGREDOS_DO_PAINEL) == set(cofre.SEGREDOS)
    destino = configurar.gravar_env({**SEGREDOS_DO_PAINEL, "ML_CLIENT_ID": "id-publico-ml",
                                     "MARGEM_MINIMA_PCT": "21"})

    env = (isolamento / ".env").read_text(encoding="utf-8")
    assert "ML_CLIENT_ID=id-publico-ml" in env and "MARGEM_MINIMA_PCT=21" in env
    for variavel, valor in SEGREDOS_DO_PAINEL.items():
        assert valor not in env
        assert os.environ.get(variavel) != valor
        assert cofre.ler(*cofre.SEGREDOS[variavel]) == valor
    assert destino["cofre"] == sorted(SEGREDOS_DO_PAINEL)
    assert destino["env"] == ["MARGEM_MINIMA_PCT", "ML_CLIENT_ID"]
    nenhum_arquivo_contem(isolamento, *SEGREDOS_DO_PAINEL.values())


def test_gravar_env_so_com_segredo_nem_cria_o_env(isolamento):
    from painel import configurar

    configurar.gravar_env({"ANTHROPIC_API_KEY": "sk-teste-999"})
    assert not (isolamento / ".env").exists()


def test_gravar_env_nao_mexe_na_linha_antiga_de_segredo(isolamento):
    from painel import configurar

    env = isolamento / ".env"
    env.write_bytes(b"ML_CLIENT_SECRET=antigo-em-texto-puro\nML_CLIENT_ID=abc\n")
    antes = impressao(env)

    destino = configurar.gravar_env({"ML_CLIENT_SECRET": "novo-no-cofre"})

    assert impressao(env) == antes
    assert destino["em_texto_no_env"] == ["ML_CLIENT_SECRET"]
    assert cofre.ler("mercadolivre", "client_secret") == "novo-no-cofre"


def test_api_salvar_guarda_segredo_no_cofre_e_nao_devolve_valor(sessao, isolamento):
    cliente, csrf = sessao
    segredos = {k: v for k, v in SEGREDOS_DO_PAINEL.items() if k not in cofre.ROTATIVOS}

    r = cliente.post("/api/configuracao/salvar", json={**segredos, "ML_CLIENT_ID": "id-publico-ml"},
                     headers=cabecalho(csrf))

    assert r.status_code == 200
    assert r.json()["no_cofre"] == sorted(segredos)
    assert r.json()["salvas"] == sorted([*segredos, "ML_CLIENT_ID"])
    # O conftest põe ML_CLIENT_SECRET no ambiente do sistema: o painel avisa que ele vence.
    assert "ML_CLIENT_SECRET" in r.json()["aviso"]
    for variavel, valor in segredos.items():
        assert valor not in r.text
        assert cofre.ler(*cofre.SEGREDOS[variavel]) == valor
    assert all(v not in (isolamento / ".env").read_text(encoding="utf-8") for v in segredos.values())

    tela = cliente.get("/api/configuracao")
    assert tela.json()["claude"]["app_criado"] is True  # a chave só existe no cofre
    assert all(v not in tela.text for v in segredos.values())


def test_painel_mostra_o_erro_do_cofre_sem_quebrar(sessao, monkeypatch):
    cliente, csrf = sessao
    monkeypatch.delenv("SHOPEE_PARTNER_KEY")
    cofre.guardar_segredo("ANTHROPIC_API_KEY", "sk-guardada")
    cofre.guardar_segredo("SHOPEE_PARTNER_KEY", "pk-guardada")
    errada = nova_chave()
    monkeypatch.setenv("CHAVE_COFRE", errada)

    tela = cliente.get("/api/configuracao")
    assert tela.status_code == 200
    assert "não abre com a chave atual do cofre" in tela.json()["claude"]["aviso"]
    assert tela.json()["claude"]["app_criado"] is False

    r = cliente.post("/api/configuracao/salvar", json={"ANTHROPIC_API_KEY": "sk-nova"},
                     headers=cabecalho(csrf))
    assert r.status_code == 503 and "não confere" in r.json()["detail"]

    link = cliente.get("/api/configuracao/url-autorizacao/shopee")
    assert link.status_code == 503 and "shopee/partner_key" in link.json()["detail"]
    inicio = cliente.get("/oauth/shopee/iniciar", follow_redirects=False)
    assert inicio.status_code == 400 and "shopee/partner_key" in inicio.text

    for texto in (tela.text, r.text, link.text, inicio.text):
        for valor in ("sk-guardada", "sk-nova", "pk-guardada", errada):
            assert valor not in texto


# ---------------------------------------------------------------- eventos

def test_nenhum_segredo_nem_chave_aparece_nos_eventos(monkeypatch, sessao, isolamento, capsys):
    import cli
    import worker
    from conectores import mercadolivre

    cliente, csrf = sessao
    painel = {"ML_CLIENT_SECRET": "ev-ml-cs", "SHOPEE_PARTNER_KEY": "ev-sp-pk",
              "AMZ_LWA_CLIENT_SECRET": "ev-amz-cs", "AMZ_REFRESH_TOKEN": "ev-amz-rt",
              "ANTHROPIC_API_KEY": "ev-anthropic"}
    assert cliente.post("/api/configuracao/salvar", json=painel,
                        headers=cabecalho(csrf)).status_code == 200

    # Arquivo legado importado, depois renovação do token do ML.
    escrever_legado(mercadolivre.ARQ_TOKEN, access_token="ev-ml-at-legado",
                    refresh_token="ev-ml-rt-legado", expira_em=0)
    monkeypatch.setattr(mercadolivre, "requests", HttpFalso(
        {"access_token": "ev-ml-at-novo", "refresh_token": "ev-ml-rt-novo", "expires_in": 21600}))
    assert mercadolivre.MercadoLivre().pedidos_recentes() == []

    # Migração pela CLI e rotação de chave.
    (isolamento / ".env").write_text("SHOPEE_REFRESH_TOKEN=ev-sp-rt-env\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["cli.py", "cofre", "migrar"])
    cli.main()
    antiga, nova = os.environ["CHAVE_COFRE"], nova_chave()
    monkeypatch.setenv("CHAVE_COFRE", f"{nova},{antiga}")
    assert cofre.rotacionar() > 0

    # Chave errada: o worker registra a falha.
    errada = nova_chave()
    monkeypatch.setenv("CHAVE_COFRE", errada)
    assert worker.ingerir_mercadolivre(mercadolivre.MercadoLivre()) == 0

    proibidos = [*painel.values(), "ev-ml-at-legado", "ev-ml-rt-legado", "ev-ml-at-novo",
                 "ev-ml-rt-novo", "ev-sp-rt-env", antiga, nova, errada]
    registrados = eventos()
    assert len(registrados) >= 6
    assert any("Ingestão ML falhou" in e for e in registrados)
    for valor in proibidos:
        assert not any(valor in e for e in registrados), valor
    saida = capsys.readouterr()
    for valor in proibidos:
        assert valor not in saida.out and valor not in saida.err


# --------------------------------------------------------------------- CLI

def test_cli_migrar_copia_confere_e_nao_altera_o_env(monkeypatch, capsys, isolamento):
    import cli
    from conectores import mercadolivre

    env = isolamento / ".env"
    env.write_bytes(b"# comentario do operador\nML_CLIENT_ID=id-publico\n"
                    b"ML_CLIENT_SECRET=mig-ml-cs\nANTHROPIC_API_KEY='mig-anthropic'\n"
                    b"AMZ_REFRESH_TOKEN=\nMARGEM_MINIMA_PCT=20\n")
    antes_env = impressao(env)
    antes_ml = escrever_legado(mercadolivre.ARQ_TOKEN, access_token="mig-ml-at",
                               refresh_token="mig-ml-rt", expira_em=int(time.time()) + 999)
    monkeypatch.setattr(sys, "argv", ["cli.py", "cofre", "migrar"])

    cli.main()
    saida = capsys.readouterr().out

    assert impressao(env) == antes_env
    assert impressao(mercadolivre.ARQ_TOKEN) == antes_ml
    assert cofre.ler("mercadolivre", "client_secret") == "mig-ml-cs"
    assert cofre.ler("anthropic", "api_key") == "mig-anthropic"
    assert cofre.ler("mercadolivre", "refresh_token") == "mig-ml-rt"
    assert cofre.ler("amazon", "refresh_token") is None  # vazio no .env não entra

    linhas = next(l for l in saida.splitlines() if "as linhas:" in l)
    assert str(env) in linhas
    assert "ML_CLIENT_SECRET" in linhas and "ANTHROPIC_API_KEY" in linhas
    assert "ML_CLIENT_ID" not in linhas and "MARGEM_MINIMA_PCT" not in linhas
    assert f"o arquivo {mercadolivre.ARQ_TOKEN}" in saida
    for valor in ("mig-ml-cs", "mig-anthropic", "mig-ml-at", "mig-ml-rt", "id-publico"):
        assert valor not in saida

    # De novo: nada muda, e o relatório diz que já estava tudo lá.
    cli.main()
    saida = capsys.readouterr().out
    assert "Já estavam no cofre com o mesmo valor:   ML_CLIENT_SECRET, ANTHROPIC_API_KEY" in saida
    assert impressao(env) == antes_env
    migracoes = [e for e in eventos() if "Migração:" in e]
    assert len(migracoes) == 1 and "O .env não foi alterado" in migracoes[0]


def test_cli_migrar_para_se_a_leitura_de_conferencia_nao_bate(monkeypatch, capsys, isolamento):
    import cli

    env = isolamento / ".env"
    env.write_bytes(b"ANTHROPIC_API_KEY=mig-conferencia\n")
    antes = impressao(env)
    monkeypatch.setattr(cofre, "guardar", lambda *a, **k: None)  # gravação que não chega ao banco
    monkeypatch.setattr(sys, "argv", ["cli.py", "cofre", "migrar"])

    with pytest.raises(SystemExit) as saida:
        cli.main()
    assert saida.value.code == 1
    capturado = capsys.readouterr()
    assert "conferência de ANTHROPIC_API_KEY" in capturado.err
    assert "apagar à mão" not in capturado.out  # nada é dado como pronto para apagar
    assert "mig-conferencia" not in capturado.out + capturado.err
    assert impressao(env) == antes


def test_importacao_do_legado_para_se_a_leitura_de_conferencia_nao_bate(monkeypatch):
    from conectores import mercadolivre

    antes = escrever_legado(mercadolivre.ARQ_TOKEN, access_token="at-conf", refresh_token="rt-conf")
    # Gravação que diz ter gravado, mas não chega ao banco.
    monkeypatch.setattr(cofre, "_gravar", lambda *a, **k: True)

    with pytest.raises(cofre.ErroCofre, match="conferência"):
        cofre.importar_legado("mercadolivre", mercadolivre.ARQ_TOKEN)
    assert impressao(mercadolivre.ARQ_TOKEN) == antes
    assert not any("importados" in e for e in eventos())


def test_cli_migrar_sem_nada_para_migrar(monkeypatch, capsys, isolamento):
    import cli

    monkeypatch.setattr(sys, "argv", ["cli.py", "cofre", "migrar"])
    cli.main()
    assert "Nada para migrar" in capsys.readouterr().out
    assert not (isolamento / ".env").exists()


def test_cli_rotacionar(monkeypatch, capsys):
    import cli

    antiga, nova = os.environ["CHAVE_COFRE"], nova_chave()
    cofre.guardar("shopee", "partner_key", SEGREDO)
    monkeypatch.setenv("CHAVE_COFRE", f"{nova},{antiga}")
    monkeypatch.setattr(sys, "argv", ["cli.py", "cofre", "rotacionar"])

    cli.main()
    saida = capsys.readouterr().out
    assert saida.startswith("1 credencial(is) recifrada(s)")
    assert "A chave antiga já pode sair" in saida
    assert nova not in saida and antiga not in saida and SEGREDO not in saida

    monkeypatch.setenv("CHAVE_COFRE", nova)
    assert cofre.ler("shopee", "partner_key") == SEGREDO
    # Com uma chave só, não há chave antiga para tirar.
    cli.main()
    assert "chave antiga" not in capsys.readouterr().out


def test_cli_com_chave_errada_sai_com_erro_claro(monkeypatch, capsys):
    import cli

    cofre.guardar("shopee", "partner_key", SEGREDO)
    errada = nova_chave()
    monkeypatch.setenv("CHAVE_COFRE", errada)
    monkeypatch.setattr(sys, "argv", ["cli.py", "cofre", "rotacionar"])

    with pytest.raises(SystemExit) as saida:
        cli.main()
    assert saida.value.code == 1
    erro = capsys.readouterr().err
    assert "Nada foi recifrado" in erro
    assert errada not in erro and SEGREDO not in erro


def test_cli_listar_e_apagar_nao_mostram_valor(monkeypatch, capsys):
    import cli

    cofre.guardar("amazon", "refresh_token", SEGREDO)
    monkeypatch.setattr(sys, "argv", ["cli.py", "cofre", "listar"])
    cli.main()
    saida = capsys.readouterr().out
    assert "amazon" in saida and "refresh_token" in saida and SEGREDO not in saida

    monkeypatch.setattr(sys, "argv", ["cli.py", "cofre", "apagar", "amazon", "refresh_token"])
    cli.main()
    assert "Apagado do cofre: amazon/refresh_token" in capsys.readouterr().out
    assert cofre.ler("amazon", "refresh_token") is None
    assert any("Credencial apagada do cofre: amazon/refresh_token" in e for e in eventos())


# ------------------------------------------- refresh token de uso único

def test_banco_travado_na_renovacao_nao_perde_o_refresh_novo(monkeypatch, sem_espera, capsys):
    from conectores import mercadolivre

    srv = ServidorDeTokens({"rt-1"})
    monkeypatch.setattr(mercadolivre, "requests", srv)
    cofre.guardar("mercadolivre", "refresh_token", "rt-1")

    outro = sqlite3.connect(config.config.db_path)
    outro.execute("BEGIN IMMEDIATE")  # outro programa com o banco aberto para escrita
    try:
        ml = mercadolivre.MercadoLivre()
        assert ml.token == "at-2"  # sem exceção: o par novo segue na memória
        assert srv.validos == {"rt-2"}
        assert cofre.so_na_memoria("mercadolivre")
        assert cofre.ler("mercadolivre", "refresh_token") == "rt-2"
        assert no_banco("mercadolivre", "refresh_token") == "rt-1"
        assert mercadolivre.MercadoLivre().token == "at-2"  # outra instância também
    finally:
        outro.rollback()
        outro.close()
    aviso = capsys.readouterr().err  # com o banco travado, o aviso vai para a saída de erro
    assert "só na memória" in aviso and "Não feche nem reinicie" in aviso
    assert "rt-2" not in aviso and "at-2" not in aviso

    # Banco livre: o próximo uso grava o par e avisa.
    assert mercadolivre.MercadoLivre().token == "at-2"
    assert not cofre.so_na_memoria("mercadolivre")
    assert no_banco("mercadolivre", "refresh_token") == "rt-2"
    assert no_banco("mercadolivre", "access_token") == "at-2"
    assert any("foram gravados no cofre" in e for e in eventos())

    # E a renovação seguinte manda o refresh novo, que o servidor aceita.
    ml = mercadolivre.MercadoLivre()
    ml._pelo_cofre(ml._renovar)
    assert srv.recebidos == ["rt-1", "rt-2"]
    assert no_banco("mercadolivre", "refresh_token") == "rt-3"
    assert not any(v in e for e in eventos() for v in ("rt-1", "rt-2", "rt-3", "at-2", "at-3"))


def test_gravacao_que_falha_depois_da_renovacao_guarda_o_token_na_memoria(monkeypatch, sem_espera):
    from conectores import shopee

    srv = ServidorDeTokens({"sp-rt-1"}, prefixo="sp-")
    monkeypatch.setattr(shopee, "requests", srv)
    cofre.guardar("shopee", "refresh_token", "sp-rt-1")
    gravar_de_verdade = cofre._para_gravar

    def chave_ilegivel(*_a, **_k):  # .chave_cofre aberto num editor na hora da gravação
        raise cofre.ErroCofre(".chave_cofre está vazio.")

    monkeypatch.setattr(cofre, "_para_gravar", chave_ilegivel)

    s = shopee.Shopee()
    assert s.pedidos_recentes() == []
    assert srv.recebidos == ["sp-rt-1"] and srv.validos == {"sp-rt-2"}
    assert s._access_token == "sp-at-2"
    assert cofre.ler("shopee", "refresh_token") == "sp-rt-2"
    assert no_banco("shopee", "refresh_token") == "sp-rt-1"
    avisos = [e for e in eventos() if "só na memória" in e]
    assert len(avisos) == 1 and avisos[0].startswith("atencao cofre")

    # Nova renovação, ainda sem conseguir gravar: usa o refresh da memória e não repete o aviso.
    s2 = shopee.Shopee()
    s2._pelo_cofre(s2._renovar)
    assert srv.recebidos == ["sp-rt-1", "sp-rt-2"]
    assert len([e for e in eventos() if "só na memória" in e]) == 1

    # A chave volta: o próximo uso grava o par mais novo.
    monkeypatch.setattr(cofre, "_para_gravar", gravar_de_verdade)
    assert shopee.Shopee().token == "sp-at-3"
    assert no_banco("shopee", "refresh_token") == "sp-rt-3"
    assert not cofre.so_na_memoria("shopee")
    assert not any(v in e for e in eventos() for v in ("sp-rt-2", "sp-rt-3", "sp-at-2", "sp-at-3"))


def test_resposta_sem_refresh_nao_descarta_o_refresh_que_so_esta_na_memoria(monkeypatch, sem_espera):
    from conectores import mercadolivre

    cofre.guardar("mercadolivre", "refresh_token", "rt-1")
    gravar_de_verdade = cofre._para_gravar

    def chave_ilegivel(*_a, **_k):
        raise cofre.ErroCofre(".chave_cofre está vazio.")

    monkeypatch.setattr(cofre, "_para_gravar", chave_ilegivel)
    mercadolivre.guardar_tokens({"access_token": "at-2", "refresh_token": "rt-2"})
    with pytest.raises(mercadolivre.ErroMercadoLivre, match="sem access_token ou sem refresh_token"):
        mercadolivre.guardar_tokens({"access_token": "at-3"})  # veio sem refresh
    assert cofre.ler("mercadolivre", "refresh_token") == "rt-2"
    assert cofre.ler("mercadolivre", "access_token") == "at-3"

    monkeypatch.setattr(cofre, "_para_gravar", gravar_de_verdade)
    cofre.regravar_pendente("mercadolivre")
    assert not cofre.so_na_memoria("mercadolivre")
    assert no_banco("mercadolivre", "refresh_token") == "rt-2"
    assert no_banco("mercadolivre", "access_token") == "at-3"


def test_renovacao_com_a_chave_do_cofre_errada_nao_gasta_o_refresh(monkeypatch):
    from conectores import mercadolivre, shopee

    antiga, nova = os.environ["CHAVE_COFRE"], nova_chave()
    cofre.guardar("anthropic", "api_key", SEGREDO)  # só abre com a antiga
    monkeypatch.setenv("CHAVE_COFRE", f"{nova},{antiga}")
    cofre.guardar("mercadolivre", "refresh_token", "rt-1")  # cifrados com a nova
    cofre.guardar("shopee", "refresh_token", "sp-rt-1")
    monkeypatch.setenv("CHAVE_COFRE", nova)  # a antiga saiu sem rotacionar
    assert cofre.ler("mercadolivre", "refresh_token") == "rt-1"
    srv_ml, srv_sp = ServidorDeTokens({"rt-1"}), ServidorDeTokens({"sp-rt-1"}, prefixo="sp-")
    monkeypatch.setattr(mercadolivre, "requests", srv_ml)
    monkeypatch.setattr(shopee, "requests", srv_sp)

    with pytest.raises(mercadolivre.ErroMercadoLivre, match="não confere"):
        mercadolivre.MercadoLivre().token
    with pytest.raises(shopee.ErroShopee, match="não confere"):
        shopee.Shopee().token
    # O refresh de uso único não foi gasto: nada saiu para o marketplace.
    assert srv_ml.recebidos == [] and srv_sp.recebidos == []
    assert cofre.ler("mercadolivre", "refresh_token") == "rt-1"


def test_duas_threads_mandam_o_refresh_uma_vez_so(monkeypatch):
    from conectores import mercadolivre

    srv = ServidorDeTokens({"rt-1"}, demora=0.3)
    monkeypatch.setattr(mercadolivre, "requests", srv)
    cofre.guardar("mercadolivre", "refresh_token", "rt-1")
    largada = threading.Barrier(2)
    resultados = []

    def usar():  # o worker e o painel, cada um com o próprio conector
        ml = mercadolivre.MercadoLivre()
        largada.wait(timeout=5)
        try:
            resultados.append(ml.token)
        except Exception as e:
            resultados.append(f"{type(e).__name__}: {e}")

    threads = [threading.Thread(target=usar) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert srv.recebidos == ["rt-1"]
    assert resultados == ["at-2", "at-2"]
    assert cofre.ler("mercadolivre", "refresh_token") == "rt-2"


def test_quem_esperou_a_vez_usa_o_token_que_o_outro_renovou(monkeypatch):
    from conectores import mercadolivre

    srv = ServidorDeTokens({"rt-2"}, prefixo="novo-")
    monkeypatch.setattr(mercadolivre, "requests", srv)
    cofre.guardar_lote("mercadolivre", {"access_token": "at-1", "refresh_token": "rt-1",
                                        "expira_em": "1"})
    ml = mercadolivre.MercadoLivre()
    ml._carregar_token()  # esta instância ficou com o at-1, vencido
    # Enquanto isso, outra thread renovou.
    cofre.guardar_lote("mercadolivre", {"access_token": "at-2", "refresh_token": "rt-2",
                                        "expira_em": str(int(time.time()) + 3600)})

    assert ml.token == "at-2"
    assert srv.recebidos == []  # o rt-1 já gasto não sai

    # Recusado o at-2 (401), aí sim renova, com o refresh que está no cofre.
    ml._pelo_cofre(ml._renovar)
    assert srv.recebidos == ["rt-2"] and ml._access_token == "novo-at-2"
    assert cofre.ler("mercadolivre", "refresh_token") == "novo-rt-2"


def test_na_shopee_quem_esperou_a_vez_usa_o_token_que_o_outro_renovou(monkeypatch):
    from conectores import shopee

    srv = ServidorDeTokens({"sp-rt-2"}, prefixo="novo-")
    monkeypatch.setattr(shopee, "requests", srv)
    cofre.guardar_lote("shopee", {"access_token": "sp-at-1", "refresh_token": "sp-rt-1",
                                  "expira_em": "1"})
    s = shopee.Shopee()
    s._carregar()  # esta instância ficou com o sp-at-1, vencido
    cofre.guardar_lote("shopee", {"access_token": "sp-at-2", "refresh_token": "sp-rt-2",
                                  "expira_em": str(int(time.time()) + 3600)})

    assert s.token == "sp-at-2"
    assert srv.recebidos == []
    s._pelo_cofre(s._renovar)
    assert srv.recebidos == ["sp-rt-2"] and s._access_token == "novo-at-2"


# Outro processo que trava o arquivo de trava de um provedor, como o cofre faz.
# "segurar": trava, avisa e espera uma linha na entrada; "tentar": diz se conseguiu.
OUTRO_PROCESSO = textwrap.dedent("""
    import os, sys
    fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, 0, os.SEEK_SET)
    except OSError:
        print("ocupado", flush=True)
        sys.exit(0)
    print("travado", flush=True)
    if sys.argv[2] == "segurar":
        sys.stdin.readline()
""")


def tentar_travar_em_outro_processo(provedor: str) -> str:
    feito = subprocess.run(
        [sys.executable, "-c", OUTRO_PROCESSO, str(cofre._arquivo_trava(provedor)), "tentar"],
        capture_output=True, text=True, timeout=60)
    return feito.stdout.strip()


def test_renovacao_espera_a_de_outro_processo_e_nao_manda_o_refresh(monkeypatch):
    from conectores import mercadolivre

    monkeypatch.setattr(cofre, "ESPERA_TRAVA", 0.5)
    srv = ServidorDeTokens({"rt-1"})
    monkeypatch.setattr(mercadolivre, "requests", srv)
    cofre.guardar("mercadolivre", "refresh_token", "rt-1")
    outro = subprocess.Popen(
        [sys.executable, "-c", OUTRO_PROCESSO, str(cofre._arquivo_trava("mercadolivre")),
         "segurar"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert outro.stdout.readline().strip() == "travado"
        with pytest.raises(mercadolivre.ErroMercadoLivre, match="Outro processo está renovando"):
            mercadolivre.MercadoLivre().token
        assert srv.recebidos == []
        # A trava é por provedor: a Shopee não espera o ML.
        with cofre.trava_de_tokens("shopee"):
            pass
    finally:
        outro.communicate(input="\n", timeout=30)

    assert mercadolivre.MercadoLivre().token == "at-2"
    assert srv.recebidos == ["rt-1"]


def test_soltar_a_trava_de_um_provedor_nao_solta_a_de_outro(monkeypatch):
    # No Linux e no macOS, fechar um descritor solta todas as travas que o
    # processo tem naquele arquivo: por isso cada provedor tem o seu.
    monkeypatch.setattr(cofre, "ESPERA_TRAVA", 0.5)
    with cofre.trava_de_tokens("mercadolivre"):
        with cofre.trava_de_tokens("shopee"):
            pass
        assert tentar_travar_em_outro_processo("mercadolivre") == "ocupado"
        with cofre.trava_de_tokens("mercadolivre"):  # reentrante na mesma thread
            pass
        assert tentar_travar_em_outro_processo("mercadolivre") == "ocupado"
    assert tentar_travar_em_outro_processo("mercadolivre") == "travado"
    assert tentar_travar_em_outro_processo("shopee") == "travado"


def test_importacao_atrasada_do_legado_nao_cobre_o_token_renovado(monkeypatch):
    from conectores import mercadolivre

    antes = escrever_legado(mercadolivre.ARQ_TOKEN, access_token="at-leg", refresh_token="rt-leg",
                            expira_em=int(time.time()) + 999)
    # Outra thread já importou e renovou...
    cofre.guardar_lote("mercadolivre", {"access_token": "at-2", "refresh_token": "rt-2"})
    # ...depois que este importador viu o cofre vazio.
    monkeypatch.setattr(cofre, "tem_tokens", lambda provedor: False)

    assert cofre.importar_legado("mercadolivre", mercadolivre.ARQ_TOKEN) is False
    assert no_banco("mercadolivre", "refresh_token") == "rt-2"
    assert no_banco("mercadolivre", "access_token") == "at-2"
    assert no_banco("mercadolivre", "expira_em") is None  # nem a linha que faltava entra
    assert not any("importados" in e for e in eventos())
    assert impressao(mercadolivre.ARQ_TOKEN) == antes


def test_depois_da_importacao_o_legado_nao_volta_no_ml(monkeypatch):
    from conectores import mercadolivre

    antes = escrever_legado(mercadolivre.ARQ_TOKEN, access_token="at-leg", refresh_token="rt-leg",
                            expira_em=0)
    srv = ServidorDeTokens({"rt-leg"})
    monkeypatch.setattr(mercadolivre, "requests", srv)

    ml = mercadolivre.MercadoLivre()
    assert ml.token == "at-2"  # importou e renovou com o refresh do arquivo
    ml._pelo_cofre(ml._renovar)  # segunda renovação, com o arquivo ainda no disco
    assert srv.recebidos == ["rt-leg", "rt-2"]
    assert mercadolivre.MercadoLivre().token == "at-3"  # do cofre, sem outro POST
    assert len(srv.recebidos) == 2
    assert impressao(mercadolivre.ARQ_TOKEN) == antes

    # Depois da importação o arquivo nem é mais lido: estragado à mão, não muda nada.
    mercadolivre.ARQ_TOKEN.write_text("{isto não é json", encoding="utf-8")
    assert mercadolivre.MercadoLivre().token == "at-3"
    outro = mercadolivre.MercadoLivre()
    outro._pelo_cofre(outro._renovar)
    assert srv.recebidos == ["rt-leg", "rt-2", "rt-3"]
    assert not any("ilegível" in e for e in eventos())


def test_depois_da_importacao_o_legado_nao_volta_na_shopee(monkeypatch):
    from conectores import shopee

    antes = escrever_legado(shopee.ARQ_TOKEN, access_token="at-velho-sp",
                            refresh_token="rt-velho-sp", expira_em=0)
    srv = ServidorDeTokens({"rt-velho-sp"}, prefixo="sp-")
    monkeypatch.setattr(shopee, "requests", srv)

    s = shopee.Shopee()
    assert s.token == "sp-at-2"
    s._pelo_cofre(s._renovar)
    assert srv.recebidos == ["rt-velho-sp", "sp-rt-2"]
    assert shopee.Shopee().token == "sp-at-3"
    assert len(srv.recebidos) == 2
    assert impressao(shopee.ARQ_TOKEN) == antes

    shopee.ARQ_TOKEN.write_text("{isto não é json", encoding="utf-8")
    outra = shopee.Shopee()
    outra._pelo_cofre(outra._renovar)
    assert srv.recebidos == ["rt-velho-sp", "sp-rt-2", "sp-rt-3"]
    assert not any("ilegível" in e for e in eventos())


# -------------------------------------------- valor preso à própria linha

def test_valor_copiado_para_outra_linha_e_recusado_sem_aparecer(monkeypatch, sessao):
    import worker
    from conectores import mercadolivre

    cliente, csrf = sessao
    chave_api = "sk-ant-SEGREDO-REAL-42"
    cofre.guardar("anthropic", "api_key", chave_api)
    cofre.guardar_lote("mercadolivre", {"access_token": "at-x", "refresh_token": "rt-x",
                                        "expira_em": "0"})
    monkeypatch.setattr(mercadolivre, "requests", sem_http())
    copiar_cifrado(("anthropic", "api_key"), ("mercadolivre", "expira_em"))

    with pytest.raises(cofre.ErroCofre, match="pertence a outra linha") as erro:
        cofre.ler("mercadolivre", "expira_em")
    assert chave_api not in str(erro.value)
    assert erro.value.__context__ is None and erro.value.__cause__ is None

    resumo = worker.ciclo()
    r = cliente.post("/api/configuracao/testar/mercadolivre", headers=cabecalho(csrf))
    assert r.status_code == 200 and r.json()["ok"] is False
    assert "pertence a outra linha" in r.json()["detalhe"]
    for texto in (json.dumps(resumo, ensure_ascii=False), r.text, *eventos()):
        assert chave_api not in texto

    # O refresh token copiado para o lugar da chave da API também é recusado.
    copiar_cifrado(("mercadolivre", "refresh_token"), ("anthropic", "api_key"))
    with pytest.raises(cofre.ErroCofre, match="pertence a outra linha") as erro:
        cofre.segredo("ANTHROPIC_API_KEY")
    assert "rt-x" not in str(erro.value)


def test_validade_que_nao_e_numero_nao_repete_o_valor(monkeypatch, sessao):
    import worker
    from conectores import mercadolivre, shopee

    cliente, csrf = sessao
    estranho = "valor-nao-numerico-77"
    for provedor in ("mercadolivre", "shopee"):
        cofre.guardar_lote(provedor, {"access_token": "at-x", "refresh_token": "rt-x",
                                      "expira_em": estranho})
    monkeypatch.setattr(mercadolivre, "requests", sem_http())
    monkeypatch.setattr(shopee, "requests", sem_http())

    with pytest.raises(cofre.ErroCofre, match="não é um número") as erro:
        cofre.ler_validade("mercadolivre")
    assert estranho not in str(erro.value) and erro.value.__context__ is None
    with pytest.raises(shopee.ErroShopee, match="não é um número") as erro:
        shopee.Shopee().token
    assert estranho not in str(erro.value)

    worker.ciclo()
    r = cliente.post("/api/configuracao/testar/mercadolivre", headers=cabecalho(csrf))
    assert "não é um número" in r.json()["detalhe"]
    for texto in (r.text, *eventos()):
        assert estranho not in texto


def test_erro_inesperado_chega_so_com_o_tipo(monkeypatch, sessao):
    import worker
    from conectores import mercadolivre

    cliente, csrf = sessao
    # Montado em tempo de execução: a pilha mostra linhas de código, e o valor
    # não pode estar escrito na linha do raise, como no int() de verdade.
    oculto = "-".join(["valor", "secreto", "no", "erro"])

    def explodir(*_a, **_k):
        raise ValueError(oculto)

    monkeypatch.setattr(mercadolivre.MercadoLivre, "pedidos_recentes", explodir)
    monkeypatch.setattr(worker, "analisar_novos", explodir)

    r = cliente.post("/api/configuracao/testar/mercadolivre", headers=cabecalho(csrf))
    assert r.json() == {"ok": False, "detalhe": "Não foi possível testar a conexão (ValueError)."}
    resumo = worker.ciclo()
    assert resumo["ingeridos"] == resumo["analisados"] == "erro: ValueError"
    falhas = [e for e in eventos() if "Etapa 'analisados' falhou: ValueError" in e]
    assert len(falhas) == 1 and "worker.py" in falhas[0]  # a pilha fica, sem a mensagem
    for texto in (r.text, json.dumps(resumo), *eventos()):
        assert oculto not in texto


def test_valor_do_formato_anterior_so_abre_depois_de_rotacionar():
    cofre.guardar("amazon", "lwa_client_secret", "outro-valor")
    # Como uma versão anterior gravava: o valor cifrado sozinho.
    anterior = Fernet(os.environ["CHAVE_COFRE"].encode()).encrypt(SEGREDO.encode()).decode()
    with conectar() as conn:
        conn.execute("INSERT INTO credenciais VALUES ('shopee', 'partner_key', ?, ?)",
                     (anterior, "2026-09-30T00:00:00+00:00"))

    with pytest.raises(cofre.ErroCofre, match="cofre rotacionar") as erro:
        cofre.ler("shopee", "partner_key")
    assert SEGREDO not in str(erro.value)

    assert cofre.rotacionar() == 2
    assert cofre.ler("shopee", "partner_key") == SEGREDO
    assert no_banco("shopee", "partner_key") == SEGREDO  # agora preso à própria linha
    assert cofre.ler("amazon", "lwa_client_secret") == "outro-valor"
    assert any("formato anterior" in e for e in eventos())


def test_rotacionar_recusa_valor_de_outra_linha_e_nao_muda_nada():
    cofre.guardar("anthropic", "api_key", SEGREDO)
    cofre.guardar("shopee", "partner_key", "pk-qualquer")
    copiar_cifrado(("anthropic", "api_key"), ("shopee", "partner_key"))
    antes = cifrados()

    with pytest.raises(cofre.ErroCofre, match="Nada foi recifrado") as erro:
        cofre.rotacionar()
    assert "outra linha" in str(erro.value) and SEGREDO not in str(erro.value)
    assert cifrados() == antes


# ------------------------------------------------- migração e avisos

def test_migrar_nao_troca_o_valor_mais_novo_do_cofre(isolamento):
    cofre.guardar_segredo("ML_REFRESH_TOKEN", "rt-mais-novo")
    cofre.guardar_segredo("ANTHROPIC_API_KEY", "sk-do-cofre")
    env = isolamento / ".env"
    env.write_text("ML_REFRESH_TOKEN=rt-velho-do-env\nANTHROPIC_API_KEY=sk-velha-do-env\n",
                   encoding="utf-8")

    rel = cofre.migrar(env, {})

    assert rel.diferentes == ["ML_REFRESH_TOKEN", "ANTHROPIC_API_KEY"]
    assert rel.copiados == []
    assert cofre.ler("mercadolivre", "refresh_token") == "rt-mais-novo"
    assert cofre.ler("anthropic", "api_key") == "sk-do-cofre"


def test_migrar_nao_manda_apagar_arquivo_que_nao_foi_importado(isolamento):
    from conectores import mercadolivre

    mercadolivre.ARQ_TOKEN.write_text("{isto não é json", encoding="utf-8")
    antes = impressao(mercadolivre.ARQ_TOKEN)

    rel = cofre.migrar(isolamento / "nao-existe.env", {"mercadolivre": mercadolivre.ARQ_TOKEN})

    assert rel.arquivos == []
    assert impressao(mercadolivre.ARQ_TOKEN) == antes


def test_troca_do_codigo_da_shopee_nao_gasta_o_codigo_com_o_cofre_quebrado(monkeypatch):
    from conectores import shopee
    from painel import configurar

    chamadas = []
    monkeypatch.setattr(shopee, "requests", types.SimpleNamespace(
        post=lambda *a, **k: chamadas.append(a), request=lambda *a, **k: chamadas.append(a)))
    cofre.guardar("shopee", "refresh_token", "rt-antigo")
    monkeypatch.setenv("CHAVE_COFRE", nova_chave())

    with pytest.raises(cofre.ErroCofre):
        configurar.shopee_trocar_code("CODIGO-DE-USO-UNICO", "2000002")
    assert chamadas == []


def test_aviso_de_importacao_nao_traz_nem_pedaco_do_token():
    from conectores import mercadolivre

    tokens = {"access_token": secrets.token_hex(24).upper(),
              "refresh_token": secrets.token_hex(24).upper()}
    escrever_legado(mercadolivre.ARQ_TOKEN, **tokens, expira_em=int(time.time()) + 3600)

    assert cofre.importar_legado("mercadolivre", mercadolivre.ARQ_TOKEN) is True
    registrados = eventos()
    assert any("importados" in e for e in registrados)
    for token in tokens.values():
        for i in range(len(token) - 5):
            assert not any(token[i:i + 6] in e for e in registrados), token[i:i + 6]


def test_legado_ilegivel_nao_leva_o_conteudo_para_o_evento():
    from conectores import mercadolivre

    mercadolivre.ARQ_TOKEN.write_text(
        '{"access_token": "at-truncado-segredo-81", "refresh_token": "rt-truncado-segredo-82", "exp',
        encoding="utf-8")

    assert cofre.importar_legado("mercadolivre", mercadolivre.ARQ_TOKEN) is False
    assert any("ilegível" in e for e in eventos())
    assert not any("truncado-segredo" in e for e in eventos())


def test_avisos_do_salvar_levam_so_nomes(sessao, isolamento):
    cliente, csrf = sessao
    (isolamento / ".env").write_text("ANTHROPIC_API_KEY=sk-antigo-no-env\n", encoding="utf-8")
    novos = {"ANTHROPIC_API_KEY": "sk-novo-do-painel", "ML_CLIENT_SECRET": "cs-novo-do-painel"}

    r = cliente.post("/api/configuracao/salvar", json=novos, headers=cabecalho(csrf))

    assert r.status_code == 200
    aviso = r.json()["aviso"]
    assert "ANTHROPIC_API_KEY" in aviso  # linha antiga no .env
    assert "ML_CLIENT_SECRET" in aviso  # também no ambiente do sistema (conftest)
    for valor in (*novos.values(), "sk-antigo-no-env", os.environ["ML_CLIENT_SECRET"]):
        assert valor not in r.text


# ------------------------------------------------ chave errada ou perdida

def test_conferir_com_chave_errada_nao_vaza_a_chave(monkeypatch):
    cofre.guardar("shopee", "partner_key", SEGREDO)
    errada = nova_chave()
    monkeypatch.setenv("CHAVE_COFRE", errada)

    with pytest.raises(cofre.ErroCofre, match="não confere") as erro:
        cofre.conferir()
    mensagem = str(erro.value)
    assert errada not in mensagem and SEGREDO not in mensagem
    assert erro.value.__cause__ is None and erro.value.__suppress_context__


def test_apagar_revoga_com_a_chave_errada(monkeypatch):
    cofre.guardar("amazon", "refresh_token", SEGREDO)
    monkeypatch.setenv("CHAVE_COFRE", nova_chave())

    assert cofre.apagar("amazon", "refresh_token") is True
    assert cofre.listar() == []
    assert not cofre.ARQ_CHAVE.exists()


def test_apagar_revoga_com_a_chave_perdida(monkeypatch):
    cofre.guardar("amazon", "refresh_token", SEGREDO)
    monkeypatch.delenv("CHAVE_COFRE")

    assert cofre.apagar("amazon", "refresh_token") is True
    assert cofre.listar() == []
    assert not cofre.ARQ_CHAVE.exists()


def test_mensagens_de_chave_errada_ou_ausente_apontam_a_saida(monkeypatch):
    cofre.guardar("amazon", "refresh_token", SEGREDO)
    monkeypatch.setenv("CHAVE_COFRE", nova_chave())
    for tentativa in (lambda: cofre.guardar("amazon", "lwa_client_secret", "x"), cofre.conferir):
        with pytest.raises(cofre.ErroCofre, match="não confere") as erro:
            tentativa()
        assert "cofre apagar --tudo" in str(erro.value)

    monkeypatch.delenv("CHAVE_COFRE")
    for tentativa in (lambda: cofre.guardar("amazon", "lwa_client_secret", "x"), cofre.conferir):
        with pytest.raises(cofre.ErroCofre, match="Chave do cofre ausente") as erro:
            tentativa()
        assert "cofre apagar --tudo" in str(erro.value)


def test_chave_perdida_sai_com_apagar_tudo(monkeypatch, capsys):
    import cli

    cofre.guardar("mercadolivre", "client_secret", SEGREDO)
    cofre.guardar("anthropic", "api_key", "sk-antiga")
    monkeypatch.delenv("CHAVE_COFRE")  # chave perdida, sem backup
    with pytest.raises(cofre.ErroCofre):
        cofre.guardar("anthropic", "api_key", "sk-nova")

    monkeypatch.setattr("builtins.input", lambda _pergunta="": "APAGAR")
    monkeypatch.setattr(sys, "argv", ["cli.py", "cofre", "apagar", "--tudo"])
    cli.main()
    saida = capsys.readouterr().out
    assert "2 credencial(is) apagada(s)" in saida
    assert SEGREDO not in saida and "sk-antiga" not in saida
    assert cofre.listar() == []
    assert not cofre.ARQ_CHAVE.exists()  # apagar não precisa de chave nem cria uma
    assert any(e.startswith("atencao cofre Cofre esvaziado") for e in eventos())

    # Volta a gravar, com uma chave nova.
    cofre.guardar("anthropic", "api_key", "sk-nova")
    assert cofre.ler("anthropic", "api_key") == "sk-nova"
    assert cofre.ARQ_CHAVE.exists()


def test_apagar_tudo_sem_confirmacao_nao_apaga(monkeypatch, capsys):
    import cli

    cofre.guardar("amazon", "refresh_token", SEGREDO)
    monkeypatch.setattr("builtins.input", lambda _pergunta="": "sim")
    monkeypatch.setattr(sys, "argv", ["cli.py", "cofre", "apagar", "--tudo"])

    cli.main()

    assert "Nada foi apagado" in capsys.readouterr().out
    assert [i["nome"] for i in cofre.listar()] == ["refresh_token"]


# --------------------------------------------------------------- repositório

def test_gitignore_cobre_a_chave_do_cofre():
    linhas = (RAIZ / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".chave_cofre" in linhas
    assert ".trava_tokens.*" in linhas
    # Qualquer chave, token antigo ou trava com o mesmo prefixo (.trava_ciclo...).
    assert {".chave_*", ".token_*", ".trava_*"} <= set(linhas)
