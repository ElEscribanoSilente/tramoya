"""
Dungeon crawler con tramoya.

Usa: SubMachine, observers, guards, wildcards, internal transitions,
     undo, is_stuck, trigger_many, serialization, Mermaid export.
"""

import json
import random
from tramoya import Machine, MachineBuilder, SubMachine

random.seed(42)

# ── Combate (SubMachine) ────────────────────────────────────────────────────

combate = Machine(
    states=["inicio", "turno_jugador", "turno_enemigo", "victoria", "derrota"],
    transitions=[
        ("comenzar",    "inicio",         "turno_jugador"),
        ("atacar",      "turno_jugador",  "turno_enemigo", lambda c: c.get("enemigo_hp", 0) > 0),
        ("atacar",      "turno_jugador",  "victoria",      lambda c: c.get("enemigo_hp", 0) <= 0),
        ("recibir",     "turno_enemigo",  "turno_jugador", lambda c: c.get("hp", 0) > 0),
        ("recibir",     "turno_enemigo",  "derrota",       lambda c: c.get("hp", 0) <= 0),
        ("huir",        "turno_jugador",  "victoria",      lambda c: random.random() < 0.5),
        ("huir",        "turno_jugador",  "derrota",       lambda c: True),  # fallback: no escapaste
        ("curar",       "turno_jugador",  None,            None,
         lambda c: c.update({"hp": min(c["hp"] + 20, c["max_hp"])}) or None),
    ],
    initial="inicio",
    on_enter={
        "victoria": lambda c: print(f"    ** Victoria! +{c.get('xp_reward', 0)} XP"),
        "derrota":  lambda c: print("    ** Has sido derrotado..."),
    },
)

sub_combate = SubMachine("combate", combate, shared_keys=["hp", "max_hp", "atk", "nivel"])

# ── Juego principal ─────────────────────────────────────────────────────────

b = MachineBuilder("pueblo")
b.add_states("pueblo", "bosque", "cueva", "combate", "jefe", "fin")

b.transition("explorar",    "pueblo",   "bosque")
b.transition("avanzar",     "bosque",   "cueva")
b.transition("retroceder",  "bosque",   "pueblo")
b.transition("retroceder",  "cueva",    "bosque")
b.transition("entrar_jefe", "cueva",    "jefe")
b.transition("descansar",   "pueblo",   "pueblo")  # self-loop

# Encuentro aleatorio en bosque
b.transition("encuentro", "bosque", "combate")
b.transition("encuentro", "cueva",  "combate")

# Salir de combate
b.transition("volver", "combate", "bosque")
b.transition("volver", "combate", "cueva")

# Jefe final
b.transition("vencer_jefe", "jefe", "fin")

# Morir en cualquier momento
b.transition("morir", "*", "fin")

@b.guard("entrar_jefe", "cueva", "jefe")
def necesita_nivel(ctx):
    return ctx.get("nivel", 1) >= 3

@b.guard("vencer_jefe", "jefe", "fin")
def jefe_derrotado(ctx):
    return ctx.get("jefe_derrotado", False)

# "volver" regresa a donde estabas cuando salto el encuentro. Sin estos guards,
# la arista a "cueva" seria codigo muerto: la de "bosque", sin guard y
# registrada antes, ganaria siempre (Machine.lint() lo detecta como dead_edge).

@b.on("encuentro", "bosque", "combate")
def origen_bosque(ctx):
    ctx["origen"] = "bosque"

@b.on("encuentro", "cueva", "combate")
def origen_cueva(ctx):
    ctx["origen"] = "cueva"

@b.guard("volver", "combate", "bosque")
def volvia_del_bosque(ctx):
    return ctx.get("origen") == "bosque"

@b.guard("volver", "combate", "cueva")
def volvia_de_la_cueva(ctx):
    return ctx.get("origen") == "cueva"

@b.enter("pueblo")
def descansar(ctx):
    ctx["hp"] = ctx["max_hp"]
    print(f"  [Pueblo] HP restaurado a {ctx['hp']}")

@b.enter("bosque")
def entrar_bosque(ctx):
    print(f"  [Bosque] Escuchas criaturas entre los arboles...")

@b.enter("cueva")
def entrar_cueva(ctx):
    print(f"  [Cueva] La oscuridad te envuelve. Nivel: {ctx.get('nivel', 1)}")

@b.enter("fin")
def final(ctx):
    if ctx.get("jefe_derrotado"):
        print("\n  ========================================")
        print("  ===  FELICIDADES! COMPLETASTE EL JUEGO  ===")
        print("  ========================================")
    elif ctx.get("hp", 0) <= 0:
        print("\n  GAME OVER")

@b.enter("combate")
def enter_combate(ctx):
    sub_combate.enter(ctx)

@b.exit("combate")
def exit_combate(ctx):
    sub_combate.exit(ctx)

juego = b.build(ctx={
    "hp": 100, "max_hp": 100, "atk": 15, "nivel": 1,
    "xp": 0, "oro": 50,
})

# ── Observer: log de aventura ───────────────────────────────────────────────

log_aventura = []

def registrar(trigger, src, dst, ctx):
    log_aventura.append(f"{src} --{trigger}--> {dst}")

juego.subscribe(registrar)

# ── Helpers ─────────────────────────────────────────────────────────────────

