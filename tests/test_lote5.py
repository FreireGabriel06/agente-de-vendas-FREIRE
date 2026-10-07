"""Lote 5: bugs conhecidos.

1. Revelar o comprador não quebra com endereço em texto puro ou apagado.
2. PORTA_PAINEL e INTERVALO_WORKER do .env valem no executar.py.
3. cli.py aprovar aplica a mesma conformidade do painel.
4. MODO_SIMULACAO tem efeito: os executores só registram o que fariam.
5. Claude API pelo SDK oficial, com as regras do claude-opus-5-5 (cliente falso,
   sem rede).
6. Textos honestos: a compra aprovada só grava a ordem; o teto só rotula.
7. Conformidade falha fechada: dado que falta bloqueia pedindo confirmação.
8. Cada pergunta vai ao modelo uma vez, sem texto do comprador no registro.
9. Confirmações digitadas à mão e regras sem teste.
10. LGPD: exportação e rastreio.
11. A resposta cuja publicação falhou volta para a fila; um ciclo de cada vez.
"""
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import anthropic
import httpx2
import pytest
from cryptography.fernet import Fernet

import config
import worker
from apoio import AnthropicFalso, USUARIO, cabecalho, resposta_claude
from core import aprovacao, cofre, conformidade, dinheiro, privacidade
from core.estados import ESTADOS_CRITICOS, Estado, historico
from db import agora, conectar

RAIZ = Path(__file__).resolve().parent.parent
CHAVE_API = "sk-ant-chave-de-teste-SECRETA-123"
PERGUNTA = "Qual a cor da alça? Meu CPF é 000.000.000-00"  # dado pessoal no prompt


def eventos() -> list[str]:
    with conectar() as conn:
        return [f"{l['mensagem']} {l['detalhe_json'] or ''}"
                for l in conn.execute("SELECT * FROM eventos ORDER BY id")]


def acessos() -> list[tuple]:
    with conectar() as conn:
        return [tuple(l) for l in conn.execute(
            "SELECT pedido_id, operacao, ator FROM acessos_pii ORDER BY id")]


def inserir_pedido(**colunas) -> int:
    dados = {"marketplace": "mercadolivre", "id_externo": "5000000001", "quantidade": 1,
             "valor_bruto": 50.0, "valor_bruto_moeda": "BRL", "estado": Estado.NOVO.value,
             "criado_em": agora(), "atualizado_em": agora()}
    dados.update(colunas)
    with conectar() as conn:
        cur = conn.execute(f"INSERT INTO pedidos ({', '.join(dados)}) VALUES "
                           f"({', '.join('?' * len(dados))})", tuple(dados.values()))
        return cur.lastrowid


def cadastrar(sku="ORG-001", canal="email", prazo=4, **produto):
    """Um fornecedor e um produto. As colunas de confirmação vêm de **produto."""
    with conectar() as c:
        fornecedor = c.execute("INSERT INTO fornecedores (nome, canal, contato, prazo_dias)"
                               " VALUES ('Fornecedor Teste', ?, 'pedidos@fornecedor.example', ?)",
                               (canal, prazo)).lastrowid
        colunas = {"sku": sku, "titulo": "Organizador", "custo_fornecedor": 18.5,
                   "custo_fornecedor_moeda": "BRL",
                   "peso_kg": 0.4, "fornecedor_id": fornecedor, "criado_em": agora(), **produto}
        return c.execute(f"INSERT INTO produtos ({', '.join(colunas)}) VALUES "
                         f"({', '.join('?' * len(colunas))})", tuple(colunas.values())).lastrowid


def campos_do_produto(sku="ORG-001") -> dict:
    """Os campos do produto que o worker grava no payload da compra: os ids
    (a conformidade acha produto e fornecedor por eles) e o valor, a moeda e
    a quantidade, que ela confere com o custo atual (CUSTO-ALTERADO)."""
    with conectar() as c:
        linha = c.execute("SELECT id, fornecedor_id, custo_fornecedor, custo_fornecedor_dec,"
                          " custo_fornecedor_moeda FROM produtos WHERE sku = ?", (sku,)).fetchone()
    if not linha:
        return {}
    custo = dinheiro.ler(linha["custo_fornecedor_dec"], linha["custo_fornecedor"])
    return {"produto_id": linha["id"], "fornecedor_id": linha["fornecedor_id"], "quantidade": 1,
            "valor": dinheiro.texto(custo), "moeda": linha["custo_fornecedor_moeda"]}


def compra_na_fila(marketplace="mercadolivre", sku="ORG-001", margem=30.0) -> int:
    """Uma compra pendente como o worker monta (payload sem confirmações)."""
    pedido = inserir_pedido(marketplace=marketplace, estado=Estado.AGUARDANDO_APROVACAO.value)
    return aprovacao.enfileirar("compra_fornecedor", f"[ROTINA] Comprar 1x {sku}", {
        "pedido_id": pedido, **campos_do_produto(sku), "fornecedor": "Fornecedor Teste",
        "canal": "email", "contato": "pedidos@fornecedor.example", "sku": sku,
        "produto": "Organizador", "quantidade": 1, "valor": 18.5, "marketplace": marketplace,
        "pedido_externo": "5000000001", "margem_prevista": margem,
    }, pedido_id=pedido, valor=18.5)


def erro_api(classe, status):
    pedido = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    corpo = f"corpo da resposta com {CHAVE_API}"
    if issubclass(classe, anthropic.APIConnectionError):
        return classe(request=pedido)
    return classe(corpo, response=httpx2.Response(status, request=pedido), body=None)


# =========================================================== 1. revelar PII

def test_revelar_pedido_do_demo_nao_quebra_e_registra_o_acesso(sessao):
    """Linha como a do demo.py: nome em texto puro, endereço '{}' sem cifra."""
    cliente, _ = sessao
    pedido = inserir_pedido(comprador_nome="Comprador Teste", endereco_json="{}")

    r = cliente.get(f"/api/pedido/{pedido}/comprador")

    assert r.status_code == 200
    assert r.json() == {"nome": "Comprador Teste", "identificador": None, "endereco": {},
                        "aviso": privacidade.AVISOS_PII["texto_puro"]}
    assert acessos() == [(pedido, "leitura", USUARIO)]


def test_revelar_depois_da_eliminacao_devolve_vazio_com_aviso(sessao, pedido_cifrado):
    cliente, _ = sessao
    assert privacidade.eliminar_dados("424242") == 1  # deixa endereco_json = '{}'

    r = cliente.get(f"/api/pedido/{pedido_cifrado}/comprador")

    assert r.status_code == 200
    assert r.json() == {"nome": None, "identificador": None, "endereco": {},
                        "aviso": privacidade.AVISOS_PII["vazio"]}
    assert acessos()[-1] == (pedido_cifrado, "leitura", USUARIO)


def test_revelar_depois_do_expurgo_devolve_vazio(pedido_cifrado):
    with conectar() as conn:
        conn.execute("UPDATE pedidos SET criado_em = '2000-01-01T00:00:00+00:00'")
    assert privacidade.expurgar(dias=30) == 1

    lido = privacidade.ler_comprador(pedido_cifrado, ator="cli")
    assert lido == {"nome": None, "identificador": None, "endereco": {},
                    "aviso": privacidade.AVISOS_PII["vazio"]}
    assert acessos()[-1] == (pedido_cifrado, "leitura", "cli")


def test_valores_legados_e_ilegiveis_viram_resultado_limpo():
    de_outra_chave = Fernet(Fernet.generate_key()).encrypt(b"Fulano").decode()
    legado = inserir_pedido(id_externo="1", comprador_nome=de_outra_chave,
                            endereco_json='{"shipping_id": "9"}')
    quebrado = inserir_pedido(id_externo="2", comprador_nome=privacidade.cifrar("Beltrano"),
                              endereco_json="isto não é json nem token")

    lido = privacidade.ler_comprador(legado)
    assert lido["nome"] is None and lido["endereco"] == {"shipping_id": "9"}
    assert privacidade.AVISOS_PII["ilegivel"] in lido["aviso"]
    assert privacidade.AVISOS_PII["texto_puro"] in lido["aviso"]

    lido = privacidade.ler_comprador(quebrado)
    assert lido["nome"] == "Beltrano" and lido["endereco"] == {}
    assert privacidade.AVISOS_PII["ilegivel"] in lido["aviso"]
    assert len(acessos()) == 2  # cada leitura deixou rastro


def test_pedido_cifrado_continua_sem_aviso(pedido_cifrado):
    assert "aviso" not in privacidade.ler_comprador(pedido_cifrado)


# ===================================================== 2. porta e intervalo

def ambiente_utf8(**extra) -> dict:
    """Ambiente do processo filho com a saída em UTF-8. No Windows o console
    usa cp1252, e 'não' chegaria como byte 0xe3 ao pai, que lê UTF-8."""
    return {**os.environ, "PYTHONIOENCODING": "utf-8", **extra}


@pytest.mark.parametrize("conteudo, esperado, avisos", [
    ("PORTA_PAINEL=8999\nINTERVALO_WORKER=42\n", [8999, 42], []),
    # O .env não corta comentário no fim da linha; 0 faria o laço girar sem pausa.
    ("PORTA_PAINEL=8777 # painel\nINTERVALO_WORKER=0\n", [8777, 300],
     ["PORTA_PAINEL='8777 # painel' não vale", "INTERVALO_WORKER='0' não vale"]),
    ("PORTA_PAINEL=70000\nINTERVALO_WORKER=-5\n", [8777, 300],
     ["PORTA_PAINEL='70000' não vale", "INTERVALO_WORKER='-5' não vale"]),
])
def test_porta_e_intervalo_do_env_valem_no_executar(tmp_path, conteudo, esperado, avisos):
    """Importação limpa, em outro processo, com os valores só no .env. O
    executar é importado primeiro, como no bug antigo (ele lia o ambiente
    antes de o .env ser carregado); AGENTE_DADOS aponta para tmp_path."""
    (tmp_path / ".env").write_text(conteudo, encoding="utf-8")
    ambiente = {k: v for k, v in ambiente_utf8(AGENTE_DADOS=str(tmp_path)).items()
                if k not in ("PORTA_PAINEL", "INTERVALO_WORKER")}
    script = ("import json, executar, config\n"
              "assert config.DATA_DIR != config.BASE_DIR\n"
              "print(json.dumps([executar.porta(), executar.intervalo()]))\n")
    feito = subprocess.run([sys.executable, "-c", script], cwd=RAIZ, env=ambiente,
                           capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert feito.returncode == 0, feito.stderr
    assert json.loads(feito.stdout) == esperado
    for aviso in avisos:
        assert aviso in feito.stderr
    assert ("não vale" in feito.stderr) is bool(avisos)


def test_main_sobe_o_painel_na_porta_do_config(monkeypatch, capsys):
    import uvicorn
    import executar

    monkeypatch.setattr(config.config, "porta_painel", 8999)
    subiu = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: subiu.append(kw))

    executar.main()

    assert subiu == [{"host": "127.0.0.1", "port": 8999, "log_level": "warning"}]
    saida = capsys.readouterr().out
    assert "http://127.0.0.1:8999" in saida and "Modo simulação ligado" in saida


