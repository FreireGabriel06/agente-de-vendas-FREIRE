#!/usr/bin/env python3
"""
Painel de comando do robô.

  python cli.py init                           cria o banco
  python cli.py pendencias                     o que está esperando você (e o que está bloqueado)
  python cli.py aprovar 7                      confere a conformidade, aprova e executa a ação 7
  python cli.py aprovar 7 8 9                  em lote
  python cli.py recusar 7 "custo subiu"        recusa com motivo
  python cli.py nichos "termo a" "termo b"     análise de oportunidade
  python cli.py preco 30 --peso 0.4            sugere preço pra um custo
  python cli.py margem 89.90 30 --peso 0.4     decompõe uma venda
  python cli.py ciclo                          roda uma passada do worker
  python cli.py rodar --intervalo 300          roda em laço contínuo (padrão: INTERVALO_WORKER)
  python cli.py eventos                        últimos alertas
  python cli.py operador --usuario ana         cria o operador ou troca a senha
  python cli.py cofre migrar                   copia segredos do .env e tokens antigos pro cofre
  python cli.py cofre rotacionar               recifra o cofre com a chave atual
  python cli.py cofre listar                   o que está no cofre (nomes, nunca valores)
  python cli.py cofre apagar mercadolivre refresh_token   revoga uma credencial
  python cli.py cofre apagar --tudo            chave perdida: apaga tudo, sem a chave (pede confirmação)

Cadastro (a mesma validação da API do painel, core/cadastro.py):

  python cli.py fornecedor criar --nome "Fábrica" --canal email --contato pedidos@fabrica.example --prazo 4
  python cli.py fornecedor editar 1 --prazo 6           só o que for informado muda
  python cli.py fornecedor desativar 1                  nada é apagado
  python cli.py fornecedor editar 1 --reativar
  python cli.py fornecedor listar --ativo true          sem --ativo, lista todos
  python cli.py produto criar --sku ORG-001 --titulo "Organizador" --custo 18.50 --moeda BRL --peso 0.4 --fornecedor 1
  python cli.py produto editar ORG-001 --custo 19.90 --moeda BRL   valor e moeda andam juntos
  python cli.py produto editar ORG-001 --categoria-regulada nenhuma --reembalagem sim
  python cli.py produto desativar ORG-001               a compra dele fica bloqueada até reativar
  python cli.py produto listar

Pedido em PROBLEMA, depois de corrigido o cadastro (nenhuma compra dele pode ter saído):

  python cli.py pedido reanalisar 12                    volta para NOVO; o próximo ciclo refaz a análise
  python cli.py pedido reanalisar 12 --moeda-venda BRL  registra a moeda que o marketplace não informou
"""
import argparse
import re
import sys

from config import avisar_pasta_de_dados, config
from db import inicializar, conectar
from core import aprovacao, cadastro, conformidade, dinheiro, privacidade
from inteligencia import precificacao, tendencias

AVISO_SIMULACAO = ("Modo simulação ligado (MODO_SIMULACAO=true): aprovar não publica "
                   "resposta, não muda preço e marca a ordem de compra como teste. O pedido "
                   "e a pergunta continuam esperando: voltam para a fila quando você "
                   "desligar a simulação.")


def cmd_init(_):
    inicializar()
    privacidade.criar_tabelas_lgpd()
    print("Banco criado. Cadastre fornecedores e produtos antes de ligar o worker.")


def _saidas(resultado, recuo: str = "      ") -> None:
    for v in resultado.bloqueios:
        print(f"{recuo}Saída ({v.regra}): {v.saida}")


