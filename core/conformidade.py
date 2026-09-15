"""
Motor de conformidade.

A questão da Amazon não se resolve com um aviso no README — resolve-se com
código que impede a ação. Este módulo é consultado ANTES de qualquer compra,
publicação ou envio. Se a regra barra, a ação não entra na fila; vira
pendência marcada como bloqueada, com o motivo e a saída possível.

Cada regra tem fonte declarada. Quando a plataforma mudar a política, você
sabe qual regra revisar e onde conferir.
"""
from dataclasses import dataclass, field
from enum import Enum


class Severidade(str, Enum):
    BLOQUEIO = "bloqueio"      # a ação não sai, ponto
    ALERTA = "alerta"          # sai, mas você é avisado


@dataclass
class Violacao:
    regra: str
    severidade: Severidade
    mensagem: str
    saida: str
    fonte: str


@dataclass
class Resultado:
    violacoes: list[Violacao] = field(default_factory=list)

    @property
    def bloqueado(self) -> bool:
        return any(v.severidade == Severidade.BLOQUEIO for v in self.violacoes)

    @property
    def resumo(self) -> str:
        if not self.violacoes:
            return "Conforme"
        return " | ".join(f"{v.regra}: {v.mensagem}" for v in self.violacoes)


# ---------------------------------------------------------------- Regras

def _amazon_remetente_terceiro(ctx: dict) -> Violacao | None:
    """
    A Amazon exige que o vendedor registrado seja o único identificado em nota,
    embalagem e romaneio. Envio direto da fábrica ao comprador viola a política
    de dropshipping e é motivo de suspensão com retenção de saldo.
    """
    if ctx.get("marketplace") != "amazon":
        return None
    if ctx.get("envio_direto_fornecedor"):
        return Violacao(
            regra="AMZ-DROPSHIP",
            severidade=Severidade.BLOQUEIO,
            mensagem="Envio direto do fornecedor ao comprador é proibido na Amazon.",
            saida="Receba a mercadoria, reembale sem identificação do fornecedor "
                  "e despache com seus dados. Ou use FBA.",
            fonte="Amazon Seller Central — Política de Dropshipping",
        )
    return None


def _amazon_identificacao_fornecedor(ctx: dict) -> Violacao | None:
    if ctx.get("marketplace") != "amazon":
        return None
    if not ctx.get("reembalagem_confirmada", False):
        return Violacao(
            regra="AMZ-REEMBALAGEM",
            severidade=Severidade.BLOQUEIO,
            mensagem="Reembalagem não confirmada para pedido Amazon.",
            saida="Marque `reembalagem_confirmada` no produto só depois de "
                  "garantir que nota, caixa e romaneio saem sem o nome do fornecedor.",
            fonte="Amazon Seller Central — Política de Dropshipping",
        )
    return None


def _prazo_incompativel(ctx: dict) -> Violacao | None:
    """Prazo de fábrica maior que a promessa do anúncio gera atraso sistêmico."""
    prazo_forn = ctx.get("prazo_fornecedor_dias", 0)
    prazo_anuncio = ctx.get("prazo_anuncio_dias", 0)
    if prazo_forn and prazo_anuncio and prazo_forn >= prazo_anuncio:
        return Violacao(
            regra="PRAZO",
            severidade=Severidade.BLOQUEIO,
            mensagem=f"Fornecedor leva {prazo_forn}d; anúncio promete {prazo_anuncio}d.",
            saida="Mantenha estoque mínimo deste SKU ou aumente o prazo do anúncio.",
            fonte="Regra operacional própria",
        )
    return None


def _categoria_restrita(ctx: dict) -> Violacao | None:
    """
    Categorias que exigem registro, licença ou laudo. Vender sem habilitação
    gera remoção do anúncio e, dependendo do item, responsabilidade sanitária.
    """
    restritas = {
        "suplemento": "Registro/notificação na Anvisa e rotulagem conforme RDC.",
        "cosmetico": "Registro ou notificação Anvisa.",
        "medicamento": "Venda restrita a farmácia licenciada.",
        "alimento": "Registro sanitário e rastreabilidade de lote.",
        "brinquedo": "Certificação compulsória Inmetro.",
        "eletrico": "Certificação Inmetro para produtos energizados.",
        "eletronico_radio": "Homologação Anatel para qualquer produto com rádio (Wi-Fi, Bluetooth).",
        "puericultura": "Certificação Inmetro compulsória.",
        "airsoft": "Registro no Exército.",
        "agrotoxico": "Registro Mapa/Ibama.",
    }
    cat = (ctx.get("categoria_regulada") or "").lower()
    if cat in restritas:
        if not ctx.get("habilitacao_confirmada", False):
            return Violacao(
                regra="CATEGORIA-RESTRITA",
                severidade=Severidade.BLOQUEIO,
                mensagem=f"Categoria '{cat}' exige habilitação não confirmada.",
                saida=restritas[cat],
                fonte="Legislação setorial brasileira (Anvisa / Inmetro / Anatel)",
            )
    return None