def test_iniciadores_nao_anunciam_porta_fixa():
    """O executar.py imprime o endereço com a porta do config; o iniciar.bat e
    o iniciar.sh não podem anunciar outra."""
    for nome in ("iniciar.bat", "iniciar.sh"):
        texto = (RAIZ / nome).read_text(encoding="utf-8")
        assert "127.0.0.1:8777" not in texto, nome
        assert "PORTA_PAINEL" in texto, nome


def test_laco_do_worker_espera_o_intervalo_do_config(monkeypatch):
    import executar

    class Parar(Exception):
        pass

    esperas = []

    def dormir(segundos):
        esperas.append(segundos)
        if len(esperas) == 2:
            raise Parar

    monkeypatch.setattr(config.config, "intervalo_worker", 42)
    monkeypatch.setattr(executar.time, "sleep", dormir)
    monkeypatch.setattr(worker, "ciclo", lambda: {})
    with pytest.raises(Parar):
        executar._laco_worker()
    assert esperas == [5, 42]


def test_rodar_do_worker_usa_o_intervalo_do_config(monkeypatch):
    class Parar(Exception):
        pass

    esperas = []

    def dormir(segundos):
        esperas.append(segundos)
        raise Parar

    monkeypatch.setattr(config.config, "intervalo_worker", 42)
    monkeypatch.setattr(worker.time, "sleep", dormir)
    monkeypatch.setattr(worker, "ciclo", lambda: {})
    with pytest.raises(Parar):
        worker.rodar()
    assert esperas == [42]


def test_cli_rodar_sem_intervalo_usa_o_do_config(monkeypatch):
    """`python cli.py rodar` sem --intervalo vale INTERVALO_WORKER. Parar é
    BaseException porque o cli.main captura Exception."""
    import cli

    class Parar(BaseException):
        pass

    esperas = []

    def dormir(segundos):
        esperas.append(segundos)
        raise Parar

    monkeypatch.setattr(config.config, "intervalo_worker", 42)
    monkeypatch.setattr(worker.time, "sleep", dormir)
    monkeypatch.setattr(worker, "ciclo", lambda: {})
    monkeypatch.setattr(sys, "argv", ["cli.py", "rodar"])
    with pytest.raises(Parar):
        cli.main()
    assert esperas == [42]


# ============================================= 3. cli.py aprovar = painel

def test_cli_aprovar_recusa_o_bloqueado_com_o_motivo_do_painel(sessao, fila_de_compras,
                                                                 monkeypatch, capsys):
    import cli

    cliente, csrf = sessao
    alvo = fila_de_compras[0]
    # A confirmação da nota fiscal sai depois que a compra já está na fila:
    # a checagem lê a configuração na hora e volta a bloquear.
    monkeypatch.setattr(config.config.negocio, "emite_nota", None)

    r = cliente.post(f"/api/aprovar/{alvo}", headers=cabecalho(csrf))
    assert r.status_code == 409
    motivo = r.json()["detail"]
    assert motivo == "Bloqueado pela conformidade: Falta confirmar a emissão de nota fiscal nas vendas."

    monkeypatch.setattr(sys, "argv", ["cli.py", "aprovar", str(alvo)])
    cli.main()
    saida = capsys.readouterr().out
    assert f"✗ {alvo} não executada. {motivo}" in saida
    assert "Saída (FISCAL-NF): Se você emite nota fiscal em toda venda, defina " \
           "EMITE_NOTA_FISCAL=true" in saida

    # Nada foi executado: a pendência continua, sem ordem de compra.
    assert aprovacao.obter_pendente(alvo) is not None
    assert not worker.PASTA_ORDENS.exists()

    # Com a confirmação de volta, o mesmo comando aprova.
    monkeypatch.setattr(config.config.negocio, "emite_nota", True)
    cli.main()
    saida = capsys.readouterr().out
    assert f"✓ {alvo} simulada: [SIMULAÇÃO] Ordem de compra gravada" in saida
    assert aprovacao.obter_pendente(alvo) is None


def test_cli_pendencias_mostra_o_bloqueio_e_a_saida(fila_de_compras, monkeypatch, capsys):
    import cli

    monkeypatch.setattr(config.config.negocio, "emite_nota", None)
    monkeypatch.setattr(sys, "argv", ["cli.py", "pendencias"])
    cli.main()
    saida = capsys.readouterr().out
    assert "Modo simulação ligado" in saida
    assert saida.count("Bloqueado pela conformidade: Falta confirmar a emissão de nota fiscal") == 2
    assert "2 bloqueada(s) pela conformidade" in saida
    assert "Exposição se você aprovar todas as liberadas: R$ 0.00" in saida


# ========================================================= 4. simulação

@pytest.mark.parametrize("valor, ligado", [
    (None, True), ("", True), ("true", True), ("talvez", True), ("1", True),
    ("false", False), (" FALSE ", False), ("0", False), ("nao", False), ("off", False),
])
def test_modo_simulacao_so_desliga_com_um_nao_explicito(monkeypatch, valor, ligado):
    if valor is None:
        monkeypatch.delenv("MODO_SIMULACAO", raising=False)
    else:
        monkeypatch.setenv("MODO_SIMULACAO", valor)
    assert config._modo_simulacao() is ligado


def test_simulacao_e_o_padrao_nos_testes():
    assert config.config.modo_simulacao is True


class MercadoLivreVigiado:
    chamadas: list = []

    def __init__(self, *a, **k):
        MercadoLivreVigiado.chamadas.append("criado")

    def responder_pergunta(self, question_id, texto):
        MercadoLivreVigiado.chamadas.append(("resposta", question_id, texto))

    def atualizar_preco(self, item_id, preco):
        MercadoLivreVigiado.chamadas.append(("preco", item_id, preco))


@pytest.fixture
def marketplace_vigiado(monkeypatch):
    MercadoLivreVigiado.chamadas = []
    monkeypatch.setattr(worker, "MercadoLivre", MercadoLivreVigiado)
    return MercadoLivreVigiado.chamadas


def _enfileirar_resposta_e_preco():
    resposta = aprovacao.enfileirar("resposta_cliente", "Responder pergunta 77",
                                    {"question_id": "77", "pergunta": "cor?", "resposta": "Azul."})
    preco = aprovacao.enfileirar("ajuste_preco", "Preço do MLB1",
                                 {"item_id": "MLB1", "preco": 59.9, "margem_prevista": 22.0})
    return resposta, preco


def test_simulacao_nao_publica_resposta_nem_muda_preco(marketplace_vigiado):
    resposta, preco = _enfileirar_resposta_e_preco()

    r1 = aprovacao.aprovar(resposta, worker.EXECUTORES)
    r2 = aprovacao.aprovar(preco, worker.EXECUTORES)

    assert marketplace_vigiado == []  # nem o conector foi criado
    assert aprovacao.foi_simulado(r1) and aprovacao.foi_simulado(r2)
    assert "nada foi enviado ao marketplace" in r1 and "seria R$ 59.90" in r2
    with conectar() as conn:
        gravados = [tuple(l) for l in conn.execute(
            "SELECT status, resultado FROM aprovacoes ORDER BY id")]
    assert gravados == [("executada", r1), ("executada", r2)]
    assert f"Ação {resposta} (resposta_cliente) simulada: nada saiu do sistema" in "\n".join(eventos())


def test_sem_simulacao_os_executores_chamam_o_marketplace(monkeypatch, marketplace_vigiado):
    monkeypatch.setattr(config.config, "modo_simulacao", False)
    resposta, preco = _enfileirar_resposta_e_preco()

    r1 = aprovacao.aprovar(resposta, worker.EXECUTORES)
    r2 = aprovacao.aprovar(preco, worker.EXECUTORES)

    assert ("resposta", "77", "Azul.") in marketplace_vigiado
    assert ("preco", "MLB1", 59.9) in marketplace_vigiado
    assert not aprovacao.foi_simulado(r1) and not aprovacao.foi_simulado(r2)


def estado_do_pedido(pedido_id: int) -> str:
    with conectar() as conn:
        return conn.execute("SELECT estado FROM pedidos WHERE id = ?", (pedido_id,)).fetchone()[0]


def test_compra_simulada_marca_o_arquivo_e_nao_consome_o_pedido(sessao, fila_de_compras):
    """O pedido pode ser real (Mercado Livre conectado): a aprovação simulada
    não o tira dos estados que pedem atenção."""
    cliente, csrf = sessao
    alvo = fila_de_compras[0]

    r = cliente.post(f"/api/aprovar/{alvo}", headers=cabecalho(csrf))

    assert r.status_code == 200
    corpo = r.json()
    assert corpo["simulado"] is True and corpo["resultado"].startswith("[SIMULAÇÃO] ")
    assert "volta para a fila quando MODO_SIMULACAO for desligado" in corpo["resultado"]
    item = aprovacao.pendentes()  # sobrou só a segunda
    with conectar() as conn:
        pedido = conn.execute("SELECT pedido_id FROM aprovacoes WHERE id = ?", (alvo,)).fetchone()[0]
    texto = (worker.PASTA_ORDENS / f"oc_{pedido}.txt").read_text(encoding="utf-8")
    assert texto.startswith(worker.AVISO_SIMULACAO_OC)
    assert estado_do_pedido(pedido) == "AGUARDANDO_APROVACAO"
    assert Estado.AGUARDANDO_APROVACAO in ESTADOS_CRITICOS
    assert [h["para"] for h in historico(pedido)] == ["ANALISADO", "AGUARDANDO_APROVACAO"]
    assert f"Pedido {pedido}: compra simulada" in "\n".join(eventos())
    assert len(item) == 1
    # Ainda em simulação, o worker não repõe a compra na fila.
    assert worker.montar_ordens_de_compra() == 0


