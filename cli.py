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
"""
import argparse
import sys

from config import avisar_pasta_de_dados, config
from db import inicializar, conectar
from core import aprovacao, conformidade, privacidade
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
