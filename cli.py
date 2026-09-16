#!/usr/bin/env python3
"""
Painel de comando do robô.

  python cli.py init                           cria o banco
  python cli.py pendencias                     o que está esperando você
  python cli.py aprovar 7                      aprova e executa a ação 7
  python cli.py aprovar 7 8 9                  em lote
  python cli.py recusar 7 "custo subiu"        recusa com motivo
  python cli.py nichos "termo a" "termo b"     análise de oportunidade
  python cli.py preco 30 --peso 0.4            sugere preço pra um custo
  python cli.py margem 89.90 30 --peso 0.4     decompõe uma venda
  python cli.py ciclo                          roda uma passada do worker
  python cli.py rodar --intervalo 300          roda em laço contínuo
  python cli.py eventos                        últimos alertas
  python cli.py operador --usuario ana         cria o operador ou troca a senha
"""
import argparse
import sys

from db import inicializar, conectar
from core import aprovacao
from inteligencia import precificacao, tendencias


def cmd_init(_):
    inicializar()
    print("Banco criado. Cadastre fornecedores e produtos antes de ligar o worker.")


def cmd_pendencias(_):
    itens = aprovacao.pendentes()
    if not itens:
        print("Nada pendente.")
        return
    print(f"{len(itens)} ação(ões) esperando sua decisão:\n")
    total = 0.0
    for a in itens:
        valor = f"R$ {a.valor:.2f}" if a.valor else "—"
        print(f"  [{a.id}] {a.tipo:<20} {valor:>12}  {a.resumo}")
        total += a.valor or 0
    print(f"\n  Exposição total se você aprovar tudo: R$ {total:.2f}")


def cmd_aprovar(args):
    from worker import EXECUTORES
    ok, falhas = aprovacao.aprovar_em_lote(args.ids, EXECUTORES)
    for i in ok:
        print(f"  ✓ {i} executada")
    for i, erro in falhas:
        print(f"  ✗ {i} falhou: {erro}")


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

    ro = sub.add_parser("rodar"); ro.add_argument("--intervalo", type=int, default=300); ro.set_defaults(func=cmd_rodar)
    ev = sub.add_parser("eventos"); ev.add_argument("-n", type=int, default=25); ev.set_defaults(func=cmd_eventos)

    op = sub.add_parser("operador"); op.add_argument("--usuario", required=True)
    op.set_defaults(func=cmd_operador)

    args = p.parse_args()
    try:
        args.func(args)
    except Exception as e:
        print(f"Erro: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
