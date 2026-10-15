"""Lote de estrutura E4: o painel dividido em routers (painel/rotas_*.py) expõe
exatamente as mesmas rotas de antes, e a proteção continua valendo em todas."""
import pytest

from painel import app as modulo_app

# Inventário tirado do painel/app.py anterior à divisão. Rota nova entra aqui
# de propósito; rota que some quebra este teste.
ROTAS = {
    ("GET", "/"),
    ("GET", "/api/alertas"),
    ("GET", "/api/amazon/anuncios"),
    ("GET", "/api/amazon/pedidos"),
    ("GET", "/api/amazon/sincronizacao"),
    ("GET", "/api/configuracao"),
    ("GET", "/api/configuracao/url-autorizacao/{provedor}"),
    ("GET", "/api/eventos"),
    ("GET", "/api/fornecedores"),
    ("GET", "/api/fornecedores/{fornecedor_id}"),
    ("GET", "/api/lgpd"),
    ("GET", "/api/pedido/{pedido_id}"),
    ("GET", "/api/pedido/{pedido_id}/comprador"),
    ("GET", "/api/pedidos"),
    ("GET", "/api/pendencias"),
    ("GET", "/api/produtos"),
    ("GET", "/api/produtos/{produto_id}"),
    ("GET", "/api/saude"),
    ("GET", "/api/sessao"),
    ("GET", "/login"),
    ("GET", "/oauth/amazon/login"),
    ("GET", "/oauth/amazon/retorno"),
    ("GET", "/oauth/ml/retorno"),
    ("GET", "/oauth/shopee/iniciar"),
    ("GET", "/oauth/shopee/retorno"),
    ("GET", "/saude"),
    ("PATCH", "/api/fornecedores/{fornecedor_id}"),
    ("PATCH", "/api/produtos/{produto_id}"),
    ("POST", "/api/aprovar/{aprovacao_id}"),
    ("POST", "/api/ciclo"),
    ("POST", "/api/configuracao/concluir-amazon"),
    ("POST", "/api/configuracao/concluir-ml"),
    ("POST", "/api/configuracao/salvar"),
    ("POST", "/api/configuracao/testar/{marketplace}"),
    ("POST", "/api/fornecedores"),
    ("POST", "/api/fornecedores/{fornecedor_id}/desativar"),
    ("POST", "/api/lgpd/expurgar"),
    ("POST", "/api/login"),
    ("POST", "/api/logout"),
    ("POST", "/api/nichos"),
    ("POST", "/api/pedidos/{pedido_id}/reanalisar"),
    ("POST", "/api/preco"),
    ("POST", "/api/produtos"),
    ("POST", "/api/produtos/{produto_id}/desativar"),
    ("POST", "/api/recusar/{aprovacao_id}"),
}


def _todas(rotas):
    for r in rotas:
        if hasattr(r, "original_router"):
            yield from _todas(r.original_router.routes)
        else:
            yield r


def test_mesmas_rotas_de_antes():
    expostas = {(m, r.path) for r in _todas(modulo_app.app.routes)
                for m in getattr(r, "methods", ()) if m != "HEAD"}
    assert expostas == ROTAS


@pytest.mark.parametrize("metodo, caminho", sorted(ROTAS))
def test_toda_rota_exige_sessao_ou_e_livre(cliente, metodo, caminho):
    caminho = caminho.replace("{", "").replace("}", "")  # parâmetro vira texto qualquer
    r = cliente.request(metodo, caminho, follow_redirects=False)
    if caminho in modulo_app.CAMINHOS_LIVRES:
        assert r.status_code not in (401, 303)
    else:
        assert r.status_code in (401, 303), (metodo, caminho, r.status_code)
    assert r.headers["x-frame-options"] == "DENY"