def cmd_pendencias(_):
    itens = aprovacao.pendentes()
    if config.modo_simulacao:
        print(AVISO_SIMULACAO + "\n")
    if not itens:
        print("Nada pendente.")
        return
    print(f"{len(itens)} ação(ões) esperando sua decisão:\n")
    total = 0.0
    bloqueadas = 0
    for a in itens:
        valor = f"R$ {a.valor:.2f}" if a.valor else "—"
        print(f"  [{a.id}] {a.tipo:<20} {valor:>12}  {a.resumo}")
        # A mesma checagem do painel e da aprovação.
        checagem = aprovacao.checar(a)
        if checagem.bloqueado:
            bloqueadas += 1
            print(f"       {conformidade.motivo_do_bloqueio(checagem)}")
            _saidas(checagem, "       ")
        else:
            total += a.valor or 0
    if bloqueadas:
        print(f"\n  {bloqueadas} bloqueada(s) pela conformidade: não há como aprovar "
              "antes de resolver a causa.")
    print(f"\n  Exposição se você aprovar todas as liberadas: R$ {total:.2f}")


def cmd_aprovar(args):
    """Aprova pela mesma porta do painel (core/aprovacao.aprovar): item
    bloqueado pela conformidade não executa, e a recusa traz o mesmo motivo
    que o painel devolve com HTTP 409."""
    from worker import EXECUTORES
    for i in args.ids:
        try:
            resultado = aprovacao.aprovar(i, EXECUTORES)
        except aprovacao.BloqueadoPelaConformidade as e:
            print(f"  ✗ {i} não executada. {e}")
            _saidas(e.resultado)
            continue
        except Exception as e:
            print(f"  ✗ {i} falhou: {e}")
            continue
        situacao = "simulada" if aprovacao.foi_simulado(resultado) else "executada"
        print(f"  ✓ {i} {situacao}: {resultado}")


def cmd_recusar(args):
    aprovacao.recusar(args.id, args.motivo)
    print(f"Ação {args.id} recusada.")


def cmd_nichos(args):
    print(f"Analisando {len(args.termos)} termo(s)... (o Google Trends limita a taxa, leva um tempo)\n")
    ops = tendencias.analisar(args.termos, nicho=args.nicho or "")
    if not ops:
        print("Nada retornado. Verifique se o pytrends está instalado e se há token do ML.")
        return
    for o in ops:
        print("  " + o.resumo())
    print("\nScore alto = demanda boa com concorrência baixa. É onde dá pra entrar.")


def cmd_preco(args):
    c = precificacao.sugerir_preco(args.custo, args.margem, args.peso, args.tipo)
    print(f"Preço sugerido: R$ {c.preco_venda:.2f}")
    print(f"  comissão     R$ {c.comissao:.2f}")
    print(f"  custo unid.  R$ {c.custo_unidade:.2f}")
    print(f"  frete        R$ {c.frete:.2f}")
    print(f"  imposto      R$ {c.imposto:.2f}")
    print(f"  produto      R$ {c.custo_produto:.2f}")
    print(f"  → lucro      R$ {c.lucro_liquido:.2f}  ({c.margem_pct}%)")


def cmd_margem(args):
    c = precificacao.calcular(args.preco, args.custo, args.peso, args.tipo)
    for k, v in c.como_dict().items():
        print(f"  {k:<16} {v}")


def cmd_ciclo(_):
    from worker import ciclo
    print(ciclo())


def cmd_rodar(args):
    from worker import rodar
    rodar(args.intervalo)


def cmd_operador(args):
    """Cria o operador do painel ou troca a senha. A senha nunca vem por argumento."""
    import getpass
    from core import seguranca

    inicializar()
    seguranca.inicializar_seguranca()
    senha = getpass.getpass(f"Senha (mínimo {seguranca.SENHA_MINIMA} caracteres): ")
    if senha != getpass.getpass("Repita a senha: "):
        print("As senhas não conferem.")
        sys.exit(1)
    print(seguranca.definir_operador(args.usuario, senha) + f": {args.usuario}")


def _nomes(itens) -> str:
    return ", ".join(itens) if itens else "—"


