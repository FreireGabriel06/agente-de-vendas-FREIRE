"""
Configuração central. Tudo vem de variáveis de ambiente — nenhuma credencial
no código, nenhuma credencial no git.

Copie .env.example para .env e preencha.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _carregar_env():
    """Loader mínimo de .env, sem dependência externa."""
    caminho = BASE_DIR / ".env"
    if not caminho.exists():
        return
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, valor = linha.split("=", 1)
        os.environ.setdefault(chave.strip(), valor.strip().strip('"').strip("'"))


_carregar_env()


@dataclass
class ConfigMercadoLivre:
    client_id: str = os.getenv("ML_CLIENT_ID", "")
    client_secret: str = os.getenv("ML_CLIENT_SECRET", "")
    refresh_token: str = os.getenv("ML_REFRESH_TOKEN", "")
    seller_id: str = os.getenv("ML_SELLER_ID", "")
    site_id: str = os.getenv("ML_SITE_ID", "MLB")  # MLB = Brasil
    base_url: str = "https://api.mercadolibre.com"

    @property
    def configurado(self) -> bool:
        return bool(self.client_id and self.client_secret and self.refresh_token)


@dataclass
class ConfigAmazon:
    """Amazon SP-API. Exige conta de vendedor aprovada e app registrado."""
    lwa_client_id: str = os.getenv("AMZ_LWA_CLIENT_ID", "")
    lwa_client_secret: str = os.getenv("AMZ_LWA_CLIENT_SECRET", "")
    refresh_token: str = os.getenv("AMZ_REFRESH_TOKEN", "")
    marketplace_id: str = os.getenv("AMZ_MARKETPLACE_ID", "A2Q3Y263D00KWC")  # BR
    regiao_endpoint: str = os.getenv("AMZ_ENDPOINT", "https://sellingpartnerapi-na.amazon.com")

    @property
    def configurado(self) -> bool:
        return bool(self.lwa_client_id and self.lwa_client_secret and self.refresh_token)


@dataclass
class ConfigNegocio:
    """Regras que definem quando o robô age sozinho e quando ele te chama."""

    # Margem líquida mínima aceitável. Abaixo disso o pedido é recusado
    # automaticamente — é a trava que impede vender no prejuízo.
    margem_minima_pct: float = float(os.getenv("MARGEM_MINIMA_PCT", "18"))

    # Valor acima do qual QUALQUER compra no fornecedor exige seu OK explícito,
    # mesmo que a margem esteja boa.
    teto_compra_automatica: float = float(os.getenv("TETO_COMPRA_AUTOMATICA", "300"))

    # Imposto estimado sobre a venda (Simples Nacional, anexo de comércio).
    # Ajuste pra sua faixa real de faturamento.
    aliquota_imposto_pct: float = float(os.getenv("ALIQUOTA_IMPOSTO_PCT", "4"))

    # Quantos dias de prazo o fornecedor leva. Entra no cálculo de risco
    # de estourar o prazo do marketplace.
    prazo_fornecedor_dias: int = int(os.getenv("PRAZO_FORNECEDOR_DIAS", "5"))


@dataclass
class Config:
    ml: ConfigMercadoLivre = field(default_factory=ConfigMercadoLivre)
    amazon: ConfigAmazon = field(default_factory=ConfigAmazon)
    negocio: ConfigNegocio = field(default_factory=ConfigNegocio)
    db_path: str = os.getenv("DB_PATH", str(BASE_DIR / "agente.db"))
    modo_simulacao: bool = os.getenv("MODO_SIMULACAO", "true").lower() == "true"


config = Config()