def test_compra_simulada_volta_para_a_fila_quando_a_simulacao_desliga(monkeypatch, sessao,
                                                                     fila_de_compras):
    cliente, csrf = sessao
    pedidos = []
    for alvo in fila_de_compras:
        assert cliente.post(f"/api/aprovar/{alvo}", headers=cabecalho(csrf)).status_code == 200
        with conectar() as conn:
            pedidos.append(conn.execute("SELECT pedido_id FROM aprovacoes WHERE id = ?",
                                        (alvo,)).fetchone()[0])
    assert aprovacao.pendentes() == []

    monkeypatch.setattr(config.config, "modo_simulacao", False)
    assert worker.analisar_novos() == 0
    assert worker.montar_ordens_de_compra() == 2
    assert worker.montar_ordens_de_compra() == 0  # não duplica

    novas = aprovacao.pendentes()
    assert sorted((a.tipo, a.pedido_id) for a in novas) == [
        ("compra_fornecedor", pedidos[0]), ("compra_fornecedor", pedidos[1])]
    assert "\n".join(eventos()).count("voltou para a fila: a aprovação anterior foi em "
                                      "modo simulação") == 2
    assert {estado_do_pedido(p) for p in pedidos} == {"AGUARDANDO_APROVACAO"}

    # Aprovada de verdade: COMPRA_ENVIADA, e a ordem real substitui a de teste.
    real = next(a for a in novas if a.pedido_id == pedidos[0])
    corpo = cliente.post(f"/api/aprovar/{real.id}", headers=cabecalho(csrf)).json()
    assert corpo["simulado"] is False
    assert estado_do_pedido(pedidos[0]) == "COMPRA_ENVIADA"
    texto = (worker.PASTA_ORDENS / f"oc_{pedidos[0]}.txt").read_text(encoding="utf-8")
    assert texto.startswith("PEDIDO DE COMPRA") and "SIMULAÇÃO" not in texto

    # Recusada a compra de verdade, o pedido não volta sozinho.
    recusada = next(a for a in novas if a.pedido_id == pedidos[1])
    aprovacao.recusar(recusada.id, "teste")
    assert worker.montar_ordens_de_compra() == 0
    assert aprovacao.pendentes() == []


def test_compra_sem_simulacao_grava_a_ordem_e_diz_que_o_envio_e_manual(
        monkeypatch, sessao, fila_de_compras):
    monkeypatch.setattr(config.config, "modo_simulacao", False)
    cliente, csrf = sessao
    alvo = fila_de_compras[0]

    corpo = cliente.post(f"/api/aprovar/{alvo}", headers=cabecalho(csrf)).json()

    assert corpo["simulado"] is False
    assert "Envie ao fornecedor Fornecedor Teste pelo canal email: o programa não envia." \
        in corpo["resultado"]
    with conectar() as conn:
        pedido = conn.execute("SELECT pedido_id FROM aprovacoes WHERE id = ?", (alvo,)).fetchone()[0]
    texto = (worker.PASTA_ORDENS / f"oc_{pedido}.txt").read_text(encoding="utf-8")
    assert texto.startswith("PEDIDO DE COMPRA") and "SIMULAÇÃO" not in texto
    assert historico(pedido)[-1]["motivo"].endswith("o envio ao fornecedor é manual")


def test_painel_mostra_que_a_simulacao_esta_ligada(monkeypatch, sessao):
    cliente, _ = sessao
    assert cliente.get("/api/pendencias").json()["modo_simulacao"] is True
    pagina = cliente.get("/").text
    assert 'id="aviso-simulacao"' in pagina and "Modo simulação ligado." in pagina

    monkeypatch.setattr(config.config, "modo_simulacao", False)
    assert cliente.get("/api/pendencias").json()["modo_simulacao"] is False


# ========================================================== 5. Claude API

@pytest.fixture
def claude_falso(monkeypatch):
    """Chave no cofre e a classe anthropic.Anthropic trocada por um dublê."""
    from atendimento import claude_api

    cofre.guardar_segredo("ANTHROPIC_API_KEY", CHAVE_API)
    falso = AnthropicFalso(resposta_claude("Azul-marinho."))
    monkeypatch.setattr(claude_api.anthropic, "Anthropic", falso)
    return falso


def test_redigir_usa_o_sdk_com_as_regras_do_opus_5_5(claude_falso):
    from atendimento import bot
    from atendimento.persona import persona_padrao

    assert bot.redigir(PERGUNTA, {"título": "Mochila"}) == "Azul-marinho."

    assert claude_falso.criados == [{"api_key": CHAVE_API, "base_url": "https://api.anthropic.com",
                                     "timeout": 60.0, "max_retries": 2}]
    enviado = claude_falso.mensagens[0]
    assert enviado["model"] == "claude-opus-5-5"
    assert enviado["max_tokens"] == 4000
    assert enviado["output_config"] == {"effort": "low"}
    assert enviado["betas"] == ["server-side-fallback-2026-07-01"]
    assert enviado["fallbacks"] == "default"
    assert enviado["system"] == persona_padrao.system_prompt()
    assert [m["role"] for m in enviado["messages"]] == ["user"]  # sem prefill
    for proibido in ("temperature", "top_p", "top_k", "thinking", "tool_choice", "extra_body"):
        assert proibido not in enviado


def test_modelo_e_esforco_vem_da_configuracao(monkeypatch, claude_falso):
    from atendimento import bot

    monkeypatch.setattr(config.config.claude, "modelo", "claude-sonnet-5-5")
    monkeypatch.setattr(config.config.claude, "esforco", "medium")
    bot.redigir(PERGUNTA, {})
    assert claude_falso.mensagens[0]["model"] == "claude-sonnet-5-5"
    assert claude_falso.mensagens[0]["output_config"] == {"effort": "medium"}


@pytest.mark.parametrize("esforco, max_tokens, tempo", [
    ("low", 4000, 60.0), ("medium", 8000, 120.0),
    ("high", 16000, 300.0), ("xhigh", 16000, 300.0), ("max", 16000, 300.0),
])
def test_max_tokens_e_tempo_de_espera_seguem_o_esforco(monkeypatch, claude_falso, esforco,
                                                       max_tokens, tempo):
    """O raciocínio conta dentro do max_tokens e cresce com o esforço."""
    from atendimento import bot

    monkeypatch.setattr(config.config.claude, "esforco", esforco)
    assert bot.redigir(PERGUNTA, {}) == "Azul-marinho."
    assert claude_falso.mensagens[0]["max_tokens"] == max_tokens
    assert claude_falso.mensagens[0]["output_config"] == {"effort": esforco}
    assert claude_falso.criados[0]["timeout"] == tempo


def test_tempo_esgotado_tem_mensagem_propria_com_o_limite_do_esforco(monkeypatch, claude_falso):
    """APITimeoutError é subclasse de APIConnectionError: sem um ramo próprio,
    viraria 'sem conexão'."""
    from atendimento import claude_api

    monkeypatch.setattr(config.config.claude, "esforco", "high")
    claude_falso.erro = erro_api(anthropic.APITimeoutError, None)
    with pytest.raises(claude_api.ErroClaude) as erro:
        claude_api.redigir_texto("sistema", PERGUNTA, chave=CHAVE_API)
    assert "demorou mais que 300 s" in str(erro.value) and erro.value.temporaria
    assert "sem conexão" not in str(erro.value)