def cmd_cofre(args):
    """Cofre de credenciais. Nunca imprime valor nem chave."""
    from core import cofre

    inicializar()
    cofre.inicializar_cofre()

    if args.acao == "migrar":
        from conectores import mercadolivre, shopee
        from painel import configurar

        arquivo_env = configurar.ARQ_ENV
        rel = cofre.migrar(arquivo_env, {mercadolivre.PROVEDOR: mercadolivre.ARQ_TOKEN,
                                         shopee.PROVEDOR: shopee.ARQ_TOKEN})
        print("Cofre de credenciais: migração. O .env e os arquivos de token não são alterados.\n")
        if not (rel.linhas_env or rel.arquivos):
            print("Nada para migrar: o .env não tem segredo preenchido e não há arquivo de token antigo.")
            return
        print(f"  Copiados agora e conferidos por leitura: {_nomes(rel.copiados)}")
        print(f"  Já estavam no cofre com o mesmo valor:   {_nomes(rel.ja_no_cofre)}")
        print(f"  No cofre com outro valor, que é o que vale: {_nomes(rel.diferentes)}")
        print(f"  Tokens de arquivo antigo no cofre:        {_nomes(a.name for a in rel.arquivos)}")
        if rel.no_sistema:
            print(f"  Também no ambiente do sistema, que vale antes do cofre: {_nomes(rel.no_sistema)}")
        print("\nDepois de testar as conexões no painel, você pode apagar à mão:")
        if rel.linhas_env:
            print(f"  no arquivo {arquivo_env}, as linhas: {_nomes(rel.linhas_env)}")
        for arquivo in rel.arquivos:
            print(f"  o arquivo {arquivo}")
        print(f"\nChave do cofre em uso: {cofre.origem_da_chave() or '(nenhuma ainda)'}. "
              "Faça backup dela: sem a chave, é preciso reconectar os marketplaces e "
              "digitar os segredos de novo.")
        return

    if args.acao == "rotacionar":
        total = cofre.rotacionar()
        print(f"{total} credencial(is) recifrada(s) com a chave atual (a primeira de "
              f"{cofre.VARIAVEL_CHAVE} ou de {cofre.ARQ_CHAVE.name}).")
        if total and cofre.quantidade_de_chaves() > 1:
            print("A chave antiga já pode sair. Guarde o backup da chave nova.")
        return

    if args.acao == "listar":
        itens = cofre.listar()
        if not itens:
            print("Cofre vazio.")
        for item in itens:
            print(f"  {item['provedor']:<14} {item['nome']:<18} atualizado em {item['atualizado_em']}")
        return

    if args.acao == "apagar" and args.tudo:
        if args.provedor or args.nome:
            raise ValueError("Use --tudo sozinho, ou informe PROVEDOR e NOME sem --tudo.")
        total = len(cofre.listar())
        if not total:
            print("Cofre vazio.")
            return
        print(f"Isto apaga as {total} credencial(is) do cofre, sem precisar da chave. É a "
              "saída quando a chave se perdeu: depois será preciso reconectar os "
              "marketplaces e digitar os segredos de novo no painel.")
        if input("Digite APAGAR para confirmar: ").strip() != "APAGAR":
            print("Nada foi apagado.")
            return
        apagadas = cofre.apagar_tudo()
        print(f"{apagadas} credencial(is) apagada(s) do cofre. A chave "
              f"({cofre.origem_da_chave() or 'uma nova, criada na próxima gravação'}) "
              "vale para as próximas gravações. Revogue no portal do marketplace o que "
              "possa ter vazado.")
        return

    if args.acao == "apagar":
        if cofre.apagar(args.provedor, args.nome):
            print(f"Apagado do cofre: {args.provedor}/{args.nome}. Revogue também no portal "
                  "do marketplace, se a credencial vazou.")
        else:
            print(f"Não havia {args.provedor}/{args.nome} no cofre.")


# ------------------------------------------------- Produtos e fornecedores
#
# O cli.py só traduz as opções para o mesmo dicionário que a API recebe e
# chama core/cadastro: a validação e as mensagens são as mesmas do painel.
# Recusa sai com código 1 e a explicação na saída de erro, nunca traceback.

