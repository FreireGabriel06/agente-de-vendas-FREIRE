"""Lote de estrutura E5: número de operação mal escrito no ambiente (o .env não
corta comentário no fim da linha) não derruba a importação; vale o padrão,
com aviso. Constante lida na importação: o teste roda num processo novo, com
a pasta de dados temporária dos testes."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
LER = ("import config, core.seguranca as s, core.privacidade as p, conectores.shopee as sh; "
       "print(s.OCIOSIDADE_MIN, s.DURACAO_MAX_H, p.RETENCAO_DIAS, "
       "config.config.negocio.prazo_fornecedor_dias, sh.Shopee().shop_id)")


def ler(**variaveis):
    ambiente = {**os.environ, **variaveis}
    assert "agente-testes-" in ambiente["AGENTE_DADOS"]  # nunca a pasta real
    r = subprocess.run([sys.executable, "-c", LER], cwd=RAIZ, env=ambiente,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.split(), r.stderr


def test_valores_validos_valem():
    valores, _ = ler(SESSAO_OCIOSA_MIN="15", SESSAO_MAX_HORAS="4", RETENCAO_PII_DIAS="30",
                     PRAZO_FORNECEDOR_DIAS="7", SHOPEE_SHOP_ID="123")
    assert valores == ["15", "4", "30", "7", "123"]


@pytest.mark.parametrize("invalido", ["30 # minutos", "abc", "-5", "999999999"])
def test_valor_invalido_avisa_e_usa_o_padrao(invalido):
    valores, avisos = ler(SESSAO_OCIOSA_MIN=invalido, SESSAO_MAX_HORAS=invalido,
                          RETENCAO_PII_DIAS=invalido, PRAZO_FORNECEDOR_DIAS=invalido,
                          SHOPEE_SHOP_ID=invalido)
    # shop_id da Shopee não tem máximo: um número grande vale.
    shop = invalido if invalido.isdigit() else "0"
    assert valores == ["30", "8", "1825", "5", shop]
    for variavel in ("SESSAO_OCIOSA_MIN", "SESSAO_MAX_HORAS", "RETENCAO_PII_DIAS",
                     "PRAZO_FORNECEDOR_DIAS") + (() if invalido.isdigit() else ("SHOPEE_SHOP_ID",)):
        assert f"Aviso: {variavel}=" in avisos
