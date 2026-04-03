"""
Sistema de pedidos de una taquería usando tramoya.

Estados: pedido -> cocina -> listo -> entregado (o cancelado desde cualquier punto).
"""

from tramoya import Machine, MachineBuilder

# ── Builder API ──────────────────────────────────────────────────────────────

b = MachineBuilder("pedido")
b.add_states("pedido", "cocina", "listo", "entregado", "cancelado")

b.transition("preparar",  "pedido",  "cocina")
b.transition("terminar",  "cocina",  "listo")
b.transition("entregar",  "listo",   "entregado")
b.transition("cancelar",  "*",       "cancelado")

@b.guard("entregar", "listo", "entregado")
def cobrado(ctx):
    return ctx.get("pagado", False)

@b.on("preparar", "pedido", "cocina")
def mostrar_ticket(ctx):
    items = ", ".join(ctx.get("items", []))
    print(f"  TICKET #{ctx.get('id', '?')}: {items}")

@b.enter("listo")
def gritar(ctx):
    print(f"  >> PEDIDO #{ctx.get('id', '?')} LISTO!")

@b.enter("cancelado")
def reembolso(ctx):
    if ctx.get("pagado"):
        print(f"  $$ Reembolso pedido #{ctx.get('id', '?')}")

taqueria = b.build(ctx={"id": 1, "items": ["3 al pastor", "1 suadero", "agua de horchata"]})

# ── Flujo normal ─────────────────────────────────────────────────────────────

print("=== Taqueria La Tramoya ===\n")

print(f"Estado: {taqueria.state}")
taqueria.trigger("preparar")
print(f"Estado: {taqueria.state}")

taqueria.trigger("terminar")
print(f"Estado: {taqueria.state}")

# Intentar entregar sin pagar
print(f"\n  Puede entregar? {taqueria.can('entregar')}")
taqueria.ctx["pagado"] = True
print(f"  Pago recibido. Puede entregar? {taqueria.can('entregar')}")

taqueria.trigger("entregar")
print(f"Estado: {taqueria.state}")

# ── Undo ─────────────────────────────────────────────────────────────────────

print(f"\nOops, el cliente cambio de opinion...")
taqueria.undo()
print(f"Estado: {taqueria.state}, pagado={taqueria.ctx.get('pagado')}")

# ── Cancelar ─────────────────────────────────────────────────────────────────

taqueria.trigger("cancelar")
print(f"Estado: {taqueria.state}")

# ── Snapshot ─────────────────────────────────────────────────────────────────

print(f"\nJSON: {taqueria.to_json()}")
print(f"\nHistorial: {taqueria.history}")
print(f"Dead end? {taqueria.is_final}")
