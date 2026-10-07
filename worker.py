"""
Worker: o laço principal do robô.

Roda a cada N minutos (INTERVALO_WORKER) e executa, em ordem:

  1. Ingestão        — puxa pedidos novos dos marketplaces
  2. Análise         — calcula margem real; recusa o que não fecha
  3. Roteamento      — monta a ordem de compra, passa pela conformidade e põe
                       na fila de aprovação. Toda compra espera o seu OK; o
                       teto (TETO_COMPRA_AUTOMATICA) só muda o rótulo da fila,
                       ROTINA ou ACIMA DO TETO
  4. Atendimento     — redige respostas às perguntas abertas que o bot ainda
                       não tratou (pra fila); cada pergunta vai ao modelo uma
                       vez, salvo falha temporária
  5. Rastreio        — atualiza status de quem já está em trânsito

Nenhuma etapa gasta dinheiro ou publica texto. Publicar resposta e mudar
preço só acontecem depois que você aprova na fila, e só com MODO_SIMULACAO
desligado. A compra aprovada vira um arquivo de ordem de compra: quem envia
ao fornecedor, e paga, é você. Aprovação em simulação não consome o item:
a compra e a resposta voltam para a fila quando MODO_SIMULACAO for
desligado. É isso que mantém o robô seguro rodando desatendido.

Um ciclo de cada vez: com outro em andamento, neste processo ou em outro,
a passada é pulada (ciclo).
"""
import json
import os
import threading
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from config import config, DATA_DIR, avisar_pasta_de_dados
from db import conectar, agora, inicializar, registrar_evento
from core import aprovacao, cadastro, conformidade, dinheiro, privacidade
# A mesma trava de arquivo que o cofre usa para a renovação de tokens.
from core.cofre import _soltar_arquivo, _trancar
from core.estados import Estado, transicionar
from inteligencia import precificacao
from conectores.mercadolivre import MercadoLivre, ErroMercadoLivre
from atendimento import bot

PASTA_ORDENS = DATA_DIR / "ordens_de_compra"


# --------------------------------------------------------------- 1. Ingestão

def ingerir_mercadolivre(ml: MercadoLivre) -> int:
    novos = 0
    try:
        brutos = ml.pedidos_recentes(limit=50)
    except ErroMercadoLivre as e:
        registrar_evento("erro", "worker", f"Ingestão ML falhou: {e}")
        return 0

    for bruto in brutos:
        p = ml.normalizar_pedido(bruto)
        with conectar() as conn:
            existe = conn.execute(
                "SELECT 1 FROM pedidos WHERE marketplace = ? AND id_externo = ?",
                (p["marketplace"], p["id_externo"]),
            ).fetchone()
            if existe:
                continue

            produto = conn.execute(
                "SELECT id FROM produtos WHERE sku = ?", (p["sku"],)
            ).fetchone()

            # O valor vem do JSON do marketplace como float: entra em Decimal
            # pelo texto curto (dinheiro.do_real). A moeda é a que o
            # marketplace informou (currency_id); sem ela, fica NULL e a
            # margem diz por que não calcula. Nada presume BRL.
            valor = dinheiro.do_real(p["valor_bruto"])
            # LGPD art. 46: PII cifrada já na entrada. Em nenhum momento o
            # nome ou o identificador do comprador toca o disco em texto puro.
            conn.execute(
                "INSERT INTO pedidos (marketplace, id_externo, produto_id, quantidade,"
                " valor_bruto, valor_bruto_dec, valor_bruto_moeda, estado, comprador_nome,"
                " comprador_id, endereco_json, criado_em, atualizado_em)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (p["marketplace"], p["id_externo"], produto["id"] if produto else None,
                 p["quantidade"], p["valor_bruto"],
                 dinheiro.texto(valor) if valor is not None else None,
                 dinheiro.moeda_lida(p.get("moeda")), Estado.NOVO.value,
                 privacidade.cifrar(p["comprador_nome"]),
                 privacidade.cifrar(p["comprador_id"]),
                 privacidade.cifrar(json.dumps({"shipping_id": p["shipping_id"]})),
                 agora(), agora()),
            )
        novos += 1

    if novos:
        registrar_evento("info", "worker", f"{novos} pedido(s) novo(s) do Mercado Livre")
    return novos


# ---------------------------------------------------------------- 2. Análise

def _comando_custo(ped) -> str:
    """O comando que cadastra o custo com a moeda, com o SKU só quando ele
    pode ir para o terminal (conformidade.sku_no_comando)."""
    sku = conformidade.sku_no_comando(ped["sku_produto"])
    comando = f"python cli.py produto editar {sku} --custo VALOR --moeda BRL"
    if sku != ped["sku_produto"]:
        comando += (f" (troque SKU pelo SKU do produto {ped['produto_id']}: ele tem "
                    "caracteres que o terminal interpretaria)")
    return comando


def _comando_reanalisar(ped) -> str:
    """O comando que tira o pedido de PROBLEMA depois de corrigido o dado
    (reanalisar). Pedido sem a moeda da venda precisa dela: ninguém presume."""
    comando = f"python cli.py pedido reanalisar {ped['id']}"
    if dinheiro.moeda_lida(ped["valor_bruto_moeda"]) is None:
        comando += (" --moeda-venda BRL (a moeda da venda não foi informada: troque BRL pela "
                    "moeda em que a venda foi feita)")
    return comando


