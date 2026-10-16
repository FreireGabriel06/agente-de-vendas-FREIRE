"""
Rotas da operação: fila de aprovação, pedidos, comprador, preço, nichos,
LGPD, saúde, eventos e ciclo manual.
"""
from decimal import Decimal, InvalidOperation

from fastapi import Query, Request, HTTPException
from starlette.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from config import config
from db import conectar, registrar_evento
from core import aprovacao, conformidade, privacidade
from core.erros import mensagem_segura
from core.estados import ESTADOS_CRITICOS, historico
from inteligencia import precificacao, tendencias
from fastapi import APIRouter

rotas = APIRouter()

# ------------------------------------------------------------------ Dados

def _pendencias_com_conformidade() -> list[dict]:
    itens = []
    for a in aprovacao.pendentes():
        # A mesma checagem que core/aprovacao.aprovar faz antes de executar.
        res = aprovacao.checar(a)
        itens.append({
            "id": a.id,
            "tipo": a.tipo,
            "resumo": a.resumo,
            "valor": a.valor,
            # A moeda do valor, como a ordem a gravou; None é "moeda não
            # informada" (fila de versão anterior, ou item sem dinheiro).
            "moeda": a.payload.get("moeda"),
            "margem": a.payload.get("margem_prevista"),
            "marketplace": a.payload.get("marketplace", ""),
            "bloqueado": res.bloqueado,
            "motivo": conformidade.motivo_do_bloqueio(res) if res.bloqueado else "",
            "violacoes": [
                {"regra": v.regra, "severidade": v.severidade.value,
                 "mensagem": v.mensagem, "saida": v.saida, "fonte": v.fonte,
                 "confirmacao": v.confirmacao}
                for v in res.violacoes
            ],
            "reversivel": a.tipo != "compra_fornecedor",
        })
    return itens


@rotas.get("/api/pendencias")
def api_pendencias():
    itens = _pendencias_com_conformidade()
    liberadas = [i for i in itens if not i["bloqueado"]]
    return {
        "itens": itens,
        "total": len(itens),
        "bloqueadas": len(itens) - len(liberadas),
        "exposicao": round(sum(i["valor"] or 0 for i in liberadas), 2),
        "modo_simulacao": config.modo_simulacao,
    }


@rotas.get("/api/pedidos")
def api_pedidos(estado: str | None = None):
    """valor_bruto é o espelho REAL antigo; valor_bruto_dec e valor_bruto_moeda
    são o valor decimal e a moeda que o marketplace informou (None: moeda não
    informada)."""
    sql = ("SELECT id, marketplace, id_externo, valor_bruto, valor_bruto_dec, valor_bruto_moeda,"
           " margem_prevista, estado, codigo_rastreio, criado_em FROM pedidos")
    params = ()
    if estado:
        sql += " WHERE estado = ?"
        params = (estado,)
    sql += " ORDER BY id DESC LIMIT 200"
    with conectar() as conn:
        linhas = conn.execute(sql, params).fetchall()
    return {"pedidos": [dict(l) for l in linhas],
            "criticos": [e.value for e in ESTADOS_CRITICOS]}


@rotas.get("/api/pedido/{pedido_id}")
def api_pedido(pedido_id: int):
    with conectar() as conn:
        p = conn.execute("SELECT * FROM pedidos WHERE id = ?", (pedido_id,)).fetchone()
    if not p:
        raise HTTPException(404, "Pedido não encontrado")
    d = dict(p)
    # PII nunca vai pra tela inteira sem motivo. Mascarado por padrão.
    d["comprador_nome"] = privacidade.mascarar(
        privacidade.decifrar(p["comprador_nome"]), visivel=4)
    d.pop("comprador_id", None)
    d.pop("endereco_json", None)
    d["historico"] = [dict(h) for h in historico(pedido_id)]
    return d


@rotas.get("/api/pedido/{pedido_id}/comprador")
def api_comprador(pedido_id: int, request: Request):
    """Revela o PII sob demanda, e registra o acesso. LGPD art. 6º, X.

    O registro guarda o operador que pediu, não o nome do sistema: sem isso
    a trilha não responde quem viu o dado do comprador.
    """
    return privacidade.ler_comprador(pedido_id, ator=request.state.operador,
                                     finalidade="conferência de entrega pelo operador")


@rotas.post("/api/aprovar/{aprovacao_id}")
def api_aprovar(aprovacao_id: int, request: Request):
    """A conformidade é conferida dentro de aprovacao.aprovar, a mesma porta do
    cli.py: bloqueado volta 409 com o mesmo motivo que o cli.py imprime."""
    from worker import EXECUTORES
    if aprovacao.obter_pendente(aprovacao_id) is None:
        raise HTTPException(404, "Pendência não encontrada")
    try:
        resultado = aprovacao.aprovar(aprovacao_id, EXECUTORES)
    except aprovacao.BloqueadoPelaConformidade as e:
        raise HTTPException(409, str(e)) from None
    except aprovacao.JaDecidida:
        raise HTTPException(409, "Esta pendência já foi decidida por outra chamada.") from None
    except Exception as e:
        # Sem corpo de resposta do marketplace nem mensagem de exceção
        # inesperada na tela (achado S-06); o evento guarda a pilha.
        raise HTTPException(500, mensagem_segura(e)) from None
    registrar_evento("info", "aprovacao",
                     f"Ação {aprovacao_id} liberada por {request.state.operador}")
    return {"ok": True, "resultado": resultado, "simulado": aprovacao.foi_simulado(resultado)}