ATOR_CLI = "cli"
_SIM_NAO = {"sim": True, "nao": False, "limpar": None}
_INTEIRO = re.compile(r"[+-]?[0-9]+")


def _inteiro_ou_texto(texto: str):
    """Opção numérica (--prazo, --fornecedor). O que não for inteiro segue
    como texto, e a validação do cadastro recusa com a mesma mensagem da API;
    o type=int do argparse responderia outra coisa, em inglês."""
    limpo = texto.strip()
    return int(limpo) if _INTEIRO.fullmatch(limpo) else texto


def _opcional(texto: str | None):
    """Texto vazio na linha de comando limpa o campo (vira null)."""
    return None if texto == "" else texto


def _dinheiro(valor, moeda) -> dict | None:
    """--custo/--pedido-minimo e --moeda viram {"valor", "moeda"}; o que
    faltar a validação aponta, como na API."""
    if valor is None and moeda is None:
        return None
    dados = {}
    if valor is not None:
        dados["valor"] = valor
    if moeda is not None:
        dados["moeda"] = moeda
    return dados


def _dados_produto(args) -> dict:
    dados = {}
    for campo, valor in (("sku", args.sku), ("titulo", args.titulo), ("peso_kg", args.peso),
                         ("fornecedor_id", args.fornecedor)):
        if valor is not None:
            dados[campo] = valor
    if args.categoria_ml is not None:
        dados["categoria_ml"] = _opcional(args.categoria_ml)
    custo = _dinheiro(args.custo, args.moeda)
    if custo is not None:
        dados["custo_fornecedor"] = custo
    if getattr(args, "sem_fornecedor", False):
        dados["fornecedor_id"] = None
    if args.categoria_regulada is not None:
        dados["categoria_regulada"] = (None if args.categoria_regulada == "limpar"
                                       else args.categoria_regulada)
    if args.habilitacao is not None:
        dados["habilitacao_confirmada"] = _SIM_NAO[args.habilitacao]
    if args.reembalagem is not None:
        dados["reembalagem_confirmada"] = _SIM_NAO[args.reembalagem]
    if getattr(args, "reativar", False):
        dados["ativo"] = True
    return dados


def _dados_fornecedor(args) -> dict:
    dados = {}
    for campo, valor in (("nome", args.nome), ("canal", args.canal), ("contato", args.contato),
                         ("prazo_dias", args.prazo)):
        if valor is not None:
            dados[campo] = valor
    if args.observacoes is not None:
        dados["observacoes"] = _opcional(args.observacoes)
    minimo = _dinheiro(args.pedido_minimo, args.moeda)
    if minimo is not None:
        dados["pedido_minimo"] = minimo
    if getattr(args, "sem_pedido_minimo", False):
        dados["pedido_minimo"] = None
    if getattr(args, "reativar", False):
        dados["ativo"] = True
    return dados


def _valor(dado: dict | None) -> str:
    if dado is None:
        return "—"
    return dinheiro.formatar(dinheiro.do_texto(dado["valor"]), dado["moeda"])


def _sim_nao_texto(valor) -> str:
    return {True: "sim", False: "não"}.get(valor, "—")


def _linha_produto(p: dict) -> str:
    situacao = "" if p["ativo"] else "  [desativado]"
    fornecedor = p["fornecedor_id"] if p["fornecedor_id"] is not None else "—"
    return (f"  [{p['id']}] {p['sku']}  {p['titulo']}  custo {_valor(p['custo_fornecedor'])}"
            f"  peso {p['peso_kg']} kg  fornecedor {fornecedor}{situacao}\n"
            f"       categoria regulada: {p['categoria_regulada'] or '—'}"
            f" | habilitação: {_sim_nao_texto(p['habilitacao_confirmada'])}"
            f" | reembalagem: {_sim_nao_texto(p['reembalagem_confirmada'])}")