def _motivo_sem_margem(ped, custo, venda) -> str | None:
    """Por que não dá para calcular a margem deste pedido, ou None. Valor
    ilegível, ou moeda desconhecida ou diferente: o pedido vai para PROBLEMA
    com o motivo, em vez de misturar moedas. Quando há o que corrigir, o
    motivo diz como, e como reanalisar o pedido depois."""
    if custo is None:
        return ("custo do produto ilegível no cadastro; cadastre-o de novo: "
                f"{_comando_custo(ped)}; depois, {_comando_reanalisar(ped)}")
    if venda is None:
        return "valor da venda ilegível"
    moeda_custo = dinheiro.moeda_lida(ped["custo_fornecedor_moeda"])
    moeda_venda = dinheiro.moeda_lida(ped["valor_bruto_moeda"])
    motivo = precificacao.motivo_sem_margem(moeda_custo, moeda_venda)
    if motivo is None:
        if _peso(ped) is None:
            return ("peso do produto ilegível no cadastro; corrija o peso_kg do produto "
                    f"{ped['produto_id']} (--peso no cli.py ou PATCH /api/produtos/<id>); "
                    f"depois, {_comando_reanalisar(ped)}")
        return None
    if moeda_venda not in (None, precificacao.MOEDA):
        return motivo  # venda em outra moeda: não há cadastro que resolva
    if moeda_custo != precificacao.MOEDA:
        return (f"{motivo}. Cadastre o custo com a moeda: {_comando_custo(ped)}; depois, "
                f"{_comando_reanalisar(ped)}")
    # Custo em BRL e venda sem moeda: só você sabe a moeda da venda.
    return motivo + f". Se você sabe a moeda desta venda, registre-a: {_comando_reanalisar(ped)}"


PESO_PADRAO = Decimal("0.3")


def _peso(ped) -> Decimal | None:
    """Peso do produto para o custo de envio. Vazio ou 0 vale 0.3 kg, como
    sempre valeu. Ilegível (texto, ou um 9e999 gravado à mão, que o SQLite
    guarda como infinito) é None: só este pedido vai para PROBLEMA, em vez de
    a exceção parar a análise de todos os pedidos novos."""
    if not ped["peso_kg"]:
        return PESO_PADRAO
    return dinheiro.do_real(ped["peso_kg"])


def _analisar(ped) -> tuple[tuple | None, str | None]:
    """A margem de um pedido com os dados de agora do produto (custo, moeda e
    peso lidos do cadastro na linha ped). Devolve ((custo_total, moeda,
    composição), None), ou (None, motivo) quando não dá para calcular: aí o
    pedido vai para PROBLEMA com o motivo. A mesma conta serve ao pedido NOVO
    (analisar_novos) e ao pedido que volta para a fila (_reprecificar)."""
    if ped["custo_fornecedor"] is None and ped["custo_fornecedor_dec"] is None:
        return None, "SKU não cadastrado em produtos — sem custo pra calcular margem"

    custo = dinheiro.ler(ped["custo_fornecedor_dec"], ped["custo_fornecedor"])
    venda = dinheiro.ler(ped["valor_bruto_dec"], ped["valor_bruto"])
    motivo = _motivo_sem_margem(ped, custo, venda)
    if motivo:
        return None, f"Margem não calculada: {motivo}"

    cfg = config.negocio
    custo_total = custo * ped["quantidade"]
    comp = precificacao.calcular(
        preco_venda=venda,
        custo_produto=custo_total,
        peso_kg=_peso(ped),
        aliquota_imposto_pct=cfg.aliquota_imposto_pct,
        margem_minima_pct=cfg.margem_minima_pct,
    )
    moeda = dinheiro.moeda_lida(ped["custo_fornecedor_moeda"])  # a mesma da venda
    return (custo_total, moeda, comp), None


def _gravar_previsto(pedido_id: int, calculo: tuple | None):
    """Grava o custo previsto, a moeda e a margem da análise. None apaga os
    quatro: o custo e a margem velhos não podem servir a uma ordem nova. As
    colunas REAL antigas recebem o espelho por CAST, a partir do texto."""
    if calculo is None:
        custo_texto = moeda = margem = None
    else:
        custo_total, moeda, comp = calculo
        custo_texto, margem = dinheiro.texto(custo_total), str(comp.margem_pct)
    with conectar() as conn:
        conn.execute(
            "UPDATE pedidos SET custo_previsto = CAST(? AS REAL), custo_previsto_dec = ?,"
            " custo_previsto_moeda = ?, margem_prevista = CAST(? AS REAL) WHERE id = ?",
            (custo_texto, custo_texto, moeda, margem, pedido_id),
        )