def resolver_combate(enemigo_nombre, enemigo_hp, enemigo_atk, xp):
    """Simula un combate completo en el sub-machine."""
    print(f"\n  === COMBATE: {enemigo_nombre} (HP:{enemigo_hp} ATK:{enemigo_atk}) ===")

    sub_combate.machine.ctx.update({
        "enemigo": enemigo_nombre,
        "enemigo_hp": enemigo_hp,
        "enemigo_atk": enemigo_atk,
        "xp_reward": xp,
    })
    sub_combate.trigger("comenzar")

    rondas = 0
    while sub_combate.machine.state in ("turno_jugador", "turno_enemigo"):
        rondas += 1
        ctx = sub_combate.machine.ctx

        if sub_combate.machine.state == "turno_jugador":
            # IA simple: curar si HP bajo, sino atacar
            if ctx["hp"] < 30 and ctx["hp"] < ctx["max_hp"]:
                sub_combate.trigger("curar")
                print(f"    Ronda {rondas}: Te curas -> HP={ctx['hp']}")
                continue

            dmg = ctx["atk"] + random.randint(-3, 5)
            ctx["enemigo_hp"] -= dmg
            print(f"    Ronda {rondas}: Atacas por {dmg} -> {enemigo_nombre} HP={max(0, ctx['enemigo_hp'])}")
            sub_combate.trigger("atacar")

        elif sub_combate.machine.state == "turno_enemigo":
            dmg = enemigo_atk + random.randint(-2, 3)
            ctx["hp"] -= dmg
            print(f"    Ronda {rondas}: {enemigo_nombre} ataca por {dmg} -> Tu HP={max(0, ctx['hp'])}")
            sub_combate.trigger("recibir")

    # Propagar resultado al juego
    resultado = sub_combate.machine.state
    juego.ctx["hp"] = sub_combate.machine.ctx["hp"]

    if resultado == "victoria":
        juego.ctx["xp"] += xp
        juego.ctx["oro"] += random.randint(10, 30)
        nivel_anterior = juego.ctx["nivel"]
        juego.ctx["nivel"] = 1 + juego.ctx["xp"] // 50
        if juego.ctx["nivel"] > nivel_anterior:
            juego.ctx["max_hp"] += 20
            juego.ctx["atk"] += 5
            print(f"    >> NIVEL {juego.ctx['nivel']}! HP max={juego.ctx['max_hp']}, ATK={juego.ctx['atk']}")

    return resultado


# ── Aventura ────────────────────────────────────────────────────────────────

print("=" * 50)
print("  TRAMOYA DUNGEON CRAWLER")
print("=" * 50)

stats = lambda: f"[HP:{juego.ctx['hp']}/{juego.ctx['max_hp']} ATK:{juego.ctx['atk']} Nv:{juego.ctx['nivel']} XP:{juego.ctx['xp']} Oro:{juego.ctx['oro']}]"

print(f"\n{stats()}")
print(f"Ubicacion: {juego.state}")

# Explorar el bosque
juego.trigger("explorar")
print(f"\n{stats()}")

# Primer combate
juego.trigger("encuentro")
resultado = resolver_combate("Goblin", 30, 8, 25)
juego.trigger("volver")  # vuelve al bosque
print(f"\n{stats()}")

# Avanzar a la cueva
juego.trigger("avanzar")
print(f"\n{stats()}")

# Segundo combate
juego.trigger("encuentro")
resultado = resolver_combate("Esqueleto", 45, 12, 35)
juego.trigger("volver")  # vuelve a la cueva
print(f"\n{stats()}")

# Intentar entrar al jefe
print(f"\n  Puede entrar al jefe? {juego.can('entrar_jefe')}")
print(f"  Stuck? {juego.is_stuck()}")

# Volver al pueblo a descansar (desde la cueva son dos pasos)
juego.trigger_many("retroceder", "retroceder")
juego.trigger("descansar")
print(f"\n{stats()}")

# Tercer combate para subir nivel
juego.trigger("explorar")
juego.trigger("encuentro")
resultado = resolver_combate("Troll", 60, 15, 40)
juego.trigger("volver")
print(f"\n{stats()}")

# Ir al jefe
juego.trigger_many("avanzar", "entrar_jefe")
print(f"\nUbicacion: {juego.state}")
print(f"  Puede entrar al jefe? Ya estas aqui! Nivel: {juego.ctx['nivel']}")

# Combate final (fuera del sub-machine, directo)
print(f"\n  === JEFE FINAL: Dragon Ancestral ===")
juego.ctx["jefe_derrotado"] = True  # simulamos victoria
juego.trigger("vencer_jefe")

# ── Resumen ─────────────────────────────────────────────────────────────────

print(f"\n{'=' * 50}")
print(f"  RESUMEN DE AVENTURA")
print(f"{'=' * 50}")
print(f"  {stats()}")
print(f"  Transiciones: {len(log_aventura)}")
for entry in log_aventura:
    print(f"    {entry}")

# ── Serializar ──────────────────────────────────────────────────────────────

print(f"\n  Guardando partida...")
save = juego.to_json()
print(f"  Guardado ({len(save)} bytes)")

# Cargar en nueva instancia
juego2 = b.build(ctx={"hp": 1, "max_hp": 1, "atk": 1, "nivel": 1, "xp": 0, "oro": 0})
juego2.load_dict(json.loads(save))
print(f"  Cargado: estado={juego2.state}, nivel={juego2.ctx['nivel']}, oro={juego2.ctx['oro']}")

# ── Undo ────────────────────────────────────────────────────────────────────

print(f"\n  Undo (volver antes del jefe)...")
juego.undo()
print(f"  Estado: {juego.state}, jefe_derrotado={juego.ctx.get('jefe_derrotado')}")

# ── Mermaid ─────────────────────────────────────────────────────────────────

print(f"\n  Diagrama Mermaid del juego:")
print(juego.to_mermaid())
