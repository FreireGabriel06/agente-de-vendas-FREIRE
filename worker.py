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
from pathlib import Path

from config import config, DATA_DIR, avisar_pasta_de_dados
from db import conectar, agora, inicializar, registrar_evento
from core import aprovacao, conformidade, privacidade
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

            # LGPD art. 46: PII cifrada já na entrada. Em nenhum momento o
            # nome ou o identificador do comprador toca o disco em texto puro.
            conn.execute(
                "INSERT INTO pedidos (marketplace, id_externo, produto_id, quantidade,"
                " valor_bruto, estado, comprador_nome, comprador_id, endereco_json,"
                " criado_em, atualizado_em) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (p["marketplace"], p["id_externo"], produto["id"] if produto else None,
                 p["quantidade"], p["valor_bruto"], Estado.NOVO.value,
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

def analisar_novos() -> int:
    """Calcula a margem real de cada pedido novo e decide o destino."""
    cfg = config.negocio
    processados = 0

    with conectar() as conn:
        pendentes = conn.execute(
            "SELECT p.*, pr.custo_fornecedor, pr.peso_kg, pr.titulo AS titulo_produto,"
            " pr.fornecedor_id FROM pedidos p"
            " LEFT JOIN produtos pr ON pr.id = p.produto_id"
            " WHERE p.estado = ?", (Estado.NOVO.value,),
        ).fetchall()

    for ped in pendentes:
        if ped["custo_fornecedor"] is None:
            transicionar(ped["id"], Estado.PROBLEMA,
                         "SKU não cadastrado em produtos — sem custo pra calcular margem")
            continue

        custo_total = ped["custo_fornecedor"] * ped["quantidade"]
        comp = precificacao.calcular(
            preco_venda=ped["valor_bruto"],
            custo_produto=custo_total,
            peso_kg=ped["peso_kg"] or 0.3,
            aliquota_imposto_pct=cfg.aliquota_imposto_pct,
            margem_minima_pct=cfg.margem_minima_pct,
        )

        with conectar() as conn:
            conn.execute(
                "UPDATE pedidos SET custo_previsto = ?, margem_prevista = ? WHERE id = ?",
                (custo_total, comp.margem_pct, ped["id"]),
            )

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
    "SELECT p.*, pr.sku, pr.titulo AS titulo_produto, pr.custo_fornecedor,"
    " f.nome AS fornecedor_nome, f.canal, f.contato, f.prazo_dias"
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
    aqui, com a mesma checagem.
    """
    teto = config.negocio.teto_compra_automatica
    enfileiradas = 0

    with conectar() as conn:
        prontos = conn.execute(_SQL_COMPRA + " WHERE p.estado = ?",
                               (Estado.ANALISADO.value,)).fetchall()
        if not config.modo_simulacao:
            prontos += _compras_simuladas(conn)

    for ped in prontos:
        de_novo = ped["estado"] == Estado.AGUARDANDO_APROVACAO.value
        if ped["fornecedor_nome"] is None:
            transicionar(ped["id"], Estado.PROBLEMA, "Produto sem fornecedor vinculado")
            continue

        # Prazo que não é número: a conformidade abaixo bloqueia pedindo o prazo.
        prazo = conformidade.prazo_do_fornecedor(ped["prazo_dias"])
        if prazo is not None and prazo > conformidade.PRAZO_MAXIMO_SEM_ANUNCIO:
            transicionar(ped["id"], Estado.PROBLEMA,
                         f"Prazo do fornecedor ({prazo}d) tende a estourar o do anúncio")
            continue

        valor = ped["custo_previsto"]

        payload = {
            "pedido_id": ped["id"],
            "fornecedor": ped["fornecedor_nome"],
            "canal": ped["canal"],
            "contato": ped["contato"],
            "sku": ped["sku"],
            "produto": ped["titulo_produto"],
            "quantidade": ped["quantidade"],
            "valor": valor,
            "marketplace": ped["marketplace"],
            "pedido_externo": ped["id_externo"],
            "margem_prevista": ped["margem_prevista"],
        }

        # Conformidade ANTES de enfileirar, a mesma da aprovação. Violação dura
        # nem chega à fila — evita que você aprove por reflexo algo que
        # suspende a conta.
        checagem = conformidade.verificar_pendencia("compra_fornecedor", payload)
        if checagem.bloqueado and not checagem.so_falta_confirmar:
            transicionar(ped["id"], Estado.PROBLEMA,
                         f"Bloqueado pela conformidade — {checagem.resumo}")
            continue

        # O rótulo é só informação: as duas saem da mesma fila, com o seu OK.
        urgencia = "ROTINA" if valor <= teto else "ACIMA DO TETO"
        aprovacao.enfileirar(
            tipo="compra_fornecedor",
            resumo=(f"[{urgencia}] Comprar {ped['quantidade']}x {ped['sku']} de "
                    f"{ped['fornecedor_nome']} por R$ {valor:.2f} "
                    f"(margem {ped['margem_prevista']}%)"),
            payload=payload,
            pedido_id=ped["id"],
            valor=valor,
        )
        motivo = f"Ordem de compra montada, aguardando OK ({urgencia})"
        if checagem.bloqueado:
            motivo += f"; bloqueada até confirmar — {checagem.resumo}"
        if de_novo:
            # Já está em AGUARDANDO_APROVACAO: não há transição, só o aviso.
            registrar_evento("atencao", "worker",
                             f"Pedido {ped['id']} voltou para a fila: a aprovação anterior "
                             f"foi em modo simulação e nada foi comprado. {motivo}")
        else:
            transicionar(ped["id"], Estado.AGUARDANDO_APROVACAO, motivo)
        enfileiradas += 1

    return enfileiradas


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


def executor_compra(payload: dict) -> str:
    """
    Executa a compra depois de aprovada: grava a ordem de compra em
    ordens_de_compra/oc_<pedido>.txt, na pasta de dados. O programa NÃO envia
    nada ao fornecedor: a ordem sai do arquivo pelas suas mãos, pelo canal
    cadastrado no fornecedor.

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
    texto = (
        f"PEDIDO DE COMPRA\n"
        f"Fornecedor: {payload['fornecedor']}\n"
        f"SKU: {payload['sku']} — {payload['produto']}\n"
        f"Quantidade: {payload['quantidade']}\n"
        f"Valor: R$ {payload['valor']:.2f}\n"
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
    canal = payload.get("canal") or "canal não cadastrado"
    return (f"Ordem de compra gravada em {arquivo}. Envie ao fornecedor "
            f"{payload['fornecedor']} pelo canal {canal}: o programa não envia.")


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