def analisar_novos() -> int:
    """Calcula a margem real de cada pedido novo e decide o destino. Os
    valores são lidos em Decimal; a margem só sai com custo e venda na mesma
    moeda, a do modelo de taxas (precificacao.MOEDA)."""
    cfg = config.negocio
    processados = 0

    with conectar() as conn:
        pendentes = conn.execute(
            "SELECT p.*, pr.custo_fornecedor, pr.custo_fornecedor_dec,"
            " pr.custo_fornecedor_moeda, pr.peso_kg, pr.sku AS sku_produto,"
            " pr.titulo AS titulo_produto, pr.fornecedor_id FROM pedidos p"
            " LEFT JOIN produtos pr ON pr.id = p.produto_id"
            " WHERE p.estado = ?", (Estado.NOVO.value,),
        ).fetchall()

    for ped in pendentes:
        calculo, motivo = _analisar(ped)
        if motivo:
            transicionar(ped["id"], Estado.PROBLEMA, motivo)
            continue

        _gravar_previsto(ped["id"], calculo)
        _, _, comp = calculo
        if not comp.viavel:
            transicionar(ped["id"], Estado.RECUSADO_MARGEM,
                         f"Margem {comp.margem_pct}% < mínimo {cfg.margem_minima_pct}%. "
                         f"Lucro previsto R$ {comp.lucro_liquido}")
            continue

        transicionar(ped["id"], Estado.ANALISADO,
                     f"Margem {comp.margem_pct}% | lucro R$ {comp.lucro_liquido}")
        processados += 1

    return processados


# ------------------------------------------------------------- 3. Roteamento

_SQL_COMPRA = (
    "SELECT p.*, pr.sku, pr.sku AS sku_produto, pr.titulo AS titulo_produto,"
    " pr.custo_fornecedor, pr.custo_fornecedor_dec, pr.custo_fornecedor_moeda, pr.peso_kg,"
    " pr.fornecedor_id, f.nome AS fornecedor_nome, f.canal, f.contato, f.prazo_dias"
    " FROM pedidos p"
    " JOIN produtos pr ON pr.id = p.produto_id"
    " LEFT JOIN fornecedores f ON f.id = pr.fornecedor_id"
)


def _compras_simuladas(conn) -> list:
    """Pedidos em AGUARDANDO_APROVACAO cuja última aprovação de compra foi
    executada em modo simulação: nada foi comprado, e o pedido continua
    precisando da compra de verdade."""
    prefixo = aprovacao.PREFIXO_SIMULACAO
    return conn.execute(
        _SQL_COMPRA + " WHERE p.estado = ? AND EXISTS ("
        " SELECT 1 FROM aprovacoes a WHERE a.id = ("
        "   SELECT MAX(b.id) FROM aprovacoes b"
        "   WHERE b.pedido_id = p.id AND b.tipo = 'compra_fornecedor')"
        " AND a.status = 'executada' AND substr(a.resultado, 1, ?) = ?)",
        (Estado.AGUARDANDO_APROVACAO.value, len(prefixo), prefixo),
    ).fetchall()


VOLTA_SIMULACAO = "a aprovação anterior foi em modo simulação e nada foi comprado."


def _compras_a_refazer() -> list[tuple]:
    """(pedido, por que volta, (pendência a recusar, causa)) das compras na
    fila que a conformidade bloqueia por FORNECEDOR-TROCADO ou CUSTO-ALTERADO:
    o produto passou a outro fornecedor, ou o custo ou a moeda dele mudou,
    depois de a ordem ser montada; ou a pendência é de uma versão que não
    gravava o fornecedor. Aprovar pagaria a quem não é mais o fornecedor, ou
    o valor velho com a margem velha, então a ordem é montada de novo com os
    dados atuais do produto: margem refeita (_reprecificar) e a mesma
    checagem."""
    refazer, pedidos = [], set()
    for item in aprovacao.pendentes("compra_fornecedor"):
        causas = [v for v in aprovacao.checar(item).bloqueios
                  if v.regra in conformidade.REFAZEM_A_ORDEM]
        if not causas or item.pedido_id is None or item.pedido_id in pedidos:
            continue  # um pedido é montado uma vez por ciclo
        pedidos.add(item.pedido_id)
        with conectar() as conn:
            ped = conn.execute(_SQL_COMPRA + " WHERE p.id = ? AND p.estado = ?",
                               (item.pedido_id, Estado.AGUARDANDO_APROVACAO.value)).fetchone()
        if ped is not None:
            causa = " ".join(v.mensagem for v in causas)
            volta = (f"{causa} A pendência {item.id} foi recusada e a ordem, montada de novo "
                     "com os dados atuais do produto.")
            refazer.append((ped, volta, (item.id, causa)))
    return refazer


def _para_problema(pedido_id: int, motivo: str) -> str:
    transicionar(pedido_id, Estado.PROBLEMA, motivo)
    return motivo


def _previsto_confere(ped) -> bool:
    """O custo previsto gravado na análise ainda é o custo atual do produto
    vezes a quantidade, na mesma moeda (a conta de CUSTO-ALTERADO)?"""
    return conformidade.custo_confere(
        dinheiro.ler(ped["custo_previsto_dec"], ped["custo_previsto"]),
        dinheiro.moeda_lida(ped["custo_previsto_moeda"]), ped["quantidade"],
        dinheiro.ler(ped["custo_fornecedor_dec"], ped["custo_fornecedor"]),
        dinheiro.moeda_lida(ped["custo_fornecedor_moeda"])) is True


