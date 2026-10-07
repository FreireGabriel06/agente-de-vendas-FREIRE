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

As contas são em Decimal, arredondadas ao centavo (meio centavo para cima).
O modelo é em reais (MOEDA): o worker só calcula a margem quando o custo e a
venda estão os dois em BRL (motivo_sem_margem). Não há conversão de moeda.
"""
from dataclasses import dataclass, asdict
from decimal import Decimal, ROUND_HALF_UP

from core import dinheiro

# Moeda do modelo de taxas abaixo (Mercado Livre Brasil). Margem em outra
# moeda pede um modelo próprio; até lá, o worker diz por que não calcula.
MOEDA = "BRL"

LIMIAR_CUSTO_UNIDADE = Decimal("79.00")

# Médias práticas por grupo de categoria. Substitua pelos percentuais exatos
# da sua subcategoria — busque em GET /sites/MLB/listing_prices na API.
COMISSAO_PADRAO = {
    "classico": Decimal("0.13"),
    "premium": Decimal("0.17"),
}

_ZONA_MORTA = Decimal("70.00")
_CENTAVO = Decimal("0.01")
_ZERO = Decimal("0.00")


def _decimal(valor) -> Decimal:
    """Decimal de quem chama. Os chamadores antigos (argparse, JSON do
    /api/preco, coluna REAL) mandam float: ele entra pelo texto que o SQLite
    mostraria (core/dinheiro.do_real), nunca pelos bits binários."""
    numero = dinheiro.do_real(valor)
    if numero is None:
        raise ValueError(f"Número inválido: {valor!r}")
    return numero


def _centavos(valor: Decimal) -> Decimal:
    return valor.quantize(_CENTAVO, rounding=ROUND_HALF_UP)


def motivo_sem_margem(moeda_custo: str | None, moeda_venda: str | None) -> str | None:
    """Por que a margem não pode ser calculada com estas moedas, ou None.
    O programa não converte moeda e não presume uma: custo e venda precisam
    estar informados, iguais, e na moeda do modelo de taxas."""
    if moeda_custo is None:
        return "a moeda do custo do produto não foi informada (moeda não informada)"
    if moeda_venda is None:
        return "a moeda da venda não foi informada pelo marketplace (moeda não informada)"
    if moeda_custo != moeda_venda:
        return (f"o custo está em {moeda_custo} e a venda em {moeda_venda}; "
                "o programa não converte moedas")
    if moeda_custo != MOEDA:
        return (f"o modelo de taxas (Mercado Livre Brasil) é em {MOEDA}; "
                f"não há modelo para {moeda_custo}")
    return None


def custo_por_unidade(peso_kg) -> Decimal:
    """
    Aproximação do custo variável por unidade para itens abaixo de R$ 79.
    Desde março/2026 o ML calcula por peso e dimensão; esta é uma curva de
    referência conservadora (erra pra mais, não pra menos).
    """
    peso = _decimal(peso_kg)
    if peso <= Decimal("0.3"):
        return Decimal("6.25")
    if peso <= Decimal("0.5"):
        return Decimal("6.75")
    if peso <= Decimal("1.0"):
        return Decimal("7.50")
    if peso <= Decimal("2.0"):
        return Decimal("9.00")
    return Decimal("9.00") + (peso - 2) * Decimal("2.50")


def frete_vendedor(peso_kg, reputacao: str = "verde") -> Decimal:
    """
    Parcela do frete grátis que sobra pro vendedor em itens acima de R$ 79.
    O ML subsidia até ~70% pra reputação verde escuro. Valores de referência
    pra envio dentro da mesma região.
    """
    peso = _decimal(peso_kg)
    tabela_cheia = Decimal("17.90") + max(Decimal(0), peso - Decimal("0.5")) * Decimal("5.20")
    subsidio = {"verde_escuro": Decimal("0.70"), "verde": Decimal("0.50"),
                "amarelo": Decimal("0.30"), "vermelho": Decimal("0")}
    return _centavos(tabela_cheia * (1 - subsidio.get(reputacao, Decimal("0.40"))))


@dataclass
class Composicao:
    preco_venda: Decimal
    custo_produto: Decimal
    comissao: Decimal
    custo_unidade: Decimal
    frete: Decimal
    imposto: Decimal
    lucro_liquido: Decimal
    margem_pct: Decimal
    viavel: bool
    alerta: str = ""

    def como_dict(self):
        return asdict(self)


def calcular(
    preco_venda,
    custo_produto,
    peso_kg=Decimal("0.3"),
    tipo_anuncio: str = "classico",
    comissao_pct=None,
    aliquota_imposto_pct=Decimal("4.0"),
    reputacao: str = "verde",
    margem_minima_pct=Decimal("18.0"),
) -> Composicao:
    """Decompõe uma venda em todos os custos e devolve a margem líquida real.
    Valores em reais (MOEDA); números em Decimal, int ou float."""
    preco = _decimal(preco_venda)
    custo = _decimal(custo_produto)
    taxa = (_decimal(comissao_pct) / 100 if comissao_pct is not None
            else COMISSAO_PADRAO.get(tipo_anuncio, Decimal("0.13")))

    comissao = _centavos(preco * taxa)

    if preco < LIMIAR_CUSTO_UNIDADE:
        cu = _centavos(custo_por_unidade(peso_kg))
        fr = _ZERO
    else:
        cu = _ZERO
        fr = frete_vendedor(peso_kg, reputacao)

    imposto = _centavos(preco * _decimal(aliquota_imposto_pct) / 100)
    lucro = _centavos(preco - custo - comissao - cu - fr - imposto)
    margem = _centavos(lucro / preco * 100) if preco else _ZERO
    minimo = _decimal(margem_minima_pct)

    alerta = ""
    if _ZONA_MORTA <= preco < LIMIAR_CUSTO_UNIDADE:
        alerta = (f"Preço na zona morta (R$70–R$78,99): você paga o custo por unidade "
                  f"sem ganhar exposição. Subir pra R$ {LIMIAR_CUSTO_UNIDADE:.2f} "
                  f"tende a render mais líquido.")
    elif margem < minimo:
        alerta = f"Margem {margem}% abaixo do mínimo de {margem_minima_pct}%."

    return Composicao(
        preco_venda=_centavos(preco),
        custo_produto=_centavos(custo),
        comissao=comissao,
        custo_unidade=cu,
        frete=fr,
        imposto=imposto,
        lucro_liquido=lucro,
        margem_pct=margem,
        viavel=margem >= minimo,
        alerta=alerta,
    )


def sugerir_preco(
    custo_produto,
    margem_alvo_pct=Decimal("25.0"),
    peso_kg=Decimal("0.3"),
    tipo_anuncio: str = "classico",
    aliquota_imposto_pct=Decimal("4.0"),
    reputacao: str = "verde",
) -> Composicao:
    """
    Encontra o menor preço que atinge a margem alvo, testando também o salto
    pro outro lado do degrau dos R$ 79 — em vários casos o preço maior deixa
    mais dinheiro no bolso.
    """
    custo = _decimal(custo_produto)
    taxa = COMISSAO_PADRAO.get(tipo_anuncio, Decimal("0.13"))
    imp = _decimal(aliquota_imposto_pct) / 100
    alvo = _decimal(margem_alvo_pct) / 100

    candidatos = []

    # Cenário A: abaixo do limiar, paga custo por unidade
    cu = custo_por_unidade(peso_kg)
    denom = 1 - taxa - imp - alvo
    if denom > 0:
        p = (custo + cu) / denom
        if p < LIMIAR_CUSTO_UNIDADE:
            candidatos.append(_centavos(p))

    # Cenário B: acima do limiar, paga frete
    fr = frete_vendedor(peso_kg, reputacao)
    if denom > 0:
        p = (custo + fr) / denom
        candidatos.append(_centavos(max(p, LIMIAR_CUSTO_UNIDADE + Decimal("0.10"))))

    if not candidatos:
        raise ValueError("Margem alvo inatingível: taxas + imposto + margem passam de 100%.")

    melhor = None
    for p in candidatos:
        c = calcular(p, custo, peso_kg, tipo_anuncio,
                     aliquota_imposto_pct=aliquota_imposto_pct, reputacao=reputacao)
        if melhor is None or c.lucro_liquido > melhor.lucro_liquido:
            melhor = c
    return melhor