def _margem_negativa(ctx: dict) -> Violacao | None:
    m = ctx.get("margem_pct")
    if m is not None and m < 0:
        return Violacao(
            regra="MARGEM-NEGATIVA",
            severidade=Severidade.BLOQUEIO,
            mensagem=f"Margem prevista de {m}%. A venda dá prejuízo.",
            saida="Reprecifique o anúncio ou renegocie o custo antes de comprar.",
            fonte="Regra operacional própria",
        )
    return None


def _garantia_legal(ctx: dict) -> Violacao | None:
    """
    CDC art. 26: 30 dias (não durável) / 90 dias (durável) de garantia legal,
    independente do que o anúncio diga. Vendedor no marketplace responde
    solidariamente. Prometer menos que isso é cláusula abusiva.
    """
    prometido = ctx.get("garantia_prometida_dias")
    durabilidade = ctx.get("durabilidade", "duravel")
    minimo = 90 if durabilidade == "duravel" else 30
    if prometido is not None and prometido < minimo:
        return Violacao(
            regra="CDC-GARANTIA",
            severidade=Severidade.BLOQUEIO,
            mensagem=f"Anúncio promete {prometido}d de garantia; o CDC exige {minimo}d.",
            saida=f"Corrija o anúncio para no mínimo {minimo} dias.",
            fonte="CDC art. 26",
        )
    return None


def _direito_arrependimento(ctx: dict) -> Violacao | None:
    """CDC art. 49: 7 dias de arrependimento em compra fora do estabelecimento."""
    if ctx.get("recusa_arrependimento"):
        return Violacao(
            regra="CDC-ARREPENDIMENTO",
            severidade=Severidade.BLOQUEIO,
            mensagem="Anúncio ou resposta nega o direito de arrependimento.",
            saida="7 dias corridos a contar do recebimento, sem justificativa, "
                  "com frete de devolução por sua conta.",
            fonte="CDC art. 49",
        )
    return None


def _nota_fiscal(ctx: dict) -> Violacao | None:
    if ctx.get("emite_nota") is False:
        return Violacao(
            regra="FISCAL-NF",
            severidade=Severidade.BLOQUEIO,
            mensagem="Venda sem emissão de nota fiscal.",
            saida="Marketplaces retêm e declaram o repasse. Venda sem NF gera "
                  "divergência automática na Receita.",
            fonte="Convênio ICMS / obrigação acessória do marketplace",
        )
    return None


def _preco_fora_da_curva(ctx: dict) -> Violacao | None:
    """Preço muito abaixo da mediana costuma indicar erro de cadastro."""
    preco = ctx.get("preco")
    mediana = ctx.get("preco_mediano_mercado")
    if preco and mediana and mediana > 0 and preco < mediana * 0.4:
        return Violacao(
            regra="PRECO-SUSPEITO",
            severidade=Severidade.ALERTA,
            mensagem=f"Preço R$ {preco:.2f} é menos de 40% da mediana (R$ {mediana:.2f}).",
            saida="Confira se houve erro de vírgula ou de unidade antes de publicar.",
            fonte="Regra operacional própria",
        )
    return None


REGRAS = [
    _amazon_remetente_terceiro,
    _amazon_identificacao_fornecedor,
    _prazo_incompativel,
    _categoria_restrita,
    _margem_negativa,
    _garantia_legal,
    _direito_arrependimento,
    _nota_fiscal,
    _preco_fora_da_curva,
]


def verificar(contexto: dict) -> Resultado:
    """
    Roda todas as regras contra o contexto da ação.

    Chame antes de enfileirar compra, antes de publicar anúncio e antes de
    responder cliente. O custo é desprezível e evita o erro caro.
    """
    r = Resultado()
    for regra in REGRAS:
        v = regra(contexto)
        if v:
            r.violacoes.append(v)
    return r