def _reprecificar(ped) -> tuple:
    """A margem refeita com os dados de agora do produto antes de montar a
    ordem, para ela não levar o custo, a moeda e a margem de outra época: o
    pedido que já esperava na fila e volta para ela (ordem refeita, ou compra
    aprovada em simulação), ou o ANALISADO cujo custo previsto não confere
    mais com o produto. Devolve (linha relida, None), ou (None, motivo) com
    o pedido em PROBLEMA, que é o destino que os dois estados têm além da
    compra: também quando a margem não fecha mais."""
    calculo, motivo = _analisar(ped)
    _gravar_previsto(ped["id"], calculo)
    if calculo is not None and not calculo[2].viavel:
        comp = calculo[2]
        motivo = (f"Margem {comp.margem_pct}% < mínimo {config.negocio.margem_minima_pct}% com "
                  f"o custo atual do produto. Lucro previsto R$ {comp.lucro_liquido}")
    if motivo:
        return None, _para_problema(ped["id"], f"A ordem de compra não foi montada. {motivo}")
    with conectar() as conn:
        return conn.execute(_SQL_COMPRA + " WHERE p.id = ?", (ped["id"],)).fetchone(), None


def _montar_ordem(ped, teto, volta: str | None) -> str | None:
    """Monta a ordem de um pedido e a põe na fila. Devolve None quando ela
    entrou; senão, o motivo com que o pedido foi para PROBLEMA. volta: por
    que um pedido que já está em AGUARDANDO_APROVACAO volta para a fila (sem
    transição, só o aviso); None para o pedido ANALISADO. A margem é refeita
    antes (_reprecificar) quando o pedido volta, e quando o custo previsto
    do ANALISADO não confere mais com o produto."""
    if volta or not _previsto_confere(ped):
        ped, motivo = _reprecificar(ped)
        if motivo:
            return motivo

    if ped["fornecedor_nome"] is None:
        return _para_problema(ped["id"], "Produto sem fornecedor vinculado")

    # Prazo que não é número: a conformidade abaixo bloqueia pedindo o prazo.
    prazo = conformidade.prazo_do_fornecedor(ped["prazo_dias"])
    if prazo is not None and prazo > conformidade.PRAZO_MAXIMO_SEM_ANUNCIO:
        return _para_problema(ped["id"],
                              f"Prazo do fornecedor ({prazo}d) tende a estourar o do anúncio")

    # Decimal da coluna decimal; linha antiga (só a REAL) fica sem moeda.
    valor = dinheiro.ler(ped["custo_previsto_dec"], ped["custo_previsto"])
    moeda = dinheiro.moeda_lida(ped["custo_previsto_moeda"])
    if valor is None:
        return _para_problema(ped["id"],
                              "Custo previsto ausente ou ilegível; a compra não foi montada")

    payload = {
        "pedido_id": ped["id"],
        # Os ids: a conformidade acha o produto e o fornecedor por eles, não
        # pelo SKU (editável), e confere que o fornecedor do produto não mudou.
        "produto_id": ped["produto_id"],
        "fornecedor_id": ped["fornecedor_id"],
        "fornecedor": ped["fornecedor_nome"],
        "canal": ped["canal"],
        "contato": ped["contato"],
        "sku": ped["sku"],
        "produto": ped["titulo_produto"],
        "quantidade": ped["quantidade"],
        # Texto decimal, para o JSON da fila não passar por float.
        "valor": dinheiro.texto(valor),
        "moeda": moeda,
        "marketplace": ped["marketplace"],
        "pedido_externo": ped["id_externo"],
        "margem_prevista": ped["margem_prevista"],
    }

    # Conformidade ANTES de enfileirar, a mesma da aprovação. Violação dura
    # nem chega à fila — evita que você aprove por reflexo algo que
    # suspende a conta.
    checagem = conformidade.verificar_pendencia("compra_fornecedor", payload)
    if checagem.bloqueado and not checagem.so_falta_confirmar:
        return _para_problema(ped["id"], f"Bloqueado pela conformidade — {checagem.resumo}")

    # O rótulo é só informação: as duas saem da mesma fila, com o seu OK.
    # Teto ilegível (None) fica com o rótulo que chama mais atenção.
    urgencia = "ROTINA" if teto is not None and valor <= teto else "ACIMA DO TETO"
    aprovacao.enfileirar(
        tipo="compra_fornecedor",
        resumo=(f"[{urgencia}] Comprar {ped['quantidade']}x {ped['sku']} de "
                f"{ped['fornecedor_nome']} por {dinheiro.formatar(valor, moeda)} "
                f"(margem {ped['margem_prevista']}%)"),
        payload=payload,
        pedido_id=ped["id"],
        valor=valor,
    )
    motivo = f"Ordem de compra montada, aguardando OK ({urgencia})"
    if checagem.bloqueado:
        motivo += f"; bloqueada até confirmar — {checagem.resumo}"
    if volta:
        # Já está em AGUARDANDO_APROVACAO: não há transição, só o aviso.
        registrar_evento("atencao", "worker",
                         f"Pedido {ped['id']} voltou para a fila: {volta} {motivo}")
    else:
        transicionar(ped["id"], Estado.AGUARDANDO_APROVACAO, motivo)
    return None