def _linha_fornecedor(f: dict) -> str:
    situacao = "" if f["ativo"] else "  [desativado]"
    return (f"  [{f['id']}] {f['nome']}  {f['canal']}: {f['contato']}  prazo {f['prazo_dias']}d"
            f"  pedido mínimo {_valor(f['pedido_minimo'])}{situacao}")


def _recusar(erro: Exception):
    """A mesma recusa que a API devolve (422, 409 ou 404), na saída de erro."""
    if isinstance(erro, cadastro.ErroValidacao):
        print("Dados inválidos:", file=sys.stderr)
        for e in erro.erros:
            print(f"  {e['campo']}: {e['mensagem']}", file=sys.stderr)
    else:
        print(str(erro), file=sys.stderr)
    sys.exit(1)


def cmd_produto(args):
    inicializar()
    try:
        if args.acao == "listar":
            itens = cadastro.listar_produtos(cadastro.filtro_ativo(args.ativo))
            print("\n".join(_linha_produto(p) for p in itens) if itens
                  else "Nenhum produto encontrado.")
            return
        if args.acao == "criar":
            p = cadastro.criar_produto(_dados_produto(args), ator=ATOR_CLI)
            print(f"Produto {p['id']} criado.\n{_linha_produto(p)}")
            return
        alvo = cadastro.id_do_sku(args.sku_atual)
        if args.acao == "editar":
            p = cadastro.editar_produto(alvo, _dados_produto(args), ator=ATOR_CLI)
            print(f"Produto {p['id']} alterado.\n{_linha_produto(p)}")
        else:
            p = cadastro.desativar_produto(alvo, ator=ATOR_CLI)
            print(f"Produto {p['id']} desativado (nada foi apagado).\n{_linha_produto(p)}")
    except (cadastro.ErroValidacao, cadastro.Conflito, cadastro.NaoEncontrado) as e:
        _recusar(e)


def cmd_fornecedor(args):
    inicializar()
    try:
        if args.acao == "listar":
            itens = cadastro.listar_fornecedores(cadastro.filtro_ativo(args.ativo))
            print("\n".join(_linha_fornecedor(f) for f in itens) if itens
                  else "Nenhum fornecedor encontrado.")
            return
        if args.acao == "criar":
            f = cadastro.criar_fornecedor(_dados_fornecedor(args), ator=ATOR_CLI)
            print(f"Fornecedor {f['id']} criado.\n{_linha_fornecedor(f)}")
            return
        alvo = cadastro.id_do_texto(args.id, "Fornecedor")
        if args.acao == "editar":
            f = cadastro.editar_fornecedor(alvo, _dados_fornecedor(args), ator=ATOR_CLI)
            print(f"Fornecedor {f['id']} alterado.\n{_linha_fornecedor(f)}")
        else:
            f = cadastro.desativar_fornecedor(alvo, ator=ATOR_CLI)
            print(f"Fornecedor {f['id']} desativado (nada foi apagado).\n{_linha_fornecedor(f)}")
        if f.get("aviso"):
            print(f"Aviso: {f['aviso']}")
    except (cadastro.ErroValidacao, cadastro.Conflito, cadastro.NaoEncontrado) as e:
        _recusar(e)


def cmd_pedido(args):
    """Reanálise de um pedido em PROBLEMA (worker.reanalisar): a mesma
    validação e as mesmas mensagens da rota do painel."""
    from worker import reanalisar

    inicializar()
    try:
        alvo = cadastro.id_do_texto(args.id, "Pedido")
        dados = {} if args.moeda_venda is None else {"moeda_venda": args.moeda_venda}
        r = reanalisar(alvo, dados, ator=ATOR_CLI)
    except (cadastro.ErroValidacao, cadastro.Conflito, cadastro.NaoEncontrado) as e:
        _recusar(e)
    if dados:
        print(f"Moeda da venda do pedido {alvo} registrada: {r['moeda_venda']}.")
    print(r["mensagem"])