def test_padrao_e_claude_opus_5_5_e_o_env_troca(tmp_path):
    assert config.config.claude.modelo == "claude-opus-5-5"
    assert config.config.claude.esforco == "low"

    (tmp_path / ".env").write_text("MODELO_CLAUDE=claude-sonnet-5-5\nESFORCO_CLAUDE=turbo\n",
                                   encoding="utf-8")
    ambiente = ambiente_utf8(AGENTE_DADOS=str(tmp_path))
    script = ("import json, config\n"
              "print(json.dumps([config.config.claude.modelo, config.config.claude.esforco]))\n")
    feito = subprocess.run([sys.executable, "-c", script], cwd=RAIZ, env=ambiente,
                           capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert feito.returncode == 0, feito.stderr
    assert json.loads(feito.stdout) == ["claude-sonnet-5-5", "low"]
    assert "ESFORCO_CLAUDE='turbo' não existe" in feito.stderr


def test_so_blocos_de_texto_entram_na_resposta(claude_falso):
    import types
    from atendimento import bot

    resposta = resposta_claude("Azul.", " Também preta.")
    resposta.content.insert(0, types.SimpleNamespace(type="fallback", text="não é texto"))
    claude_falso.resposta = resposta
    assert bot.redigir(PERGUNTA, {}) == "Azul. Também preta."


@pytest.mark.parametrize("stop_reason, motivo", [
    ("refusal", "recusou"),
    ("max_tokens", "limite de tokens"),
    ("pause_turn", "stop_reason=pause_turn"),
    (None, "stop_reason=None"),
])
def test_stop_reason_que_nao_e_end_turn_escala_para_uma_pessoa(claude_falso, stop_reason, motivo):
    from atendimento import bot

    # Mesmo com texto parcial no conteúdo, nada vira resposta.
    claude_falso.resposta = resposta_claude("Texto parcial", stop_reason=stop_reason)

    feito = bot.processar_pergunta("Q1", PERGUNTA, {})

    assert feito["acao"] == "escalada" and motivo in feito["motivo"]
    assert aprovacao.pendentes() == []
    registro = "\n".join(eventos())
    assert "Pergunta Q1: ESCALAR:" in registro
    assert "000.000.000-00" not in registro and CHAVE_API not in registro


@pytest.mark.parametrize("classe, status, trecho, temporaria", [
    (anthropic.AuthenticationError, 401, "chave da API recusada", False),
    (anthropic.NotFoundError, 404, "modelo claude-opus-5-5 não encontrado", False),
    (anthropic.RateLimitError, 429, "limite de uso", True),
    (anthropic.InternalServerError, 500, "indisponível (HTTP 500)", True),
    (anthropic.APIStatusError, 529, "indisponível (HTTP 529)", True),
    (anthropic.BadRequestError, 400, "recusou o pedido (HTTP 400, tipo não informado)", False),
    (anthropic.UnprocessableEntityError, 422, "recusou o pedido (HTTP 422, tipo não informado)",
     False),
    (anthropic.APIConnectionError, None, "sem conexão", True),
    (anthropic.APITimeoutError, None, "demorou mais que 60 s", True),
])
def test_erro_da_api_escala_sem_vazar_chave_nem_prompt(claude_falso, classe, status, trecho,
                                                       temporaria):
    from atendimento import bot, claude_api

    claude_falso.erro = erro_api(classe, status)

    with pytest.raises(claude_api.ErroClaude) as erro:
        claude_api.redigir_texto("sistema", PERGUNTA, chave=CHAVE_API, max_tokens=4000)
    assert trecho in str(erro.value) and erro.value.temporaria is temporaria
    assert erro.value.__context__ is None or erro.value.__suppress_context__

    feito = bot.processar_pergunta("Q2", PERGUNTA, {})
    assert feito["acao"] == "escalada" and trecho in feito["motivo"]
    assert ("próximo ciclo" in feito["motivo"]) is temporaria
    assert aprovacao.pendentes() == []
    for texto in (feito["motivo"], *eventos()):
        assert CHAVE_API not in texto and "corpo da resposta" not in texto
        assert "000.000.000-00" not in texto


def test_teste_do_painel_usa_models_retrieve_sem_gastar_tokens(sessao, claude_falso):
    cliente, csrf = sessao

    r = cliente.post("/api/configuracao/testar/claude", headers=cabecalho(csrf))

    assert r.json() == {"ok": True, "detalhe": "Chave válida e modelo claude-opus-5-5 "
                        "disponível (esforço low). O teste não gasta tokens; cobrança e "
                        "acesso ao beta de fallback só aparecem ao redigir."}
    assert claude_falso.consultas == ["claude-opus-5-5"]
    assert claude_falso.mensagens == []  # nenhuma mensagem enviada
    assert claude_falso.criados[0]["api_key"] == CHAVE_API
    assert claude_falso.criados[0]["base_url"] == "https://api.anthropic.com"


def test_teste_do_painel_consulta_o_modelo_configurado(sessao, claude_falso, monkeypatch):
    monkeypatch.setattr(config.config.claude, "modelo", "claude-sonnet-5-5")
    cliente, csrf = sessao
    r = cliente.post("/api/configuracao/testar/claude", headers=cabecalho(csrf))
    assert claude_falso.consultas == ["claude-sonnet-5-5"]
    assert "modelo claude-sonnet-5-5 disponível" in r.json()["detalhe"]


def test_erro_da_api_cita_o_modelo_configurado(claude_falso, monkeypatch):
    from atendimento import claude_api

    monkeypatch.setattr(config.config.claude, "modelo", "claude-sonnet-5-5")
    claude_falso.erro = erro_api(anthropic.NotFoundError, 404)
    with pytest.raises(claude_api.ErroClaude, match="modelo claude-sonnet-5-5 não encontrado"):
        claude_api.redigir_texto("sistema", PERGUNTA, chave=CHAVE_API, max_tokens=4000)
    claude_falso.erro = erro_api(anthropic.PermissionDeniedError, 403)
    with pytest.raises(claude_api.ErroClaude, match="acesso ao modelo claude-sonnet-5-5"):
        claude_api.redigir_texto("sistema", PERGUNTA, chave=CHAVE_API)


@pytest.mark.parametrize("classe, status, detalhe", [
    (anthropic.AuthenticationError, 401, "Chave da API recusada pela Anthropic (401)"),
    (anthropic.NotFoundError, 404, "Modelo claude-opus-5-5 não encontrado para esta chave (404)"),
    (anthropic.PermissionDeniedError, 403, "A chave não tem acesso ao modelo claude-opus-5-5 (403)"),
    (anthropic.APIConnectionError, None, "Sem conexão com a API da Anthropic"),
])
def test_teste_do_painel_explica_a_falha(sessao, claude_falso, classe, status, detalhe):
    cliente, csrf = sessao
    claude_falso.erro = erro_api(classe, status)

    r = cliente.post("/api/configuracao/testar/claude", headers=cabecalho(csrf))

    assert r.json()["ok"] is False and r.json()["detalhe"].startswith(detalhe)
    assert CHAVE_API not in r.text and "corpo da resposta" not in r.text


def test_teste_do_painel_sem_chave(sessao, monkeypatch):
    from atendimento import claude_api

    falso = AnthropicFalso()
    monkeypatch.setattr(claude_api.anthropic, "Anthropic", falso)
    cliente, csrf = sessao
    r = cliente.post("/api/configuracao/testar/claude", headers=cabecalho(csrf))
    assert r.json() == {"ok": False, "detalhe": "Chave não preenchida."}
    assert falso.criados == []


@pytest.fixture
def sdk_de_verdade(monkeypatch):
    """O SDK anthropic de verdade, com o transporte HTTP trocado por um
    MockTransport: confere o pedido que sai do SDK (cabeçalhos e corpo), sem
    abrir conexão nenhuma. Pega o que o dublê não pega: um parâmetro que o SDK
    instalado não aceita."""
    import httpx2
    from atendimento import claude_api

    pedidos, respostas = [], []

    def tratar(req):
        pedidos.append(req)
        return respostas.pop(0)

    real = anthropic.Anthropic

    def fabrica(**kwargs):
        transporte = httpx2.MockTransport(tratar)
        return real(**kwargs, http_client=anthropic.DefaultHttpxClient(transport=transporte))

    monkeypatch.setattr(claude_api.anthropic, "Anthropic", fabrica)
    cofre.guardar_segredo("ANTHROPIC_API_KEY", CHAVE_API)
    return pedidos, respostas


def _mensagem_api(conteudo, stop_reason="end_turn", **extra):
    import httpx2

    return httpx2.Response(200, json={
        "id": "msg_teste", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": conteudo, "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5}, **extra})


def test_sdk_de_verdade_monta_o_pedido_com_as_regras_do_opus_5_5(sdk_de_verdade):
    from atendimento import bot

    pedidos, respostas = sdk_de_verdade
    respostas.append(_mensagem_api([{"type": "thinking", "thinking": "", "signature": "s"},
                                    {"type": "text", "text": "Azul-marinho."}]))

    assert bot.redigir(PERGUNTA, {"título": "Mochila"}) == "Azul-marinho."

    pedido = pedidos[0]
    assert (pedido.method, pedido.url.path) == ("POST", "/v1/messages")
    assert pedido.headers["x-api-key"] == CHAVE_API
    assert pedido.headers["anthropic-beta"] == "server-side-fallback-2026-07-01"
    corpo = json.loads(pedido.content)
    assert corpo["model"] == "claude-opus-5-5"
    assert corpo["fallbacks"] == "default"
    assert corpo["output_config"] == {"effort": "low"}
    assert corpo["max_tokens"] >= 4000
    assert [m["role"] for m in corpo["messages"]] == ["user"]
    for proibido in ("temperature", "top_p", "top_k", "thinking", "tool_choice"):
        assert proibido not in corpo


def test_sdk_de_verdade_recusa_vira_escalonamento(sdk_de_verdade):
    from atendimento import bot

    pedidos, respostas = sdk_de_verdade
    respostas.append(_mensagem_api([], stop_reason="refusal",
                                   stop_details={"type": "refusal", "category": None,
                                                 "explanation": None}))

    feito = bot.processar_pergunta("Q9", PERGUNTA, {})

    assert feito["acao"] == "escalada" and "recusou" in feito["motivo"]
    assert aprovacao.pendentes() == []
    assert len(pedidos) == 1