def montar_ordens_de_compra() -> int:
    """
    Monta a ordem de compra pro fornecedor e joga na fila de aprovação.
    Nada é enviado aqui. Toda compra espera o seu OK, abaixo ou acima do
    teto; o teto só decide o rótulo [ROTINA] / [ACIMA DO TETO].

    Conformidade antes de enfileirar, com a mesma checagem da aprovação:
    violação dura manda o pedido para PROBLEMA; falta de confirmação
    (categoria, nota fiscal, reembalagem...) entra na fila já bloqueada, com
    o motivo e a saída, e libera sozinha quando o dado é preenchido.

    Compra aprovada em modo simulação não consome o pedido: ele fica em
    AGUARDANDO_APROVACAO e, com MODO_SIMULACAO desligado, volta para a fila
    aqui, com a margem refeita pelos dados atuais do produto e a mesma
    checagem.

    Compra na fila cujo produto passou a outro fornecedor (FORNECEDOR-TROCADO)
    ou mudou de custo ou de moeda (CUSTO-ALTERADO) é montada de novo com os
    dados atuais do produto, margem refeita. A pendência velha é recusada
    depois, com o motivo: se a ordem nova entrou, diz isso; se a montagem
    falhou, diz que o pedido foi para PROBLEMA, e por quê.
    """
    # Em Decimal, uma vez. O config.py já troca nan e inf pelo padrão; o que
    # ainda assim não se lê vira None, e a comparação não quebra o ciclo.
    teto = dinheiro.do_real(config.negocio.teto_compra_automatica)
    enfileiradas = 0

    with conectar() as conn:
        prontos = [(ped, None, None) for ped in conn.execute(
            _SQL_COMPRA + " WHERE p.estado = ?", (Estado.ANALISADO.value,)).fetchall()]
        if not config.modo_simulacao:
            prontos += [(ped, VOLTA_SIMULACAO, None) for ped in _compras_simuladas(conn)]
    prontos += _compras_a_refazer()

    for ped, volta, substituida in prontos:
        falha = _montar_ordem(ped, teto, volta)
        if falha is None:
            enfileiradas += 1
        if substituida is not None:  # só depois de montar, para dizer o que aconteceu
            item_id, causa = substituida
            if falha is None:
                recusa = (f"Substituída pelo worker: {causa} A ordem do pedido {ped['id']} foi "
                          "refeita com os dados atuais do produto e está na fila.")
            else:
                recusa = (f"Recusada pelo worker: {causa} A ordem do pedido {ped['id']} não foi "
                          f"refeita; o pedido foi para PROBLEMA: {falha}")
            aprovacao.recusar(item_id, recusa)

    return enfileiradas


# ---------------------------------------------------- Saída de PROBLEMA
#
# Pedido que foi para PROBLEMA por dado do cadastro (moeda ou custo do
# produto, peso, fornecedor, produto desativado, moeda da venda) não volta
# sozinho: o worker só analisa pedidos NOVO. Corrigido o dado, reanalisar o
# devolve a NOVO, e o próximo ciclo refaz a análise e a ordem com as mesmas
# regras. Nada é presumido: a moeda da venda que o marketplace não informou
# só entra quando você a diz, e fica registrada com o seu nome.

ESTADOS_DEPOIS_DA_COMPRA = (Estado.COMPRA_ENVIADA, Estado.COMPRA_CONFIRMADA,
                            Estado.EM_TRANSITO, Estado.ENTREGUE)
REANALISE_PEDIDA = ("O pedido {id} voltou para NOVO: o worker refaz a análise e a ordem de "
                    "compra no próximo ciclo (ou agora: python cli.py ciclo).")


def _compra_que_impede_reanalise(conn, pedido_id: int) -> str | None:
    """Por que reanalisar este pedido poderia comprar duas vezes, ou None. Só
    volta a NOVO o pedido do qual nenhuma ordem de compra saiu: nunca chegou
    a COMPRA_ENVIADA, não tem compra na fila nem em execução, e nenhuma
    aprovação de compra gravou ordem de verdade (a simulada não conta)."""
    depois = conn.execute(
        f"SELECT para FROM transicoes WHERE pedido_id = ? AND para IN"
        f" ({', '.join('?' * len(ESTADOS_DEPOIS_DA_COMPRA))}) LIMIT 1",
        (pedido_id, *(e.value for e in ESTADOS_DEPOIS_DA_COMPRA))).fetchone()
    if depois is not None:
        return (f"o pedido {pedido_id} já passou por {depois['para']}: a ordem de compra foi "
                "gravada, e reanalisar poderia comprar de novo. Resolva à mão.")
    for compra in conn.execute(
            "SELECT id, status, resultado FROM aprovacoes WHERE pedido_id = ?"
            " AND tipo = 'compra_fornecedor' ORDER BY id", (pedido_id,)):
        if compra["status"] == "pendente":
            return (f"a compra do pedido {pedido_id} está na fila (pendência {compra['id']}); "
                    "aprove-a ou recuse-a antes de reanalisar.")
        if compra["status"] in ("aprovada", "erro") or (
                compra["status"] == "executada" and not aprovacao.foi_simulado(compra["resultado"])):
            return (f"a aprovação de compra {compra['id']} deste pedido ficou '{compra['status']}': "
                    "a ordem de compra pode ter sido gravada (confira ordens_de_compra/), e "
                    "reanalisar poderia comprar de novo. Resolva à mão.")
    return None


