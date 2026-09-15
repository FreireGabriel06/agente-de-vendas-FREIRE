"""
Precificação e margem.

Este é o módulo mais importante do sistema. Se ele mentir, o robô vende no
prejuízo em escala — que é o jeito mais rápido de quebrar num marketplace.

Regras do Mercado Livre vigentes em 2026 (confira no Simulador de Custos do
Seller Center antes de subir pra produção, as faixas mudam por subcategoria):

  - Comissão Clássico: 10% a 14% conforme categoria
  - Comissão Premium:  15% a 19% conforme categoria
  - Abaixo de R$ 79: paga custo por unidade, que desde 02/03/2026 é VARIÁVEL
    por peso e dimensão (antes era fixo, ~R$ 6,25 a R$ 6,75)
  - A partir de R$ 79: some o custo por unidade, mas entra frete grátis
    obrigatório, com o ML subsidiando parte conforme sua reputação

O degrau dos R$ 79 é a distorção mais explorável da plataforma: vender a
R$ 78,90 é pior que vender a R$ 79,10. O método `sugerir_preco` trata isso.
"""
from dataclasses import dataclass, asdict

LIMIAR_CUSTO_UNIDADE = 79.00

# Médias práticas por grupo de categoria. Substitua pelos percentuais exatos
# da sua subcategoria — busque em GET /sites/MLB/listing_prices na API.
COMISSAO_PADRAO = {
    "classico": 0.13,
    "premium": 0.17,
}


def custo_por_unidade(peso_kg: float) -> float:
    """
    Aproximação do custo variável por unidade para itens abaixo de R$ 79.
    Desde março/2026 o ML calcula por peso e dimensão; esta é uma curva de
    referência conservadora (erra pra mais, não pra menos).
    """
    if peso_kg <= 0.3:
        return 6.25
    if peso_kg <= 0.5:
        return 6.75
    if peso_kg <= 1.0:
        return 7.50
    if peso_kg <= 2.0:
        return 9.00
    return 9.00 + (peso_kg - 2.0) * 2.50


def frete_vendedor(peso_kg: float, reputacao: str = "verde") -> float:
    """
    Parcela do frete grátis que sobra pro vendedor em itens acima de R$ 79.
    O ML subsidia até ~70% pra reputação verde escuro. Valores de referência
    pra envio dentro da mesma região.
    """
    tabela_cheia = 17.90 + max(0.0, peso_kg - 0.5) * 5.20
    subsidio = {"verde_escuro": 0.70, "verde": 0.50, "amarelo": 0.30, "vermelho": 0.0}
    return round(tabela_cheia * (1 - subsidio.get(reputacao, 0.40)), 2)


@dataclass
class Composicao:
    preco_venda: float
    custo_produto: float
    comissao: float
    custo_unidade: float
    frete: float
    imposto: float
    lucro_liquido: float
    margem_pct: float
    viavel: bool
    alerta: str = ""

    def como_dict(self):
        return asdict(self)


def calcular(
    preco_venda: float,
    custo_produto: float,
    peso_kg: float = 0.3,
    tipo_anuncio: str = "classico",
    comissao_pct: float | None = None,
    aliquota_imposto_pct: float = 4.0,
    reputacao: str = "verde",
    margem_minima_pct: float = 18.0,
) -> Composicao:
    """Decompõe uma venda em todos os custos e devolve a margem líquida real."""
    taxa = comissao_pct / 100 if comissao_pct is not None else COMISSAO_PADRAO.get(tipo_anuncio, 0.13)

    comissao = round(preco_venda * taxa, 2)

    if preco_venda < LIMIAR_CUSTO_UNIDADE:
        cu = round(custo_por_unidade(peso_kg), 2)
        fr = 0.0
    else:
        cu = 0.0
        fr = frete_vendedor(peso_kg, reputacao)

    imposto = round(preco_venda * aliquota_imposto_pct / 100, 2)
    lucro = round(preco_venda - custo_produto - comissao - cu - fr - imposto, 2)
    margem = round(lucro / preco_venda * 100, 2) if preco_venda else 0.0

    alerta = ""
    if 70.0 <= preco_venda < LIMIAR_CUSTO_UNIDADE:
        alerta = (f"Preço na zona morta (R$70–R$78,99): você paga o custo por unidade "
                  f"sem ganhar exposição. Subir pra R$ {LIMIAR_CUSTO_UNIDADE:.2f} "
                  f"tende a render mais líquido.")
    elif margem < margem_minima_pct:
        alerta = f"Margem {margem}% abaixo do mínimo de {margem_minima_pct}%."

    return Composicao(
        preco_venda=round(preco_venda, 2),
        custo_produto=round(custo_produto, 2),
        comissao=comissao,
        custo_unidade=cu,
        frete=fr,
        imposto=imposto,
        lucro_liquido=lucro,
        margem_pct=margem,
        viavel=margem >= margem_minima_pct,
        alerta=alerta,
    )


def sugerir_preco(
    custo_produto: float,
    margem_alvo_pct: float = 25.0,
    peso_kg: float = 0.3,
    tipo_anuncio: str = "classico",
    aliquota_imposto_pct: float = 4.0,
    reputacao: str = "verde",
) -> Composicao:
    """
    Encontra o menor preço que atinge a margem alvo, testando também o salto
    pro outro lado do degrau dos R$ 79 — em vários casos o preço maior deixa
    mais dinheiro no bolso.
    """
    taxa = COMISSAO_PADRAO.get(tipo_anuncio, 0.13)
    imp = aliquota_imposto_pct / 100
    alvo = margem_alvo_pct / 100

    candidatos = []

    # Cenário A: abaixo do limiar, paga custo por unidade
    cu = custo_por_unidade(peso_kg)
    denom = 1 - taxa - imp - alvo
    if denom > 0:
        p = (custo_produto + cu) / denom
        if p < LIMIAR_CUSTO_UNIDADE:
            candidatos.append(round(p, 2))

    # Cenário B: acima do limiar, paga frete
    fr = frete_vendedor(peso_kg, reputacao)
    if denom > 0:
        p = (custo_produto + fr) / denom
        candidatos.append(round(max(p, LIMIAR_CUSTO_UNIDADE + 0.10), 2))

    if not candidatos:
        raise ValueError("Margem alvo inatingível: taxas + imposto + margem passam de 100%.")

    melhor = None
    for p in candidatos:
        c = calcular(p, custo_produto, peso_kg, tipo_anuncio,
                     aliquota_imposto_pct=aliquota_imposto_pct, reputacao=reputacao)
        if melhor is None or c.lucro_liquido > melhor.lucro_liquido:
            melhor = c
    return melhor
