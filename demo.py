"""Popula o banco com dados de exemplo e roda o fluxo sem tocar em API nenhuma."""
import sys; sys.path.insert(0, '.')
from db import inicializar, conectar, agora
from core.estados import Estado, historico
from core import aprovacao
from worker import analisar_novos, montar_ordens_de_compra, EXECUTORES

inicializar()
with conectar() as c:
    c.execute("DELETE FROM transicoes"); c.execute("DELETE FROM aprovacoes")
    c.execute("DELETE FROM pedidos"); c.execute("DELETE FROM produtos"); c.execute("DELETE FROM fornecedores")
    c.execute("INSERT INTO fornecedores (id,nome,canal,contato,prazo_dias) VALUES (1,'Fábrica Aurora','email','pedidos@fabrica-aurora.example',4)")
    c.execute("INSERT INTO fornecedores (id,nome,canal,contato,prazo_dias) VALUES (2,'Metalpar','whatsapp','+55 00 00000-0000',12)")
    c.execute("INSERT INTO produtos (id,sku,titulo,custo_fornecedor,peso_kg,fornecedor_id,criado_em) VALUES (1,'ORG-001','Organizador de gaveta 6 divisórias',18.50,0.4,1,?)",(agora(),))
    c.execute("INSERT INTO produtos (id,sku,titulo,custo_fornecedor,peso_kg,fornecedor_id,criado_em) VALUES (2,'LUM-114','Luminária de mesa articulada',62.00,1.2,1,?)",(agora(),))
    c.execute("INSERT INTO produtos (id,sku,titulo,custo_fornecedor,peso_kg,fornecedor_id,criado_em) VALUES (3,'SUP-300','Suporte de monitor em aço',95.00,3.0,2,?)",(agora(),))
    # 4 pedidos: margem boa / margem ruim / acima do teto / fornecedor lento
    for eid, pid, val in [('2000000001',1,54.90), ('2000000002',1,26.00), ('2000000003',2,289.00), ('2000000004',3,198.00)]:
        c.execute("INSERT INTO pedidos (marketplace,id_externo,produto_id,quantidade,valor_bruto,estado,comprador_nome,endereco_json,criado_em,atualizado_em) VALUES ('mercadolivre',?,?,1,?,?,'Comprador Teste','{}',?,?)",
                  (eid,pid,val,Estado.NOVO.value,agora(),agora()))

print("=== 1. ANÁLISE DE MARGEM ===")
print(f"  {analisar_novos()} pedido(s) aprovados na margem\n")
with conectar() as c:
    for p in c.execute("SELECT id_externo,valor_bruto,margem_prevista,estado FROM pedidos ORDER BY id"):
        m = f"{p['margem_prevista']}%" if p['margem_prevista'] is not None else "—"
        print(f"  {p['id_externo']}  R$ {p['valor_bruto']:>7.2f}  margem {m:>8}  →  {p['estado']}")

print("\n=== 2. MONTAGEM DAS ORDENS DE COMPRA ===")
print(f"  {montar_ordens_de_compra()} ordem(ns) na fila\n")
for a in aprovacao.pendentes():
    print(f"  [{a.id}] {a.resumo}")

print("\n=== 3. APROVANDO A PRIMEIRA (simula seu 'pode comprar') ===")
ids = [a.id for a in aprovacao.pendentes()]
if ids:
    print("  " + aprovacao.aprovar(ids[0], EXECUTORES))
    with conectar() as c:
        pid = c.execute("SELECT pedido_id FROM aprovacoes WHERE id=?", (ids[0],)).fetchone()[0]
    print("\n  Histórico do pedido:")
    for h in historico(pid):
        auto = "auto" if h['automatico'] else "VOCÊ"
        print(f"    {h['de']} → {h['para']:<22} [{auto}] {h['motivo']}")