def reanalisar(pedido_id: int, dados, ator: str) -> dict:
    """Devolve a NOVO um pedido em PROBLEMA, para o worker refazer a análise
    no próximo ciclo. dados: {"moeda_venda": "BRL"} ou vazio; a moeda da
    venda só é aceita quando o pedido não tem nenhuma (o marketplace não
    informou), e fica registrada como dita por ator. O cli.py (pedido
    reanalisar) e o painel (POST /api/pedidos/{id}/reanalisar) chamam aqui:
    a validação e as mensagens são as mesmas, e os erros são os do cadastro
    (422, 409, 404)."""
    moeda_venda = cadastro.validar_reanalise(dados)
    with conectar() as conn:
        ped = conn.execute("SELECT estado, valor_bruto_moeda FROM pedidos WHERE id = ?",
                           (pedido_id,)).fetchone()
        if ped is None:
            raise cadastro.NaoEncontrado("Pedido não encontrado.")
        if ped["estado"] != Estado.PROBLEMA.value:
            raise cadastro.Conflito("estado", f"Só pedido em PROBLEMA é reanalisado; o pedido "
                                              f"{pedido_id} está em {ped['estado']}.")
        impede = _compra_que_impede_reanalise(conn, pedido_id)
    if impede:
        raise cadastro.Conflito("estado", f"Não dá para reanalisar: {impede}")
    ja_tem = dinheiro.moeda_lida(ped["valor_bruto_moeda"])
    if moeda_venda is not None and ja_tem is not None:
        raise cadastro.ErroValidacao([{
            "campo": "moeda_venda",
            "mensagem": f"o pedido já tem a moeda da venda ({ja_tem}), a que o marketplace "
                        "informou; ela não muda."}])

    motivo = f"Reanálise pedida por {ator}"
    if moeda_venda is not None:
        with conectar() as conn:
            conn.execute("UPDATE pedidos SET valor_bruto_moeda = ?, atualizado_em = ?"
                         " WHERE id = ? AND estado = ?",
                         (moeda_venda, agora(), pedido_id, Estado.PROBLEMA.value))
        registrar_evento("atencao", "pedidos",
                         f"Pedido {pedido_id}: moeda da venda registrada como {moeda_venda} "
                         f"por {ator}; o marketplace não a informou.")
        motivo += f", com a moeda da venda {moeda_venda} informada por {ator}"
    transicionar(pedido_id, Estado.NOVO, motivo, automatico=False)
    return {"pedido_id": pedido_id, "estado": Estado.NOVO.value,
            "moeda_venda": moeda_venda or ja_tem,
            "mensagem": REANALISE_PEDIDA.format(id=pedido_id)}


# ------------------------------------------------------------ 4. Atendimento

def atender_perguntas(ml: MercadoLivre) -> int:
    """Redige (para a fila) as perguntas sem resposta que o bot ainda não
    tratou. A busca traz toda pergunta UNANSWERED a cada ciclo, inclusive as
    que já estão na fila, as escaladas e as aprovadas que não foram
    publicadas (em simulação, ou com a publicação falha): bot.ja_tratada pula
    essas antes de qualquer chamada, e só a falha temporária volta ao modelo
    no ciclo seguinte."""
    try:
        perguntas = ml.perguntas_sem_resposta(limit=30)
    except ErroMercadoLivre as e:
        registrar_evento("erro", "worker", f"Busca de perguntas falhou: {e}")
        return 0

    tratadas = 0
    for q in perguntas:
        if bot.ja_tratada(str(q.get("id"))):
            continue
        item_id = q.get("item_id", "")
        try:
            item = ml.anuncio(item_id)
            contexto = {
                "título": item.get("title", ""),
                "preço": item.get("price", ""),
                "estoque": item.get("available_quantity", ""),
                "condição": item.get("condition", ""),
                "atributos": ", ".join(
                    f"{a.get('name')}={a.get('value_name')}"
                    for a in item.get("attributes", [])[:12] if a.get("value_name")
                ),
            }
        except ErroMercadoLivre:
            contexto = {"título": "(não foi possível carregar a ficha)"}

        bot.processar_pergunta(str(q.get("id")), q.get("text", ""), contexto)
        tratadas += 1
        time.sleep(0.3)

    return tratadas


# --------------------------------------------------------------- 5. Rastreio

def atualizar_rastreio(ml: MercadoLivre) -> int:
    atualizados = 0
    with conectar() as conn:
        em_transito = conn.execute(
            "SELECT id, id_externo, endereco_json FROM pedidos WHERE estado IN (?,?)",
            (Estado.COMPRA_CONFIRMADA.value, Estado.EM_TRANSITO.value),
        ).fetchall()

    for ped in em_transito:
        # O endereço está cifrado desde a ingestão; a leitura passa pela
        # privacidade, que registra o acesso e não levanta com '{}' ou ilegível.
        shipping_id = privacidade.envio_para_rastreio(ped["id"], ped["endereco_json"])
        if not shipping_id:
            continue
        try:
            env = ml.envio(shipping_id)
        except ErroMercadoLivre:
            continue

        status = env.get("status", "")
        rastreio = env.get("tracking_number")
        if rastreio:
            with conectar() as conn:
                conn.execute("UPDATE pedidos SET codigo_rastreio = ? WHERE id = ?",
                             (rastreio, ped["id"]))
        try:
            if status == "delivered":
                transicionar(ped["id"], Estado.ENTREGUE, "Confirmado pelo ML")
                atualizados += 1
            elif status == "shipped":
                transicionar(ped["id"], Estado.EM_TRANSITO, f"Rastreio {rastreio}")
                atualizados += 1
        except Exception:
            pass  # transição inválida = já está no estado certo

    return atualizados


