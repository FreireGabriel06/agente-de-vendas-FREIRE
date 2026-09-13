"""
Worker: o laço principal do robô.

Roda a cada N minutos e executa, em ordem:

  1. Ingestão        — puxa pedidos novos dos marketplaces
  2. Análise         — calcula margem real; recusa o que não fecha
  3. Roteamento      — abaixo do teto e com margem boa, prepara compra;
                       acima do teto, enfileira pra sua aprovação
  4. Atendimento     — redige respostas às perguntas abertas (pra fila)
  5. Rastreio        — atualiza status de quem já está em trânsito

Nenhuma etapa gasta dinheiro ou publica texto. Isso só acontece quando você
aprova na fila. É isso que mantém o robô seguro rodando desatendido.
"""
import json
import time
import traceback
from datetime import datetime, timedelta, timezone

from config import config
from db import conectar, agora, inicializar, registrar_evento
from core import aprovacao, conformidade, privacidade
from core.estados import Estado, transicionar
from inteligencia import precificacao
from conectores.mercadolivre import MercadoLivre, ErroMercadoLivre
from atendimento.bot import processar_pergunta


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

def montar_ordens_de_compra() -> int:
    """
    Monta a ordem de compra pro fornecedor e joga na fila de aprovação.
    Nada é enviado aqui — a compra só sai quando você aprova.
    """
    teto = config.negocio.teto_compra_automatica
    enfileiradas = 0

    with conectar() as conn:
        prontos = conn.execute(
            "SELECT p.*, pr.sku, pr.titulo AS titulo_produto, pr.custo_fornecedor,"
            " f.nome AS fornecedor_nome, f.canal, f.contato, f.prazo_dias"
            " FROM pedidos p"
            " JOIN produtos pr ON pr.id = p.produto_id"
            " LEFT JOIN fornecedores f ON f.id = pr.fornecedor_id"
            " WHERE p.estado = ?", (Estado.ANALISADO.value,),
        ).fetchall()

    for ped in prontos:
        if ped["fornecedor_nome"] is None:
            transicionar(ped["id"], Estado.PROBLEMA, "Produto sem fornecedor vinculado")
            continue

        prazo = ped["prazo_dias"] or config.negocio.prazo_fornecedor_dias
        if prazo > 7:
            transicionar(ped["id"], Estado.PROBLEMA,
                         f"Prazo do fornecedor ({prazo}d) tende a estourar o do anúncio")
            continue

        valor = ped["custo_previsto"]

        # Conformidade ANTES de enfileirar. Se a regra barra, a ação nem chega
        # à fila — evita que você aprove por reflexo algo que suspende a conta.
        ctx = {
            "marketplace": ped["marketplace"],
            "envio_direto_fornecedor": bool(ped["canal"] in ("api", "portal")),
            "reembalagem_confirmada": ped["marketplace"] != "amazon",
            "prazo_fornecedor_dias": prazo,
            "prazo_anuncio_dias": ped["prazo_anuncio_dias"] if "prazo_anuncio_dias" in ped.keys() else None,
            "margem_pct": ped["margem_prevista"],
        }
        checagem = conformidade.verificar(ctx)
        if checagem.bloqueado:
            transicionar(ped["id"], Estado.PROBLEMA,
                         f"Bloqueado pela conformidade — {checagem.resumo}")
            continue

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
        transicionar(ped["id"], Estado.AGUARDANDO_APROVACAO,
                     f"Ordem de compra montada, aguardando OK ({urgencia})")
        enfileiradas += 1

    return enfileiradas


# ------------------------------------------------------------ 4. Atendimento

def atender_perguntas(ml: MercadoLivre) -> int:
    try:
        perguntas = ml.perguntas_sem_resposta(limit=30)
    except ErroMercadoLivre as e:
        registrar_evento("erro", "worker", f"Busca de perguntas falhou: {e}")
        return 0

    tratadas = 0
    for q in perguntas:
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

        processar_pergunta(str(q.get("id")), q.get("text", ""), contexto)
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
        shipping_id = json.loads(ped["endereco_json"] or "{}").get("shipping_id")
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

def executor_compra(payload: dict) -> str:
    """
    Executa a compra depois de aprovada.

    A maioria das fábricas não tem API. O caminho realista é gerar o pedido
    formatado e mandar pelo canal que o fornecedor usa. Aqui ele é gravado em
    arquivo; troque por envio de e-mail (SMTP) ou WhatsApp Business API
    conforme o canal cadastrado no fornecedor.
    """
    from pathlib import Path
    texto = (
        f"PEDIDO DE COMPRA\n"
        f"Fornecedor: {payload['fornecedor']}\n"
        f"SKU: {payload['sku']} — {payload['produto']}\n"
        f"Quantidade: {payload['quantidade']}\n"
        f"Valor: R$ {payload['valor']:.2f}\n"
        f"Referência: {payload['marketplace']}#{payload['pedido_externo']}\n"
    )
    destino = Path(config.db_path).parent / "ordens_de_compra"
    destino.mkdir(exist_ok=True)
    arquivo = destino / f"oc_{payload['pedido_id']}.txt"
    arquivo.write_text(texto, encoding="utf-8")

    transicionar(payload["pedido_id"], Estado.COMPRA_ENVIADA,
                 f"Ordem gerada em {arquivo.name}", automatico=False)
    return f"Ordem de compra gravada em {arquivo}"


def executor_resposta(payload: dict) -> str:
    ml = MercadoLivre()
    ml.responder_pergunta(payload["question_id"], payload["resposta"])
    return f"Resposta publicada na pergunta {payload['question_id']}"


def executor_preco(payload: dict) -> str:
    ml = MercadoLivre()
    ml.atualizar_preco(payload["item_id"], payload["preco"])
    return f"Preço do {payload['item_id']} atualizado para R$ {payload['preco']:.2f}"


EXECUTORES = {
    "compra_fornecedor": executor_compra,
    "resposta_cliente": executor_resposta,
    "ajuste_preco": executor_preco,
}


# ------------------------------------------------------------------ Laço

def ciclo() -> dict:
    """Uma passada completa. Cada etapa é isolada: falha numa não para as outras."""
    inicializar()
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
            resumo[nome] = f"erro: {e}"
            registrar_evento("erro", "worker", f"Etapa '{nome}' falhou: {e}",
                             {"traceback": traceback.format_exc()[:2000]})

    resumo["pendentes_aprovacao"] = len(aprovacao.pendentes())
    return resumo


def rodar(intervalo_seg: int = 300):
    registrar_evento("info", "worker", "Worker iniciado")
    while True:
        r = ciclo()
        print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {r}")
        time.sleep(intervalo_seg)


if __name__ == "__main__":
    rodar()