def _opcao_listar(p):
    p.add_argument("--ativo", choices=("true", "false"),
                   help="só os ativos (true) ou só os desativados (false); sem ela, todos")


def _opcoes_produto(p, edicao: bool):
    if edicao:
        p.add_argument("sku_atual", metavar="SKU", help="SKU do produto a alterar")
    p.add_argument("--sku", help="novo SKU" if edicao else "SKU (único)")
    p.add_argument("--titulo")
    p.add_argument("--custo", help="custo do fornecedor, com ponto: 18.50 (pede --moeda)")
    p.add_argument("--moeda", help="moeda do custo, ISO 4217: "
                                   + ", ".join(sorted(dinheiro.MOEDAS_ACEITAS)))
    p.add_argument("--peso", help="peso em kg, com ponto: 0.4")
    vinculo = p.add_mutually_exclusive_group()
    vinculo.add_argument("--fornecedor", type=_inteiro_ou_texto, help="id do fornecedor (ativo)")
    if edicao:
        vinculo.add_argument("--sem-fornecedor", action="store_true",
                             help="tira o fornecedor do produto")
        p.add_argument("--reativar", action="store_true")
    p.add_argument("--categoria-ml", help='categoria do Mercado Livre ("" limpa)')
    p.add_argument("--categoria-regulada",
                   help=f"{conformidade.SEM_CATEGORIA_REGULADA}, uma categoria regulada, ou limpar")
    p.add_argument("--habilitacao", choices=sorted(_SIM_NAO),
                   help="habilitação da categoria regulada em dia")
    p.add_argument("--reembalagem", choices=sorted(_SIM_NAO),
                   help="sai sem o nome do fornecedor (pedido Amazon)")


def _opcoes_fornecedor(p, edicao: bool):
    if edicao:
        p.add_argument("id", help="id do fornecedor")
    p.add_argument("--nome")
    p.add_argument("--canal", help=", ".join(cadastro.CANAIS))
    p.add_argument("--contato")
    p.add_argument("--prazo", type=_inteiro_ou_texto,
                   help=f"prazo em dias ({cadastro.PRAZO_MINIMO} a {cadastro.PRAZO_MAXIMO})")
    minimo = p.add_mutually_exclusive_group()
    minimo.add_argument("--pedido-minimo", help="valor, com ponto: 100.00 (pede --moeda)")
    p.add_argument("--moeda", help="moeda do pedido mínimo, ISO 4217")
    p.add_argument("--observacoes", help='texto livre ("" limpa)')
    if edicao:
        minimo.add_argument("--sem-pedido-minimo", action="store_true")
        p.add_argument("--reativar", action="store_true")


def cmd_eventos(args):
    with conectar() as conn:
        linhas = conn.execute(
            "SELECT nivel, origem, mensagem, ocorrido_em FROM eventos"
            " ORDER BY id DESC LIMIT ?", (args.n,),
        ).fetchall()
    for l in reversed(linhas):
        print(f"  {l['ocorrido_em'][11:19]} [{l['nivel']:<7}] {l['origem']:<14} {l['mensagem']}")


