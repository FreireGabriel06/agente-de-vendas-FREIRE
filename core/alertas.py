"""
Alertas (lote 8): estoque baixo, sincronização com falha, conexão vencida e
dado velho. Todo alerta diz o horário do último dado válido.

`avaliar()` olha o estado atual e reconcilia a tabela `alertas`: abre o que
apareceu (um aberto por tipo e chave, sem duplicar), atualiza o texto do que
continua e fecha o que sumiu. Roda a cada ciclo do worker.
"""
from datetime import datetime, timedelta, timezone

from config import config
from core import sincronizacao
from db import agora, conectar, registrar_evento

ROTULOS = {
    sincronizacao.FONTE: "pedidos da Amazon",
    sincronizacao.FONTE_ANUNCIOS: "anúncios da Amazon",
    sincronizacao.FONTE_ESTOQUE_FBA: "estoque FBA da Amazon",
}


def _quando(iso: str | None) -> str:
    return iso if iso else "nenhum até agora"


def _ler_data(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        momento = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return momento if momento.tzinfo else momento.replace(tzinfo=timezone.utc)


def condicoes(agora_utc: datetime | None = None) -> dict[tuple[str, str], tuple[str, str | None]]:
    """(tipo, chave) -> (mensagem, último dado válido) do que está errado agora."""
    agora_utc = agora_utc or datetime.now(timezone.utc)
    limite_velho = agora_utc - timedelta(hours=config.alerta_dado_velho_horas)
    achadas: dict[tuple[str, str], tuple[str, str | None]] = {}

    for s in sincronizacao.estados():
        fonte, ultimo = s["fonte"], s.get("ultimo_sucesso")
        rotulo = ROTULOS.get(fonte, fonte)
        if s.get("resultado") == "conexao_vencida":
            achadas[("conexao_vencida", "amazon")] = (
                "A Amazon recusou a autorização. Autorize o app de novo pelo painel. Se o "
                "vendedor revogou o acesso, apague os dados da Amazon em até 30 dias: "
                "python cli.py amazon apagar-dados --motivo revogado. "
                f"Último dado válido: {_quando(ultimo)}.", ultimo)
        elif s.get("resultado") == "erro":
            achadas[("sincronizacao_falhou", fonte)] = (
                f"A sincronização de {rotulo} falhou: {s.get('detalhe') or 'sem detalhe'}. "
                f"Último dado válido: {_quando(ultimo)}.", ultimo)
        momento = _ler_data(ultimo)
        if (momento is not None and momento < limite_velho
                and s.get("resultado") != "indisponivel"):
            achadas[("dado_velho", fonte)] = (
                f"Os {rotulo} não são atualizados há mais de {config.alerta_dado_velho_horas} h. "
                f"Último dado válido: {ultimo}.", ultimo)

    # Estoque baixo só com uma leitura de anúncios bem-sucedida: sem ela, o
    # número guardado pode ser velho, e quem avisa é o alerta de dado velho.
    anuncios = sincronizacao.estado(sincronizacao.FONTE_ANUNCIOS) or {}
    if anuncios.get("ultimo_sucesso"):
        with conectar() as conn:
            linhas = conn.execute(
                "SELECT sku, titulo, status, quantidade_propria, fba_disponivel, visto_em,"
                " estoque_fba_em FROM amazon_anuncios").fetchall()
        for a in linhas:
            if not (a["status"] or "").strip():
                continue  # anúncio inativo: não vende, não falta
            conhecidas = [q for q in (a["quantidade_propria"], a["fba_disponivel"]) if q is not None]
            if not conhecidas:
                continue  # quantidade não informada não é zero
            disponivel = sum(conhecidas)
            if disponivel <= config.estoque_minimo:
                ultimo = max(d for d in (a["visto_em"], a["estoque_fba_em"]) if d) \
                    if (a["visto_em"] or a["estoque_fba_em"]) else None
                nome = f"{a['sku']} ({a['titulo']})" if a["titulo"] else a["sku"]
                achadas[("estoque_baixo", a["sku"])] = (
                    f"Estoque baixo em {nome}: {disponivel} disponível(is), mínimo "
                    f"{config.estoque_minimo}. Último dado válido: {_quando(ultimo)}.", ultimo)
    return achadas


def avaliar(agora_utc: datetime | None = None) -> dict:
    """Abre, atualiza e fecha alertas. Devolve as contagens."""
    achadas = condicoes(agora_utc)
    abertos = resolvidos = 0
    with conectar() as conn:
        atuais = {(l["tipo"], l["chave"]): l for l in conn.execute(
            "SELECT id, tipo, chave, mensagem FROM alertas WHERE resolvido_em IS NULL")}
        for chave, (mensagem, ultimo) in achadas.items():
            if chave in atuais:
                conn.execute("UPDATE alertas SET mensagem = ?, ultimo_dado_valido = ?,"
                             " atualizado_em = ? WHERE id = ?",
                             (mensagem, ultimo, agora(), atuais[chave]["id"]))
                continue
            conn.execute("INSERT INTO alertas (tipo, chave, mensagem, ultimo_dado_valido,"
                         " aberto_em, atualizado_em) VALUES (?,?,?,?,?,?)",
                         (*chave, mensagem, ultimo, agora(), agora()))
            abertos += 1
        for chave, linha in atuais.items():
            if chave not in achadas:
                conn.execute("UPDATE alertas SET resolvido_em = ?, atualizado_em = ? WHERE id = ?",
                             (agora(), agora(), linha["id"]))
                resolvidos += 1
    for (tipo, chave), (mensagem, _) in achadas.items():
        if (tipo, chave) not in atuais:
            registrar_evento("atencao", "alertas", mensagem)
    return {"abertos": abertos, "resolvidos": resolvidos, "ativos": len(achadas)}


def listar(so_abertos: bool = True, limite: int = 200) -> list[dict]:
    sql = ("SELECT id, tipo, chave, mensagem, ultimo_dado_valido, aberto_em, atualizado_em,"
           " resolvido_em FROM alertas")
    if so_abertos:
        sql += " WHERE resolvido_em IS NULL"
    sql += " ORDER BY aberto_em DESC, id DESC LIMIT ?"
    with conectar() as conn:
        return [dict(l) for l in conn.execute(sql, (limite,))]
