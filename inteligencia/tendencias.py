"""
Descoberta de oportunidade: o que está sendo procurado e ainda não está
saturado.

Duas fontes reais e gratuitas:

  1. Google Trends (via pytrends) — mede DEMANDA e VELOCIDADE. Diz o que as
     pessoas estão procurando e se a curva está subindo ou já passou do pico.
  2. API de busca do Mercado Livre — mede CONCORRÊNCIA e PREÇO PRATICADO.
     Diz quantos vendedores já estão nesse termo e por quanto vendem.

A conta que interessa não é "o que vende mais". É demanda alta com
concorrência baixa. Termo com 50 mil anúncios já foi — quem chegou primeiro
tem reputação e você vai brigar por preço, que é a briga que você perde.

Limitação honesta: Google Trends dá índice relativo (0–100), não volume
absoluto. Serve pra comparar termos entre si e detectar direção da curva,
não pra estimar "quantas vendas por mês". Trate como bússola, não como GPS.
"""
import math
import statistics
import time
from dataclasses import dataclass
from typing import Iterable

import requests

from db import conectar, agora, registrar_evento
from config import config


@dataclass
class Oportunidade:
    termo: str
    demanda: float          # 0–100, índice Google Trends
    tendencia_pct: float    # variação % do último terço vs. primeiro terço
    concorrencia: int       # nº de anúncios no ML
    preco_mediano: float
    score: float
    nicho: str = ""

    def resumo(self) -> str:
        direcao = "subindo" if self.tendencia_pct > 5 else ("caindo" if self.tendencia_pct < -5 else "estável")
        return (f"{self.termo}: demanda {self.demanda:.0f}/100 ({direcao} {self.tendencia_pct:+.0f}%), "
                f"{self.concorrencia} anúncios, mediana R$ {self.preco_mediano:.2f}, "
                f"score {self.score:.1f}")


# ---------------------------------------------------------------- Google Trends

def _serie_trends(termos: list[str], periodo: str = "today 3-m", geo: str = "BR") -> dict:
    """
    Retorna {termo: [série temporal]}. Requer pytrends.
    Devolve {} silenciosamente se a lib não estiver instalada ou o Google
    limitar a taxa — o pipeline continua com as outras fontes.
    """
    try:
        from pytrends.request import TrendReq
    except ImportError:
        registrar_evento("aviso", "tendencias", "pytrends não instalado; pulando Google Trends")
        return {}

    resultado = {}
    # Google Trends aceita no máximo 5 termos por consulta.
    for i in range(0, len(termos), 5):
        lote = termos[i:i + 5]
        try:
            py = TrendReq(hl="pt-BR", tz=180)
            py.build_payload(lote, timeframe=periodo, geo=geo)
            df = py.interest_over_time()
            if df is None or df.empty:
                continue
            for termo in lote:
                if termo in df.columns:
                    resultado[termo] = df[termo].tolist()
            time.sleep(2)  # o Google bloqueia rajada; respeite o intervalo
        except Exception as e:
            registrar_evento("aviso", "tendencias", f"Falha no Trends para {lote}: {e}")
    return resultado


def _analisar_serie(serie: list[float]) -> tuple[float, float]:
    """Devolve (demanda média, variação % entre o primeiro e o último terço)."""
    if not serie:
        return 0.0, 0.0
    n = len(serie)
    if n < 6:
        return statistics.mean(serie), 0.0
    corte = n // 3
    inicio = statistics.mean(serie[:corte]) or 1.0
    fim = statistics.mean(serie[-corte:])
    return statistics.mean(serie), round((fim - inicio) / inicio * 100, 1)


# ------------------------------------------------------------- Mercado Livre

def _concorrencia_ml(termo: str, token: str | None = None) -> tuple[int, float]:
    """
    Consulta a busca do ML e devolve (total de anúncios, preço mediano).

    A partir de 2025 o ML passou a exigir token na maioria dos endpoints de
    busca. Sem token, a função degrada pra (0, 0.0) em vez de quebrar.
    """
    url = f"{config.ml.base_url}/sites/{config.ml.site_id}/search"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        r = requests.get(url, params={"q": termo, "limit": 50},
                         headers=headers, timeout=15)
        if r.status_code != 200:
            registrar_evento("aviso", "tendencias",
                             f"Busca ML '{termo}' retornou {r.status_code}")
            return 0, 0.0
        dados = r.json()
        total = dados.get("paging", {}).get("total", 0)
        precos = [item["price"] for item in dados.get("results", [])
                  if isinstance(item.get("price"), (int, float))]
        mediana = statistics.median(precos) if precos else 0.0
        return total, round(mediana, 2)
    except requests.RequestException as e:
        registrar_evento("aviso", "tendencias", f"Erro de rede na busca ML '{termo}': {e}")
        return 0, 0.0


def termos_em_alta_ml(token: str | None = None, limite: int = 20) -> list[str]:
    """Puxa as buscas em alta direto do ML. Complementa o Google Trends."""
    url = f"{config.ml.base_url}/trends/{config.ml.site_id}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code != 200:
            return []
        return [t["keyword"] for t in r.json()[:limite] if "keyword" in t]
    except (requests.RequestException, ValueError):
        return []


# ------------------------------------------------------------------- Scoring

def _score(demanda: float, tendencia_pct: float, concorrencia: int) -> float:
    """
    Demanda alta e concorrência baixa puxam o score pra cima; crescimento
    é multiplicador. O log amortece a concorrência — a diferença entre 100 e
    1.000 anúncios importa muito mais que entre 40.000 e 50.000.
    """
    if demanda <= 0:
        return 0.0
    fator_crescimento = 1 + max(-0.5, min(1.0, tendencia_pct / 100))
    denominador = math.log10(max(concorrencia, 10))
    return round(demanda * fator_crescimento / denominador, 2)


def analisar(termos: Iterable[str], token_ml: str | None = None,
             nicho: str = "", persistir: bool = True) -> list[Oportunidade]:
    """Roda o pipeline completo e devolve as oportunidades ordenadas por score."""
    termos = list(dict.fromkeys(t.strip().lower() for t in termos if t.strip()))
    if not termos:
        return []

    series = _serie_trends(termos)
    oportunidades = []

    for termo in termos:
        demanda, tendencia = _analisar_serie(series.get(termo, []))
        concorrencia, preco = _concorrencia_ml(termo, token_ml)
        op = Oportunidade(
            termo=termo,
            demanda=round(demanda, 1),
            tendencia_pct=tendencia,
            concorrencia=concorrencia,
            preco_mediano=preco,
            score=_score(demanda, tendencia, concorrencia),
            nicho=nicho,
        )
        oportunidades.append(op)
        time.sleep(0.4)  # educação com a API do ML

    oportunidades.sort(key=lambda o: o.score, reverse=True)

    if persistir and oportunidades:
        with conectar() as conn:
            conn.executemany(
                "INSERT INTO oportunidades (termo, nicho, score, demanda, concorrencia,"
                " preco_mediano, tendencia_pct, fonte, coletado_em)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                [(o.termo, o.nicho, o.score, o.demanda, o.concorrencia,
                  o.preco_mediano, o.tendencia_pct, "trends+ml", agora())
                 for o in oportunidades],
            )
    return oportunidades