def main():
    p = argparse.ArgumentParser(description="Agente comercial", formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(func=cmd_init)
    sub.add_parser("pendencias").set_defaults(func=cmd_pendencias)
    sub.add_parser("ciclo").set_defaults(func=cmd_ciclo)

    a = sub.add_parser("aprovar"); a.add_argument("ids", type=int, nargs="+"); a.set_defaults(func=cmd_aprovar)
    r = sub.add_parser("recusar"); r.add_argument("id", type=int); r.add_argument("motivo", nargs="?", default=""); r.set_defaults(func=cmd_recusar)

    n = sub.add_parser("nichos"); n.add_argument("termos", nargs="+"); n.add_argument("--nicho", default=""); n.set_defaults(func=cmd_nichos)

    pr = sub.add_parser("preco")
    pr.add_argument("custo", type=float); pr.add_argument("--margem", type=float, default=25.0)
    pr.add_argument("--peso", type=float, default=0.3); pr.add_argument("--tipo", default="classico")
    pr.set_defaults(func=cmd_preco)

    m = sub.add_parser("margem")
    m.add_argument("preco", type=float); m.add_argument("custo", type=float)
    m.add_argument("--peso", type=float, default=0.3); m.add_argument("--tipo", default="classico")
    m.set_defaults(func=cmd_margem)

    ro = sub.add_parser("rodar"); ro.add_argument("--intervalo", type=int, default=None); ro.set_defaults(func=cmd_rodar)
    ev = sub.add_parser("eventos"); ev.add_argument("-n", type=int, default=25); ev.set_defaults(func=cmd_eventos)

    op = sub.add_parser("operador"); op.add_argument("--usuario", required=True)
    op.set_defaults(func=cmd_operador)

    co = sub.add_parser("cofre", help="cofre de credenciais")
    acoes = co.add_subparsers(dest="acao", required=True)
    acoes.add_parser("migrar", help="copia segredos do .env e tokens antigos para o cofre")
    acoes.add_parser("rotacionar", help="recifra tudo com a chave atual")
    acoes.add_parser("listar", help="nomes e datas, nunca valores")
    ap = acoes.add_parser("apagar", help="remove uma credencial do cofre, ou todas com --tudo")
    ap.add_argument("provedor", nargs="?"); ap.add_argument("nome", nargs="?")
    ap.add_argument("--tudo", action="store_true",
                    help="apaga todas as credenciais sem precisar da chave (pede confirmação)")
    co.set_defaults(func=cmd_cofre)

    pd = sub.add_parser("produto", help="cadastro de produtos (sem SQL)")
    acoes_pd = pd.add_subparsers(dest="acao", required=True)
    _opcao_listar(acoes_pd.add_parser("listar", help="todos, com os desativados marcados"))
    _opcoes_produto(acoes_pd.add_parser("criar"), edicao=False)
    _opcoes_produto(acoes_pd.add_parser("editar", help="só o que for informado muda"), edicao=True)
    acoes_pd.add_parser("desativar", help="desativa sem apagar").add_argument("sku_atual", metavar="SKU")
    pd.set_defaults(func=cmd_produto)

    fo = sub.add_parser("fornecedor", help="cadastro de fornecedores (sem SQL)")
    acoes_fo = fo.add_subparsers(dest="acao", required=True)
    _opcao_listar(acoes_fo.add_parser("listar", help="todos, com os desativados marcados"))
    _opcoes_fornecedor(acoes_fo.add_parser("criar"), edicao=False)
    _opcoes_fornecedor(acoes_fo.add_parser("editar", help="só o que for informado muda"), edicao=True)
    acoes_fo.add_parser("desativar", help="desativa sem apagar").add_argument("id")
    fo.set_defaults(func=cmd_fornecedor)

    pe = sub.add_parser("pedido", help="pedido em PROBLEMA: reanalisar depois de corrigir o cadastro")
    acoes_pe = pe.add_subparsers(dest="acao", required=True)
    re_ = acoes_pe.add_parser("reanalisar", help="volta o pedido para NOVO; o próximo ciclo refaz a análise")
    re_.add_argument("id", help="id do pedido")
    re_.add_argument("--moeda-venda", help="moeda da venda, ISO 4217, só para pedido sem moeda: "
                                           + ", ".join(sorted(dinheiro.MOEDAS_ACEITAS)))
    pe.set_defaults(func=cmd_pedido)

    args = p.parse_args()
    # Antes de qualquer gravação: sem AGENTE_DADOS, diz qual pasta vai ser usada.
    avisar_pasta_de_dados()
    try:
        args.func(args)
    except Exception as e:
        print(f"Erro: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