# ------------------------------------------------------------------- Executores

AVISO_SIMULACAO_OC = ("SIMULAÇÃO (MODO_SIMULACAO=true): ordem de teste. "
                      "Não envie ao fornecedor.\n\n")


def _fornecedor_da_ordem(payload: dict) -> tuple:
    """(nome, canal, contato) do fornecedor da ordem, como estão no cadastro
    agora, achado pelo id gravado na fila: editar o nome, o canal ou o contato
    do mesmo fornecedor depois de a ordem entrar na fila não deixa o arquivo
    nem a instrução com os dados velhos (a conformidade também olha o canal
    de agora). Fila de versão anterior, sem o id, ou fornecedor que não
    existe mais: os dados copiados na ordem."""
    copiado = (payload.get("fornecedor"), payload.get("canal"), payload.get("contato"))
    fornecedor_id = payload.get("fornecedor_id")
    if not isinstance(fornecedor_id, int) or isinstance(fornecedor_id, bool):
        return copiado
    with conectar() as conn:
        atual = conn.execute("SELECT nome, canal, contato FROM fornecedores WHERE id = ?",
                             (fornecedor_id,)).fetchone()
    return copiado if atual is None else (atual["nome"], atual["canal"], atual["contato"])


def executor_compra(payload: dict) -> str:
    """
    Executa a compra depois de aprovada: grava a ordem de compra em
    ordens_de_compra/oc_<pedido>.txt, na pasta de dados. O programa NÃO envia
    nada ao fornecedor: a ordem sai do arquivo pelas suas mãos, pelo canal
    cadastrado no fornecedor. O nome, o canal e o contato são os do cadastro
    na hora da aprovação (_fornecedor_da_ordem).

    A maioria das fábricas não tem API. O caminho realista para automatizar
    depois é mandar o pedido pelo canal que o fornecedor usa (e-mail por SMTP,
    WhatsApp Business API); nada disso existe ainda.

    Com MODO_SIMULACAO ligado, o arquivo sai marcado como teste e o resultado
    leva [SIMULAÇÃO]. O pedido NÃO anda: nada foi comprado, então ele fica em
    AGUARDANDO_APROVACAO (um estado que pede atenção) e volta para a fila
    quando MODO_SIMULACAO for desligado (montar_ordens_de_compra). A ordem de
    verdade, gravada depois, substitui o arquivo de teste. Sem simulação, o
    pedido vai para COMPRA_ENVIADA.
    """
    simulacao = config.modo_simulacao
    # Fila antiga traz o valor como número e sem moeda; a nova, texto e moeda.
    valor = dinheiro.formatar(dinheiro.do_real(payload["valor"]),
                              dinheiro.moeda_lida(payload.get("moeda")))
    nome, canal, contato = _fornecedor_da_ordem(payload)
    canal = canal or "canal não cadastrado"
    texto = (
        f"PEDIDO DE COMPRA\n"
        f"Fornecedor: {nome}\n"
        f"Canal: {canal}; contato: {contato or 'não cadastrado'}\n"
        f"SKU: {payload['sku']} — {payload['produto']}\n"
        f"Quantidade: {payload['quantidade']}\n"
        f"Valor: {valor}\n"
        f"Referência: {payload['marketplace']}#{payload['pedido_externo']}\n"
    )
    if simulacao:
        texto = AVISO_SIMULACAO_OC + texto
    PASTA_ORDENS.mkdir(parents=True, exist_ok=True)
    arquivo = PASTA_ORDENS / f"oc_{payload['pedido_id']}.txt"
    arquivo.write_text(texto, encoding="utf-8")

    if simulacao:
        registrar_evento("info", "worker",
                         f"Pedido {payload['pedido_id']}: compra simulada ({arquivo.name}). "
                         "O pedido continua em AGUARDANDO_APROVACAO e volta para a fila "
                         "quando MODO_SIMULACAO for desligado.")
        return aprovacao.simular(
            f"Ordem de compra gravada em {arquivo}, marcada como teste. Nada foi enviado "
            "ao fornecedor. O pedido continua esperando a compra de verdade: volta para a "
            "fila quando MODO_SIMULACAO for desligado.")

    transicionar(payload["pedido_id"], Estado.COMPRA_ENVIADA,
                 f"Ordem gerada em {arquivo.name}; o envio ao fornecedor é manual",
                 automatico=False)
    return (f"Ordem de compra gravada em {arquivo}. Envie ao fornecedor "
            f"{nome} pelo canal {canal}: o programa não envia.")


def executor_resposta(payload: dict) -> str:
    if config.modo_simulacao:
        return aprovacao.simular(f"Resposta à pergunta {payload['question_id']} não "
                                 "publicada: nada foi enviado ao marketplace.")
    ml = MercadoLivre()
    ml.responder_pergunta(payload["question_id"], payload["resposta"])
    return f"Resposta publicada na pergunta {payload['question_id']}"


