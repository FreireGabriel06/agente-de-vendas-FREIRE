"""
Painel de operação.

Servidor local. Sobe junto com o worker no mesmo processo — você abre o
navegador e opera tudo dali.

Decisões de UX que valem explicar:

  - A tela abre no que importa: quanto dinheiro está esperando sua decisão.
    Não em gráfico, não em boas-vindas.
  - Triagem por teclado. j/k anda na lista, a aprova, r recusa, / filtra.
    Quem usa isso todo dia não quer mirar o mouse em 12 botões.
  - Compra tem confirmação; resposta a cliente tem desfazer de 6 segundos.
    Modal em tudo cansa; desfazer só funciona se o dano for reversível.
  - Item bloqueado pela conformidade não tem botão de aprovar. Não é aviso
    que você ignora clicando — o caminho não existe até a causa ser resolvida.
    A checagem é a mesma do worker e do cli.py (core/conformidade), e falta
    de dado bloqueia: nada aqui presume o valor que libera.
  - Com MODO_SIMULACAO ligado, a tela diz isso no topo.

Onde está cada parte:

  - protecao.py: sessão, CSRF, papel, Host e cabeçalhos de toda requisição;
  - rotas_operacao.py: fila, pedidos, comprador, preço, LGPD, saúde, eventos;
  - rotas_cadastro.py: produtos, fornecedores e reanálise de pedido;
  - rotas_conexoes.py: configuração e OAuth dos marketplaces;
  - rotas_amazon.py: leitura dos dados da Amazon e alertas;
  - rotas_sessao.py: login, logout, sessão e as páginas.
"""
from fastapi import FastAPI

from core import cofre, privacidade, seguranca
from db import inicializar
from painel import (protecao, rotas_amazon, rotas_cadastro, rotas_conexoes, rotas_operacao,
                    rotas_sessao)
# Nomes que outros módulos e os testes usam a partir de painel.app.
from painel.protecao import (CAMINHOS_LIVRES, CSP, hosts_permitidos,  # noqa: F401
                             leitura_pode, nome_do_host)
from painel.rotas_conexoes import _pagina_retorno  # noqa: F401

# Sem /docs, /redoc nem /openapi.json: o mapa da API não fica à mostra nem
# para quem tem sessão (achado S-24).
app = FastAPI(title="Agente Comercial", docs_url=None, redoc_url=None, openapi_url=None)
protecao.instalar(app)
rotas_cadastro.instalar_erros(app)
for _rotas in (rotas_operacao, rotas_cadastro, rotas_conexoes, rotas_amazon, rotas_sessao):
    app.include_router(_rotas.rotas)


def preparar():
    inicializar()
    privacidade.inicializar_lgpd()
    seguranca.inicializar_seguranca()
    cofre.inicializar_cofre()