def test_sdk_de_verdade_teste_de_conexao_so_consulta_o_modelo(sdk_de_verdade):
    import httpx2
    from atendimento import claude_api

    pedidos, respostas = sdk_de_verdade
    respostas.append(httpx2.Response(200, json={
        "type": "model", "id": "claude-opus-5-5", "display_name": "Claude Opus 5.5",
        "created_at": "2026-09-01T00:00:00Z"}))

    assert claude_api.testar_conexao()["ok"] is True
    assert [(p.method, p.url.path) for p in pedidos] == [("GET", "/v1/models/claude-opus-5-5")]

    respostas.append(httpx2.Response(401, json={
        "type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}))
    falha = claude_api.testar_conexao()
    assert falha == {"ok": False, "detalhe": "Chave da API recusada pela Anthropic (401); "
                                             "confira ANTHROPIC_API_KEY no painel."}
    assert len(pedidos) == 2  # 401 não é repetido


def test_sdk_de_verdade_ignora_anthropic_base_url_do_ambiente(sdk_de_verdade, monkeypatch):
    """A chave do cofre só vai para a API da Anthropic, mesmo com
    ANTHROPIC_BASE_URL no ambiente de quem inicia o programa."""
    from atendimento import bot

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://desvio.example")
    pedidos, respostas = sdk_de_verdade
    respostas.append(_mensagem_api([{"type": "text", "text": "Azul."}]))

    assert bot.redigir(PERGUNTA, {}) == "Azul."
    assert (pedidos[0].url.scheme, pedidos[0].url.host) == ("https", "api.anthropic.com")


def test_sdk_de_verdade_erro_400_cita_o_tipo_sem_o_corpo(sdk_de_verdade):
    from atendimento import claude_api

    pedidos, respostas = sdk_de_verdade
    respostas.append(httpx2.Response(400, json={"type": "error", "error": {
        "type": "invalid_request_error",
        "message": f"Unexpected value(s) `server-side-fallback-2026-07-01` {CHAVE_API}"}}))

    with pytest.raises(claude_api.ErroClaude) as erro:
        claude_api.redigir_texto("sistema", PERGUNTA, chave=CHAVE_API)
    texto = str(erro.value)
    assert "HTTP 400, invalid_request_error" in texto
    assert "MODELO_CLAUDE (claude-opus-5-5)" in texto and "server-side-fallback-2026-07-01" in texto
    assert "Unexpected" not in texto and CHAVE_API not in texto
    assert not erro.value.temporaria and len(pedidos) == 1  # 400 não é repetido


ITERACOES_COM_RESERVA = [
    {"type": "message", "input_tokens": 10, "output_tokens": 0,
     "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    {"type": "fallback_message", "model": "claude-opus-4-8", "input_tokens": 10,
     "output_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
]


def test_sdk_de_verdade_resposta_do_modelo_de_reserva_fica_registrada(sdk_de_verdade):
    """Com fallbacks="default" a resposta pode vir de outro modelo, cobrado
    pela tabela dele. O evento diz qual; nada da pergunta vai junto."""
    from atendimento import bot

    pedidos, respostas = sdk_de_verdade
    respostas.append(_mensagem_api(
        [{"type": "fallback", "from": {"model": "claude-opus-5-5"},
          "to": {"model": "claude-opus-4-8"}},
         {"type": "text", "text": "Azul."}],
        model="claude-opus-4-8",
        usage={"input_tokens": 10, "output_tokens": 5, "iterations": ITERACOES_COM_RESERVA}))

    feito = bot.processar_pergunta("Q5", PERGUNTA, {})

    assert feito["acao"] == "enfileirada" and feito["resposta"] == "Azul."
    registro = "\n".join(eventos())
    assert "Rascunho da pergunta Q5 redigido pelo modelo de reserva claude-opus-4-8" in registro
    assert "000.000.000-00" not in registro


@pytest.mark.parametrize("categoria, com_reserva, esperado, ausente", [
    ("cyber", True, "(refusal, categoria cyber); o modelo de reserva claude-opus-4-8 também "
                    "recusou", None),
    ("reasoning_extraction", False, "(refusal, categoria reasoning_extraction)", "reserva"),
    (None, False, "(refusal)", "reserva"),
])
def test_sdk_de_verdade_recusa_diz_a_categoria_e_so_cita_a_reserva_se_ela_rodou(
        sdk_de_verdade, categoria, com_reserva, esperado, ausente):
    from atendimento import claude_api

    pedidos, respostas = sdk_de_verdade
    uso = {"input_tokens": 10, "output_tokens": 0}
    if com_reserva:
        uso["iterations"] = ITERACOES_COM_RESERVA
    respostas.append(_mensagem_api([], stop_reason="refusal", usage=uso, stop_details={
        "type": "refusal", "category": categoria, "explanation": None}))

    with pytest.raises(claude_api.ErroClaude) as erro:
        claude_api.redigir_texto("sistema", PERGUNTA, chave=CHAVE_API)
    assert esperado in str(erro.value)
    if ausente:
        assert ausente not in str(erro.value)


def test_nenhum_codigo_chama_a_anthropic_na_mao_nem_fixa_modelo_antigo():
    fontes = [p for p in RAIZ.rglob("*.py")
              if not ({".venv", "tests", ".agents", "__pycache__"} & set(p.relative_to(RAIZ).parts))]
    assert fontes
    for fonte in fontes:
        texto = fonte.read_text(encoding="utf-8")
        assert "claude-sonnet-4-6" not in texto, fonte
        if fonte.relative_to(RAIZ).as_posix() == "atendimento/claude_api.py":
            # Só a base_url fixa, entregue ao SDK; nenhum cliente HTTP à mão.
            assert texto.count("api.anthropic.com") == 1
            assert 'URL_API = "https://api.anthropic.com"' in texto
            assert "base_url=URL_API" in texto
            assert "requests" not in texto and "httpx" not in texto
        else:
            assert "api.anthropic.com" not in texto, fonte
    requisitos = (RAIZ / "requirements.txt").read_text(encoding="utf-8").split()
    assert "anthropic>=1.11.0,<2" in requisitos


# ========================================================== 6. textos honestos

def test_confirmacao_da_compra_no_painel_diz_o_que_acontece(sessao):
    cliente, _ = sessao
    pagina = cliente.get("/").text
    assert "Isso envia a ordem ao fornecedor" not in pagina
    assert "envia nada ao fornecedor: enviar e pagar fica com você" in pagina
    assert "Nada é enviado ao fornecedor." in pagina


def test_teto_so_muda_o_rotulo_e_toda_compra_espera_aprovacao(nota_fiscal_confirmada):
    cadastrar(categoria_regulada="nenhuma")
    with conectar() as c:
        produto = c.execute("SELECT id FROM produtos").fetchone()[0]
    inserir_pedido(id_externo="9001", produto_id=produto, valor_bruto=54.90)
    inserir_pedido(id_externo="9002", produto_id=produto, valor_bruto=1100.0, quantidade=20)

    assert worker.analisar_novos() == 2
    assert worker.montar_ordens_de_compra() == 2

    resumos = sorted(a.resumo[:15] for a in aprovacao.pendentes())
    assert resumos == ["[ACIMA DO TETO]", "[ROTINA] Compra"]
    assert not worker.PASTA_ORDENS.exists()  # nada executado sem aprovação
    assert "só muda o rótulo" in worker.__doc__
    assert "toda compra espera" in config.ConfigNegocio.__doc__


# ============================================ 7. conformidade falha fechada

def test_compra_sem_dados_bloqueia_pedindo_confirmacao():
    res = conformidade.verificar_pendencia("compra_fornecedor",
                                           {"marketplace": "amazon", "sku": "NAO-EXISTE"})
    assert res.bloqueado and res.so_falta_confirmar
    assert {v.regra for v in res.bloqueios} == {
        "AMZ-DROPSHIP", "AMZ-REEMBALAGEM", "FORNECEDOR-TROCADO", "CUSTO-ALTERADO", "PRAZO",
        "CATEGORIA-RESTRITA", "MARGEM-NEGATIVA", "FISCAL-NF"}
    assert all(v.saida and v.confirmacao for v in res.bloqueios)


def test_painel_nao_presume_o_valor_que_libera(sessao, nota_fiscal_confirmada):
    """O bug antigo: pedido Amazon sem reembalagem informada passava no painel
    porque o contexto presumia reembalagem_confirmada=True."""
    cliente, csrf = sessao
    cadastrar(categoria_regulada="nenhuma")  # canal email: não despacha direto
    alvo = compra_na_fila(marketplace="amazon")

    fila = cliente.get("/api/pendencias").json()
    item = fila["itens"][0]
    assert item["bloqueado"] is True and fila["exposicao"] == 0
    assert [(v["regra"], v["confirmacao"]) for v in item["violacoes"]] == [("AMZ-REEMBALAGEM", True)]
    assert "UPDATE produtos SET reembalagem_confirmada = 1 WHERE sku = 'ORG-001';" \
        in item["violacoes"][0]["saida"]
    r = cliente.post(f"/api/aprovar/{alvo}", headers=cabecalho(csrf))
    assert r.status_code == 409 and r.json()["detail"] == item["motivo"]

    # A saída funciona para o item que já está na fila.
    with conectar() as c:
        c.execute("UPDATE produtos SET reembalagem_confirmada = 1 WHERE sku = 'ORG-001'")
    assert cliente.get("/api/pendencias").json()["itens"][0]["bloqueado"] is False
    assert cliente.post(f"/api/aprovar/{alvo}", headers=cabecalho(csrf)).status_code == 200


@pytest.mark.parametrize("categoria, habilitacao, bloqueia, confirmacao", [
    (None, None, True, True),
    ("nenhuma", None, False, None),
    ("suplemento", None, True, True),
    ("suplemento", 0, True, False),
    ("suplemento", 1, False, None),
    ("perfume", 1, True, True),  # categoria fora da lista
])
def test_categoria_regulada(nota_fiscal_confirmada, categoria, habilitacao, bloqueia, confirmacao):
    cadastrar(categoria_regulada=categoria, habilitacao_confirmada=habilitacao)
    res = conformidade.verificar_pendencia(
        "compra_fornecedor", {"marketplace": "mercadolivre", "sku": "ORG-001", "margem_prevista": 25,
                              **campos_do_produto()})
    assert res.bloqueado is bloqueia
    if bloqueia:
        assert [v.regra for v in res.bloqueios] == ["CATEGORIA-RESTRITA"]
        assert res.bloqueios[0].confirmacao is confirmacao


def test_nota_fiscal_negada_e_bloqueio_duro(monkeypatch):
    cadastrar(categoria_regulada="nenhuma")
    payload = {"marketplace": "mercadolivre", "sku": "ORG-001", "margem_prevista": 25,
               **campos_do_produto()}
    monkeypatch.setattr(config.config.negocio, "emite_nota", False)
    res = conformidade.verificar_pendencia("compra_fornecedor", payload)
    assert [(v.regra, v.confirmacao) for v in res.bloqueios] == [("FISCAL-NF", False)]


def test_worker_enfileira_bloqueado_quando_so_falta_confirmar(sessao, monkeypatch):
    """Sem EMITE_NOTA_FISCAL: a compra entra na fila bloqueada, com a saída, e
    libera quando o dono confirma."""
    cliente, csrf = sessao
    cadastrar(categoria_regulada="nenhuma")
    with conectar() as c:
        produto = c.execute("SELECT id FROM produtos").fetchone()[0]
    pedido = inserir_pedido(id_externo="9101", produto_id=produto, valor_bruto=54.90)

    assert worker.analisar_novos() == 1 and worker.montar_ordens_de_compra() == 1

    assert historico(pedido)[-1]["para"] == "AGUARDANDO_APROVACAO"
    assert "bloqueada até confirmar" in historico(pedido)[-1]["motivo"]
    alvo = aprovacao.pendentes()[0].id
    assert cliente.post(f"/api/aprovar/{alvo}", headers=cabecalho(csrf)).status_code == 409

    monkeypatch.setattr(config.config.negocio, "emite_nota", True)
    assert cliente.post(f"/api/aprovar/{alvo}", headers=cabecalho(csrf)).status_code == 200


def test_worker_manda_violacao_dura_para_problema(nota_fiscal_confirmada):
    cadastrar(canal="api", categoria_regulada="nenhuma", reembalagem_confirmada=1)
    with conectar() as c:
        produto = c.execute("SELECT id FROM produtos").fetchone()[0]
    pedido = inserir_pedido(id_externo="9201", marketplace="amazon", produto_id=produto,
                            valor_bruto=54.90)

    worker.analisar_novos()
    assert worker.montar_ordens_de_compra() == 0
    assert aprovacao.pendentes() == []
    ultimo = historico(pedido)[-1]
    assert ultimo["para"] == "PROBLEMA" and "AMZ-DROPSHIP" in ultimo["motivo"]


def test_prazo_sem_o_do_anuncio_usa_o_limite_fixo():
    base = {"tipo": "compra_fornecedor", "marketplace": "mercadolivre"}
    lento = conformidade.verificar({**base, "prazo_fornecedor_dias": 9})
    assert "PRAZO" in {v.regra for v in lento.bloqueios}
    assert not lento.so_falta_confirmar  # 9 dias reprova, não é dado faltando
    rapido = conformidade.verificar({**base, "prazo_fornecedor_dias": 5})
    assert "PRAZO" not in {v.regra for v in rapido.violacoes}
    com_anuncio = conformidade.verificar({**base, "prazo_fornecedor_dias": 9,
                                          "prazo_anuncio_dias": 12})
    assert "PRAZO" not in {v.regra for v in com_anuncio.violacoes}


def test_cada_tipo_tem_suas_regras_e_tipo_desconhecido_bloqueia():
    preco = conformidade.verificar_pendencia("ajuste_preco", {"item_id": "MLB1", "preco": 10})
    assert [(v.regra, v.confirmacao) for v in preco.bloqueios] == [("MARGEM-NEGATIVA", True)]

    resposta = conformidade.verificar_pendencia(
        "resposta_cliente", {"question_id": "1", "pergunta": "cor?", "resposta": "Azul."})
    assert not resposta.bloqueado

    outro = conformidade.verificar({"tipo": "apagar_conta"})
    assert [v.regra for v in outro.bloqueios] == ["TIPO-SEM-REGRAS"]


def test_banco_antigo_ganha_as_colunas_de_confirmacao(monkeypatch, tmp_path):
    import db

    antigo = tmp_path / "antigo.db"
    with sqlite3.connect(antigo) as conn:
        conn.execute("CREATE TABLE produtos (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                     " sku TEXT UNIQUE NOT NULL, titulo TEXT NOT NULL, categoria_ml TEXT,"
                     " custo_fornecedor REAL NOT NULL, peso_kg REAL DEFAULT 0.3,"
                     " fornecedor_id INTEGER, ativo INTEGER DEFAULT 1, criado_em TEXT NOT NULL)")
        conn.execute("INSERT INTO produtos (sku, titulo, custo_fornecedor, criado_em)"
                     " VALUES ('VELHO-1', 'Produto antigo', 10, 'ontem')")
    monkeypatch.setattr(config.config, "db_path", str(antigo))

    db.inicializar()
    db.inicializar()  # de novo: não tenta criar a coluna duas vezes

    with sqlite3.connect(antigo) as conn:
        colunas = {l[1] for l in conn.execute("PRAGMA table_info(produtos)")}
        linha = conn.execute("SELECT sku, categoria_regulada, habilitacao_confirmada,"
                             " reembalagem_confirmada FROM produtos").fetchone()
    assert {"categoria_regulada", "habilitacao_confirmada", "reembalagem_confirmada"} <= colunas
    assert linha == ("VELHO-1", None, None, None)  # entra sem confirmação


def test_demo_sem_confirmar_a_nota_nao_compra_e_diz_por_que(monkeypatch, capsys):
    import runpy

    monkeypatch.setattr(sys, "path", list(sys.path))
    runpy.run_path(str(RAIZ / "demo.py"), run_name="__main__")
    saida = capsys.readouterr().out

    assert "Não executada. Bloqueado pela conformidade: Falta confirmar a emissão" in saida
    assert "EMITE_NOTA_FISCAL=true" in saida
    with conectar() as conn:
        assert conn.execute("SELECT COUNT(*) FROM aprovacoes WHERE status = 'pendente'"
                            ).fetchone()[0] == 2
    assert not worker.PASTA_ORDENS.exists()


# ================= 8. cada pergunta vai ao modelo uma vez, sem texto no registro

class MercadoLivrePerguntas:
    """Perguntas que continuam UNANSWERED no marketplace a cada ciclo."""

    def __init__(self, perguntas):
        self.perguntas = perguntas
        self.fichas = []

    def perguntas_sem_resposta(self, limit=50):
        return list(self.perguntas)

    def anuncio(self, item_id):
        self.fichas.append(item_id)
        return {"title": "Mochila", "price": 99.9, "attributes": []}


@pytest.fixture
def sem_pausa(monkeypatch):
    monkeypatch.setattr(worker.time, "sleep", lambda segundos: None)


def test_mesma_pergunta_em_varios_ciclos_vai_ao_modelo_uma_vez(claude_falso, monkeypatch,
                                                               sem_pausa):
    ml = MercadoLivrePerguntas([{"id": 77, "item_id": "MLB1", "text": "Qual a cor da alça?"}])

    assert worker.atender_perguntas(ml) == 1
    assert worker.atender_perguntas(ml) == 0  # já está na fila
    assert len(claude_falso.mensagens) == 1 and ml.fichas == ["MLB1"]
    assert len(aprovacao.pendentes()) == 1

    # Aprovada em simulação: não é publicada e o ML continua com a pergunta
    # sem resposta. Nem por isso ela volta ao modelo.
    assert aprovacao.foi_simulado(aprovacao.aprovar(aprovacao.pendentes()[0].id,
                                                    worker.EXECUTORES))
    assert worker.atender_perguntas(ml) == 0
    assert len(claude_falso.mensagens) == 1 and aprovacao.pendentes() == []

    # Simulação desligada: o mesmo texto volta para a fila, sem nova chamada.
    monkeypatch.setattr(config.config, "modo_simulacao", False)
    assert worker.atender_perguntas(ml) == 0
    assert worker.atender_perguntas(ml) == 0  # não duplica
    assert len(claude_falso.mensagens) == 1
    assert [(a.tipo, a.payload["question_id"], a.payload["resposta"])
            for a in aprovacao.pendentes()] == [("resposta_cliente", "77", "Azul-marinho.")]
    assert "Resposta à pergunta 77 voltou para a fila" in "\n".join(eventos())


def test_falha_temporaria_tenta_de_novo_sem_repetir_o_aviso(claude_falso, sem_pausa):
    ml = MercadoLivrePerguntas([{"id": 78, "item_id": "MLB1", "text": "Qual a cor da alça?"}])
    claude_falso.erro = erro_api(anthropic.RateLimitError, 429)

    worker.atender_perguntas(ml)
    worker.atender_perguntas(ml)

    assert len(claude_falso.mensagens) == 2
    avisos = [e for e in eventos() if "Pergunta 78" in e]
    assert len(avisos) == 1 and "tenta de novo no próximo ciclo" in avisos[0]

    claude_falso.erro = None
    worker.atender_perguntas(ml)
    worker.atender_perguntas(ml)
    assert len(claude_falso.mensagens) == 3 and len(aprovacao.pendentes()) == 1


@pytest.mark.parametrize("caso", ["recusa", "max_tokens", "http_400", "modelo_escala",
                                  "gatilho"])
def test_escalada_definitiva_nao_volta_ao_modelo(claude_falso, sem_pausa, caso):
    texto = "Qual a cor da alça?"
    if caso == "recusa":
        claude_falso.resposta = resposta_claude(stop_reason="refusal")
    elif caso == "max_tokens":
        claude_falso.resposta = resposta_claude("Texto cort", stop_reason="max_tokens")
    elif caso == "http_400":
        claude_falso.erro = erro_api(anthropic.BadRequestError, 400)
    elif caso == "modelo_escala":
        claude_falso.resposta = resposta_claude("ESCALAR: falta a medida da alça")
    else:
        texto = "Quero devolver"
    ml = MercadoLivrePerguntas([{"id": 79, "item_id": "MLB1", "text": texto}])

    for _ in range(3):
        worker.atender_perguntas(ml)

    assert len(claude_falso.mensagens) == (0 if caso == "gatilho" else 1)
    assert len([e for e in eventos() if "Pergunta 79" in e]) == 1
    assert aprovacao.pendentes() == []


def test_rascunho_apagado_da_fila_volta_ao_modelo(claude_falso, sem_pausa):
    """O demo.py apaga a fila; a pergunta continua sem resposta e sem rascunho."""
    ml = MercadoLivrePerguntas([{"id": 80, "item_id": "MLB1", "text": "Qual a cor da alça?"}])
    worker.atender_perguntas(ml)
    with conectar() as conn:
        conn.execute("DELETE FROM aprovacoes")
    worker.atender_perguntas(ml)
    assert len(claude_falso.mensagens) == 2 and len(aprovacao.pendentes()) == 1


def test_registro_nao_leva_texto_do_comprador_nem_do_modelo(claude_falso):
    from atendimento import bot

    dado = "CPF é 111.222.333-44 e moro na Rua Exemplo 10"
    feito = bot.processar_pergunta("Q1", f"Quero devolver, meu {dado}", {})
    assert feito["acao"] == "escalada"

    claude_falso.resposta = resposta_claude(
        "ESCALAR: confirmar se entregamos na Rua Exemplo 10 para o CPF 111.222.333-44")
    feito = bot.processar_pergunta("Q2", f"Vocês entregam aqui? Meu {dado}", {})
    assert feito["acao"] == "escalada" and "Rua Exemplo 10" in feito["motivo"]

    registro = "\n".join(eventos())
    assert "Pergunta Q1 escalada (contém 'devolver')" in registro
    assert "Pergunta Q2 escalada: o modelo pediu revisão humana" in registro
    for trecho in ("111.222.333-44", "Rua Exemplo", "CPF"):
        assert trecho not in registro


def test_api_de_eventos_nao_devolve_texto_do_comprador(sessao, claude_falso):
    from atendimento import bot

    claude_falso.resposta = resposta_claude("ESCALAR: confirmar o CPF 111.222.333-44")
    bot.processar_pergunta("Q3", "Entregam na Rua Exemplo 10? CPF 111.222.333-44", {})
    cliente, _ = sessao
    corpo = cliente.get("/api/eventos").text
    assert "Pergunta Q3 escalada" in corpo
    assert "111.222.333-44" not in corpo and "Rua Exemplo" not in corpo


# ================== 9. confirmações digitadas à mão e regras sem teste

@pytest.mark.parametrize("valor, confirmacao", [
    ("nao", False), ("false", False), ("não", False), ("no", False), ("off", False),
    (" NAO ", False), (0, False),
    ("", True), ("talvez", True), (2, True), (None, True),
    ("sim", None), ("true", None), (1, None), ("1", None),
])
def test_reembalagem_digitada_a_mao_e_lida_sem_bool(nota_fiscal_confirmada, valor, confirmacao):
    """bool('nao') é True: um 'não' digitado no SQL não pode liberar a compra.
    confirmacao None: passa; False: bloqueio duro; True: falta confirmar."""
    cadastrar(categoria_regulada="nenhuma")
    with conectar() as c:
        c.execute("UPDATE produtos SET reembalagem_confirmada = ? WHERE sku = 'ORG-001'", (valor,))
    res = conformidade.verificar_pendencia(
        "compra_fornecedor", {"marketplace": "amazon", "sku": "ORG-001", "margem_prevista": 30,
                              **campos_do_produto()})
    if confirmacao is None:
        assert not res.bloqueado
    else:
        assert [(v.regra, v.confirmacao) for v in res.bloqueios] == [("AMZ-REEMBALAGEM",
                                                                       confirmacao)]


@pytest.mark.parametrize("valor, confirmacao", [
    ("nao", False), ("off", False), ("", True), ("talvez", True), ("sim", None),
])
def test_habilitacao_digitada_a_mao_e_lida_sem_bool(nota_fiscal_confirmada, valor, confirmacao):
    cadastrar(categoria_regulada="suplemento", habilitacao_confirmada=valor)
    res = conformidade.verificar_pendencia(
        "compra_fornecedor", {"marketplace": "mercadolivre", "sku": "ORG-001",
                              "margem_prevista": 30, **campos_do_produto()})
    if confirmacao is None:
        assert not res.bloqueado
    else:
        assert [(v.regra, v.confirmacao) for v in res.bloqueios] == [("CATEGORIA-RESTRITA",
                                                                       confirmacao)]


@pytest.mark.parametrize("canal", ["API", "site"])
def test_amazon_com_canal_desconhecido_pede_confirmacao_do_envio(nota_fiscal_confirmada, canal):
    cadastrar(canal=canal, categoria_regulada="nenhuma", reembalagem_confirmada=1)
    res = conformidade.verificar_pendencia(
        "compra_fornecedor", {"marketplace": "amazon", "sku": "ORG-001", "margem_prevista": 25,
                              **campos_do_produto()})
    assert [(v.regra, v.confirmacao) for v in res.bloqueios] == [("AMZ-DROPSHIP", True)]


def test_compra_de_produto_que_perdeu_o_fornecedor_bloqueia(nota_fiscal_confirmada):
    """O produto perdeu o fornecedor depois de a compra entrar na fila: a
    ordem iria para o fornecedor antigo. Sem o fornecedor nem na fila, o
    prazo padrão do .env não pode tomar o lugar do prazo que falta."""
    produto = cadastrar(sku="SEM-FORN", categoria_regulada="nenhuma")
    payload = {"marketplace": "mercadolivre", "sku": "SEM-FORN", "margem_prevista": 25,
               **campos_do_produto("SEM-FORN")}
    with conectar() as c:
        c.execute("UPDATE produtos SET fornecedor_id = NULL WHERE id = ?", (produto,))
    res = conformidade.verificar_pendencia("compra_fornecedor", payload)
    assert [(v.regra, v.confirmacao) for v in res.bloqueios] == [("FORNECEDOR-TROCADO", False)]

    res = conformidade.verificar_pendencia(
        "compra_fornecedor", {"marketplace": "mercadolivre", "sku": "SEM-FORN",
                              "margem_prevista": 25, "produto_id": produto, "quantidade": 1,
                              "valor": "18.50", "moeda": "BRL"})
    assert [(v.regra, v.confirmacao) for v in res.bloqueios] == [
        ("FORNECEDOR-TROCADO", True), ("PRAZO", True)]


def test_publicar_anuncio_sem_dados_bloqueia_pedindo_confirmacao(nota_fiscal_confirmada):
    res = conformidade.verificar_pendencia("publicar_anuncio", {"preco": 50})
    assert sorted((v.regra, v.confirmacao) for v in res.bloqueios) == [
        ("CATEGORIA-RESTRITA", True), ("CDC-GARANTIA", True)]


# ======================================== 10. LGPD: exportação e rastreio

def test_exportar_dados_nao_quebra_com_endereco_vazio_legado_ou_ilegivel():
    outra = Fernet(Fernet.generate_key()).encrypt(b'{"shipping_id": "1"}').decode()
    for i, endereco in enumerate(["{}", '{"shipping_id": "9"}', outra]):
        inserir_pedido(id_externo=f"80{i}", comprador_id=privacidade.cifrar("424242"),
                       endereco_json=endereco)

    dados = privacidade.exportar_dados("424242")

    assert {r["pedido"]: r["endereco"] for r in dados["registros"]} == {
        "800": {}, "801": {"shipping_id": "9"}, "802": {}}
    assert [a[1] for a in acessos()] == ["exportacao"] * 3


class MercadoLivreEnvios:
    STATUS = {"777": "delivered", "888": "shipped"}

    def __init__(self):
        self.consultas = []

    def envio(self, shipping_id):
        self.consultas.append(shipping_id)
        return {"status": self.STATUS[shipping_id], "tracking_number": f"BR{shipping_id}"}


def test_rastreio_le_o_endereco_cifrado_e_marca_entregue():
    """O endereco_json é cifrado na ingestão; antes, o rastreio fazia
    json.loads no texto cifrado e a etapa quebrava em todo ciclo."""
    entregue = inserir_pedido(id_externo="700", estado=Estado.EM_TRANSITO.value,
                              endereco_json=privacidade.cifrar('{"shipping_id": 777}'))
    viajando = inserir_pedido(id_externo="701", estado=Estado.EM_TRANSITO.value,
                              endereco_json=privacidade.cifrar('{"shipping_id": 888}'))
    outra = Fernet(Fernet.generate_key()).encrypt(b'{"shipping_id": 999}').decode()
    inserir_pedido(id_externo="702", estado=Estado.EM_TRANSITO.value, endereco_json="{}")
    inserir_pedido(id_externo="703", estado=Estado.COMPRA_CONFIRMADA.value, endereco_json=outra)
    ml = MercadoLivreEnvios()

    assert worker.atualizar_rastreio(ml) == 1
    assert worker.atualizar_rastreio(ml) == 0

    assert ml.consultas == ["777", "888", "888"]  # vazio e ilegível pulados, sem erro
    with conectar() as c:
        linhas = {l["id"]: (l["estado"], l["codigo_rastreio"]) for l in c.execute(
            "SELECT id, estado, codigo_rastreio FROM pedidos WHERE id IN (?, ?)",
            (entregue, viajando))}
    assert linhas == {entregue: ("ENTREGUE", "BR777"), viajando: ("EM_TRANSITO", "BR888")}
    # Um registro de acesso por pedido, não um por ciclo.
    assert acessos() == [(entregue, "rastreio", "worker"), (viajando, "rastreio", "worker")]


def test_ciclo_do_worker_nao_quebra_no_rastreio(monkeypatch):
    class MercadoLivreCiclo(MercadoLivreEnvios):
        def __init__(self, *a, **k):
            super().__init__()

        def pedidos_recentes(self, limit=50):
            return []

        def perguntas_sem_resposta(self, limit=50):
            return []

    inserir_pedido(id_externo="704", estado=Estado.EM_TRANSITO.value,
                   endereco_json=privacidade.cifrar('{"shipping_id": 777}'))
    monkeypatch.setattr(worker, "MercadoLivre", MercadoLivreCiclo)
    assert worker.ciclo()["rastreios"] == 1


# ============== 11. publicação que falhou volta; um ciclo de cada vez

def test_resposta_cuja_publicacao_falhou_volta_para_a_fila_sem_nova_chamada(
        claude_falso, monkeypatch, sem_pausa):
    """Publicar a resposta aprovada falhou uma vez (Mercado Livre fora do ar):
    a aprovação fica com status 'erro' e não pode ser aprovada de novo. A
    pergunta continua sem resposta no marketplace; o mesmo rascunho volta
    para a fila, sem nova chamada ao modelo."""
    from conectores.mercadolivre import ErroMercadoLivre

    monkeypatch.setattr(config.config, "modo_simulacao", False)
    falhas = [ErroMercadoLivre("POST /answers -> 503: temporarily unavailable")]
    publicadas = []

    class MercadoLivreInstavel:
        def __init__(self, *a, **k):
            pass

        def responder_pergunta(self, question_id, texto):
            if falhas:
                raise falhas.pop()
            publicadas.append((question_id, texto))

    monkeypatch.setattr(worker, "MercadoLivre", MercadoLivreInstavel)
    ml = MercadoLivrePerguntas([{"id": 81, "item_id": "MLB1", "text": "Qual a cor da alça?"}])

    assert worker.atender_perguntas(ml) == 1
    primeira = aprovacao.pendentes()[0].id
    with pytest.raises(ErroMercadoLivre):
        aprovacao.aprovar(primeira, worker.EXECUTORES)
    assert aprovacao.obter_pendente(primeira) is None  # 'erro': não se aprova de novo

    assert worker.atender_perguntas(ml) == 0
    assert worker.atender_perguntas(ml) == 0  # não duplica
    assert len(claude_falso.mensagens) == 1
    novas = aprovacao.pendentes()
    assert [(a.tipo, a.payload["question_id"], a.payload["resposta"]) for a in novas] == [
        ("resposta_cliente", "81", "Azul-marinho.")]
    assert ("Resposta à pergunta 81 voltou para a fila: a publicação anterior falhou"
            in "\n".join(eventos()))

    # A nova aprovação publica; publicada, a pergunta não volta.
    aprovacao.aprovar(novas[0].id, worker.EXECUTORES)
    assert publicadas == [("81", "Azul-marinho.")]
    assert worker.atender_perguntas(ml) == 0
    assert aprovacao.pendentes() == [] and len(claude_falso.mensagens) == 1


def test_resposta_recusada_depois_da_falha_nao_volta(claude_falso, monkeypatch, sem_pausa):
    from conectores.mercadolivre import ErroMercadoLivre

    monkeypatch.setattr(config.config, "modo_simulacao", False)

    class MercadoLivreFora:
        def __init__(self, *a, **k):
            pass

        def responder_pergunta(self, question_id, texto):
            raise ErroMercadoLivre("POST /answers -> 503")

    monkeypatch.setattr(worker, "MercadoLivre", MercadoLivreFora)
    ml = MercadoLivrePerguntas([{"id": 82, "item_id": "MLB1", "text": "Qual a cor da alça?"}])
    worker.atender_perguntas(ml)
    with pytest.raises(ErroMercadoLivre):
        aprovacao.aprovar(aprovacao.pendentes()[0].id, worker.EXECUTORES)
    worker.atender_perguntas(ml)
    aprovacao.recusar(aprovacao.pendentes()[0].id, "respondo no Mercado Livre")

    worker.atender_perguntas(ml)
    assert aprovacao.pendentes() == [] and len(claude_falso.mensagens) == 1


class Bloqueio:
    """Segura a primeira chamada até o teste liberar; as outras passam direto."""

    def __init__(self):
        self.entrou = threading.Event()
        self.liberar = threading.Event()
        self.chamadas = 0
        self._trava = threading.Lock()

    def esperar_se_primeira(self):
        with self._trava:
            self.chamadas += 1
            primeira = self.chamadas == 1
        if primeira:
            self.entrou.set()
            self.liberar.wait(10)


class MercadoLivreDoCiclo:
    """O conector que worker.ciclo cria, sem rede."""
    perguntas: list = []

    def __init__(self, *a, **k):
        pass

    def pedidos_recentes(self, limit=50):
        return []

    def perguntas_sem_resposta(self, limit=50):
        return list(self.perguntas)

    def anuncio(self, item_id):
        return {"title": "Mochila", "price": 99.9, "attributes": []}


def tecla_c_durante_o_ciclo_de_fundo(cliente, csrf, bloqueio):
    """Um ciclo numa thread, como o laço de fundo do executar.py, parado no
    ponto do bloqueio; enquanto isso, a tecla 'c' do painel pede outro.
    Devolve (resumo do ciclo de fundo, resposta do painel)."""
    fundo = {}
    fio = threading.Thread(target=lambda: fundo.update(worker.ciclo()))
    fio.start()
    try:
        assert bloqueio.entrou.wait(10)
        resposta = cliente.post("/api/ciclo", headers=cabecalho(csrf))
    finally:
        bloqueio.liberar.set()
        fio.join(20)
    assert not fio.is_alive()
    return fundo, resposta


def test_dois_ciclos_ao_mesmo_tempo_nao_mandam_a_pergunta_duas_vezes(sessao, claude_falso,
                                                                    monkeypatch, sem_pausa):
    cliente, csrf = sessao
    monkeypatch.setattr(MercadoLivreDoCiclo, "perguntas",
                        [{"id": 90, "item_id": "MLB1", "text": "Qual a cor da alça?"}])
    monkeypatch.setattr(worker, "MercadoLivre", MercadoLivreDoCiclo)
    bloqueio = Bloqueio()
    criar = claude_falso._criar

    def criar_devagar(**kwargs):  # o ciclo de fundo para dentro da chamada ao modelo
        bloqueio.esperar_se_primeira()
        return criar(**kwargs)

    claude_falso._criar = criar_devagar

    fundo, r = tecla_c_durante_o_ciclo_de_fundo(cliente, csrf, bloqueio)

    assert r.status_code == 409 and "ciclo em andamento" in r.json()["detail"]
    assert fundo["perguntas"] == 1
    assert bloqueio.chamadas == 1 and len(claude_falso.mensagens) == 1
    assert [a.payload["question_id"] for a in aprovacao.pendentes()] == ["90"]
    # Terminado o ciclo de fundo, a tecla volta a rodar um ciclo.
    r = cliente.post("/api/ciclo", headers=cabecalho(csrf))
    assert r.status_code == 200 and r.json()["perguntas"] == 0


def test_dois_ciclos_ao_mesmo_tempo_nao_repoem_a_compra_duas_vezes(sessao, fila_de_compras,
                                                                  monkeypatch):
    cliente, csrf = sessao
    for alvo in fila_de_compras:  # aprovadas em simulação: nada comprado
        assert aprovacao.foi_simulado(aprovacao.aprovar(alvo, worker.EXECUTORES))
    with conectar() as conn:
        pedidos = sorted(l[0] for l in conn.execute(
            "SELECT pedido_id FROM aprovacoes WHERE tipo = 'compra_fornecedor'"))
    monkeypatch.setattr(config.config, "modo_simulacao", False)
    monkeypatch.setattr(worker, "MercadoLivre", MercadoLivreDoCiclo)
    bloqueio = Bloqueio()
    ler = worker._compras_simuladas

    def ler_e_esperar(conn):  # o ciclo de fundo para entre a leitura e a fila
        linhas = ler(conn)
        bloqueio.esperar_se_primeira()
        return linhas

    monkeypatch.setattr(worker, "_compras_simuladas", ler_e_esperar)

    fundo, r = tecla_c_durante_o_ciclo_de_fundo(cliente, csrf, bloqueio)

    assert r.status_code == 409
    assert fundo["ordens_montadas"] == 2
    assert sorted(a.pedido_id for a in aprovacao.pendentes("compra_fornecedor")) == pedidos
    assert worker.ciclo()["ordens_montadas"] == 0


# "travar": pega a trava do ciclo, avisa e espera uma linha na entrada.
TRAVA_DO_CICLO_EM_OUTRO_PROCESSO = textwrap.dedent("""
    import os, sys
    fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, 0, os.SEEK_SET)
    print("travado", flush=True)
    sys.stdin.readline()
""")


def test_ciclo_de_outro_processo_faz_esta_passada_ser_pulada(tmp_path, sessao, monkeypatch):
    """Um `cli.py ciclo` ao lado do painel: a trava fica num arquivo ao lado
    do banco, e o outro processo não roda nada enquanto ela está presa."""
    cliente, csrf = sessao
    chamadas = []

    class MercadoLivreContado(MercadoLivreDoCiclo):
        def pedidos_recentes(self, limit=50):
            chamadas.append("pedidos")
            return []

    monkeypatch.setattr(worker, "MercadoLivre", MercadoLivreContado)
    assert Path(config.config.db_path).parent == tmp_path
    outro = subprocess.Popen(
        [sys.executable, "-c", TRAVA_DO_CICLO_EM_OUTRO_PROCESSO, str(tmp_path / ".trava_ciclo")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert outro.stdout.readline().strip() == "travado"
        resumo = worker.ciclo()
        r = cliente.post("/api/ciclo", headers=cabecalho(csrf))
    finally:
        outro.communicate(input="\n", timeout=30)

    assert list(resumo) == ["pulado"] and chamadas == []
    assert r.status_code == 409 and "outro" in r.json()["detail"]
    # Solta a trava, o ciclo roda.
    assert worker.ciclo()["ingeridos"] == 0 and chamadas == ["pedidos"]


# ======================== 12. prazo digitado à mão; tabelas LGPD sem o painel

@pytest.mark.parametrize("prazo", ["6 dias", "seis", "  "])
def test_prazo_do_fornecedor_que_nao_e_numero_bloqueia_sem_quebrar(sessao, nota_fiscal_confirmada,
                                                                    prazo):
    """'6 dias' digitado no SQL fica TEXT na coluna INTEGER. Antes, a comparação
    levantava TypeError: a fila do painel inteira dava 500 e o worker não
    montava compra nenhuma."""
    cliente, csrf = sessao
    produto = cadastrar(prazo=prazo, categoria_regulada="nenhuma")
    pendente = compra_na_fila()

    fila = cliente.get("/api/pendencias")
    assert fila.status_code == 200
    item = fila.json()["itens"][0]
    assert item["bloqueado"] and [(v["regra"], v["confirmacao"]) for v in item["violacoes"]] == [
        ("PRAZO", True)]
    assert cliente.post(f"/api/aprovar/{pendente}", headers=cabecalho(csrf)).status_code == 409

    # O worker põe a compra na fila, bloqueada pedindo o prazo, sem PROBLEMA.
    pedido = inserir_pedido(id_externo="5000000002", produto_id=produto,
                            estado=Estado.ANALISADO.value, custo_previsto=18.5,
                            margem_prevista=30.0)
    assert worker.montar_ordens_de_compra() == 1
    assert estado_do_pedido(pedido) == "AGUARDANDO_APROVACAO"


@pytest.mark.parametrize("gravado, lido", [(None, 5), (0, 5), ("", 5), (4, 4), (9, 9),
                                           ("12", 12), ("6 dias", None), ("6.5", None)])
def test_prazo_do_fornecedor_le_o_cadastro_sem_bool(monkeypatch, gravado, lido):
    monkeypatch.setattr(config.config.negocio, "prazo_fornecedor_dias", 5)
    assert conformidade.prazo_do_fornecedor(gravado) == lido


def test_banco_criado_sem_o_painel_tem_as_tabelas_da_lgpd(monkeypatch, tmp_path, capsys):
    """`cli.py init` e o worker sem o painel: o rastreio registra o acesso em
    acessos_pii, que antes só o painel criava."""
    import cli

    monkeypatch.setattr(config.config, "db_path", str(tmp_path / "so_cli.db"))
    monkeypatch.setattr(sys, "argv", ["cli.py", "init"])
    cli.main()
    with conectar() as conn:
        tabelas = {l[0] for l in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"acessos_pii", "solicitacoes_titular"} <= tabelas

    # Banco antigo, criado só com o schema principal: o ciclo cria o que falta.
    with conectar() as conn:
        conn.execute("DROP TABLE acessos_pii")
    pedido = inserir_pedido(id_externo="705", estado=Estado.EM_TRANSITO.value,
                            endereco_json=privacidade.cifrar('{"shipping_id": 777}'))

    class MercadoLivreCiclo(MercadoLivreEnvios):
        def __init__(self, *a, **k):
            super().__init__()

        def pedidos_recentes(self, limit=50):
            return []

        def perguntas_sem_resposta(self, limit=50):
            return []

    monkeypatch.setattr(worker, "MercadoLivre", MercadoLivreCiclo)
    assert worker.ciclo()["rastreios"] == 1
    assert acessos() == [(pedido, "rastreio", "worker")]