def executor_preco(payload: dict) -> str:
    if config.modo_simulacao:
        return aprovacao.simular(f"Preço do {payload['item_id']} seria R$ "
                                 f"{payload['preco']:.2f}; nada foi enviado ao marketplace.")
    ml = MercadoLivre()
    ml.atualizar_preco(payload["item_id"], payload["preco"])
    return f"Preço do {payload['item_id']} atualizado para R$ {payload['preco']:.2f}"


EXECUTORES = {
    "compra_fornecedor": executor_compra,
    "resposta_cliente": executor_resposta,
    "ajuste_preco": executor_preco,
}


# ------------------------------------------------------------------ Laço

def _pilha(erro: Exception) -> str:
    """Arquivo, linha e código de cada quadro, sem a mensagem da exceção."""
    quadros = traceback.format_list(traceback.extract_tb(erro.__traceback__))
    return f"{type(erro).__name__}\n{''.join(quadros)}"[-2000:]


# ------------------------------------------------------- Um ciclo de cada vez
#
# Duas passadas ao mesmo tempo (a tecla 'c' do painel durante o ciclo do laço
# de fundo do executar.py, a tecla apertada duas vezes, ou um `cli.py ciclo`
# ao lado do painel) leem o mesmo estado antes de qualquer uma gravar: a
# mesma pergunta iria duas vezes ao modelo (duas chamadas pagas) e a mesma
# compra entraria duas vezes na fila. Entre threads deste processo vale um
# Lock; entre processos, o primeiro byte de um arquivo vazio ao lado do banco
# (.trava_ciclo), que o sistema solta sozinho se o processo morrer. Quem chega
# com um ciclo em andamento não espera: pula a passada.

CICLO_EM_ANDAMENTO = ("Já há um ciclo em andamento (neste processo ou em outro, como um "
                      "cli.py rodar ao lado); esta passada foi pulada.")

_trava_ciclo = threading.Lock()


def _arquivo_trava_ciclo() -> Path:
    """Ao lado do banco, que é o que as passadas disputam."""
    return Path(config.db_path).resolve().parent / ".trava_ciclo"


def _travar_arquivo_do_ciclo() -> int | None:
    """O descritor com a trava na mão, ou None se outro processo está no ciclo."""
    fd = os.open(_arquivo_trava_ciclo(), os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0),
                 0o600)
    try:
        _trancar(fd)
    except OSError:
        os.close(fd)
        return None
    return fd


@contextmanager
def _um_ciclo_por_vez():
    """Entrega True com a vez de rodar, ou False se outro ciclo está em andamento."""
    if not _trava_ciclo.acquire(blocking=False):
        yield False
        return
    fd = None
    try:
        fd = _travar_arquivo_do_ciclo()
        yield fd is not None
    finally:
        if fd is not None:
            _soltar_arquivo(fd)
        _trava_ciclo.release()


def ciclo() -> dict:
    """Uma passada completa. Cada etapa é isolada: falha numa não para as outras.

    Com outro ciclo em andamento, neste processo ou em outro, não roda nada e
    devolve {"pulado": CICLO_EM_ANDAMENTO}."""
    with _um_ciclo_por_vez() as vez:
        if not vez:
            return {"pulado": CICLO_EM_ANDAMENTO}
        return _passada()


def _passada() -> dict:
    """As cinco etapas, com a vez de rodar já garantida por ciclo()."""
    inicializar()
    privacidade.criar_tabelas_lgpd()  # o rastreio registra o acesso em acessos_pii
    resumo = {}
    ml = MercadoLivre()

    for nome, func in [
        ("ingeridos", lambda: ingerir_mercadolivre(ml)),
        ("analisados", analisar_novos),
        ("ordens_montadas", montar_ordens_de_compra),
        ("perguntas", lambda: atender_perguntas(ml)),
        ("rastreios", lambda: atualizar_rastreio(ml)),
    ]:
        try:
            resumo[nome] = func()
        except Exception as e:
            # Só o tipo e onde falhou, nunca a mensagem: a de uma exceção
            # inesperada pode repetir um valor decifrado do cofre.
            resumo[nome] = f"erro: {type(e).__name__}"
            registrar_evento("erro", "worker", f"Etapa '{nome}' falhou: {type(e).__name__}",
                             {"traceback": _pilha(e)})

    resumo["pendentes_aprovacao"] = len(aprovacao.pendentes())
    return resumo


def rodar(intervalo_seg: int | None = None):
    """Laço contínuo. Sem intervalo, vale INTERVALO_WORKER (padrão 300 s)."""
    intervalo_seg = intervalo_seg or config.intervalo_worker
    modo = " em modo simulação" if config.modo_simulacao else ""
    registrar_evento("info", "worker", f"Worker iniciado{modo}, ciclo a cada {intervalo_seg}s")
    while True:
        r = ciclo()
        print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {r}")
        time.sleep(intervalo_seg)


def principal():
    """`python worker.py`: sem AGENTE_DADOS, diz qual pasta de dados vai ser
    usada antes da primeira gravação, e roda o laço."""
    avisar_pasta_de_dados()
    rodar()


if __name__ == "__main__":
    principal()