@rotas.post("/api/recusar/{aprovacao_id}")
async def api_recusar(aprovacao_id: int, request: Request):
    corpo = await request.json() if await request.body() else {}
    motivo = corpo.get("motivo", "recusado no painel")
    if aprovacao.obter_pendente(aprovacao_id) is None:
        raise HTTPException(404, "Pendência não encontrada")
    try:
        aprovacao.recusar(aprovacao_id, f"{motivo} (por {request.state.operador})")
    except aprovacao.JaDecidida:
        raise HTTPException(409, "Esta pendência já foi decidida por outra chamada.") from None
    return {"ok": True}


@rotas.post("/api/nichos")
async def api_nichos(request: Request):
    corpo = await request.json()
    try:
        termos = tendencias.validar_termos(corpo.get("termos", []) if isinstance(corpo, dict) else None)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    if not termos:
        raise HTTPException(400, "Informe pelo menos um termo")
    ops = await run_in_threadpool(tendencias.analisar, termos)
    return {"oportunidades": [
        {"termo": o.termo, "demanda": o.demanda, "tendencia": o.tendencia_pct,
         "concorrencia": o.concorrencia, "preco": o.preco_mediano, "score": o.score}
        for o in ops]}


def _numero(corpo: dict, nome: str, padrao, minimo: str, maximo: str) -> Decimal:
    """Número finito dentro da faixa, ou 422. Sem isso, "nan" ou um texto
    viravam erro 500 (achado S-13)."""
    valor = corpo.get(nome, padrao)
    try:
        if isinstance(valor, bool) or not isinstance(valor, (int, float, str)):
            raise InvalidOperation
        numero = Decimal(str(valor).strip().replace(",", "."))
    except InvalidOperation:
        raise HTTPException(422, f"{nome}: informe um número.") from None
    if not numero.is_finite() or not Decimal(minimo) <= numero <= Decimal(maximo):
        raise HTTPException(422, f"{nome}: use um número de {minimo} a {maximo}.")
    return numero


@rotas.post("/api/preco")
async def api_preco(request: Request):
    c = await request.json()
    if not isinstance(c, dict):
        raise HTTPException(422, "Envie um objeto JSON.")
    tipo = c.get("tipo", "classico")
    if tipo not in precificacao.COMISSAO_PADRAO:
        raise HTTPException(422, f"tipo: use {' ou '.join(precificacao.COMISSAO_PADRAO)}.")
    try:
        r = precificacao.sugerir_preco(
            custo_produto=_numero(c, "custo", None, "0.01", "1000000"),
            margem_alvo_pct=_numero(c, "margem", 25, "0", "90"),
            peso_kg=_numero(c, "peso", "0.3", "0", "1000"),
            tipo_anuncio=tipo,
        )
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    return r.como_dict()


@rotas.get("/api/lgpd")
def api_lgpd():
    with conectar() as conn:
        acessos = conn.execute(
            "SELECT operacao, ator, finalidade, ocorrido_em FROM acessos_pii"
            " ORDER BY id DESC LIMIT 50").fetchall()
        com_pii = conn.execute(
            "SELECT COUNT(*) c FROM pedidos WHERE comprador_nome IS NOT NULL").fetchone()["c"]
    return {
        "registros_com_pii": com_pii,
        "retencao_dias": privacidade.RETENCAO_DIAS,
        "base_legal": "Execução de contrato — LGPD art. 7º, V",
        "solicitacoes_vencendo": [dict(s) for s in privacidade.solicitacoes_vencendo()],
        "acessos": [dict(a) for a in acessos],
    }


@rotas.post("/api/lgpd/expurgar")
def api_expurgar():
    return {"expurgados": privacidade.expurgar()}


@rotas.get("/saude")
def saude_publica():
    """Para monitor externo, sem login: só o status (core/saude.py). Falha
    responde 503, para o monitor alarmar; os detalhes ficam em /api/saude."""
    from core import saude
    estado = saude.verificar()["status"]
    return JSONResponse({"status": estado}, status_code=503 if estado == "falha" else 200)


@rotas.get("/api/saude")
def api_saude():
    from core import saude
    return saude.verificar()


@rotas.get("/api/eventos")
def api_eventos(n: int = Query(40, ge=1, le=200)):
    with conectar() as conn:
        linhas = conn.execute(
            "SELECT nivel, origem, mensagem, ocorrido_em, ator FROM eventos"
            " ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    return {"eventos": [dict(l) for l in linhas]}


@rotas.post("/api/ciclo")
async def api_ciclo():
    """Um ciclo de cada vez (worker.ciclo): com outro em andamento, como o do
    worker de fundo, volta 409 e nada roda."""
    from worker import ciclo
    # run_in_threadpool copia o contexto: os eventos do ciclo saem com o
    # operador como ator, e não como "sistema".
    resumo = await run_in_threadpool(ciclo)
    if "pulado" in resumo:
        raise HTTPException(409, resumo["pulado"])
    return resumo
