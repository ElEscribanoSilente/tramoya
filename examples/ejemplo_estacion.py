"""
ESTACION ESPACIAL TRAMOYA-1 (v2)
=================================

Simulacion de una estacion espacial con multiples sistemas interconectados.

Fixes vs v1:
  1. Transiciones ambiguas resueltas: reparar/emergencia guardan ubicacion_previa
     en ctx, y guards la usan para volver al lugar correcto.
  2. Estado global eliminado: todo vive en ctx de la maquina. Sin dict externo.
     Hooks y acciones solo tocan ctx, nunca estado global.
  3. undo() revierte mundo completo: como todo esta en ctx y tramoya v1.3
     hace deepcopy del ctx en cada transicion, undo restaura recursos, dia, etc.
  4. destruccion se auto-verifica: check_vital() se llama despues de cada dia
     y cada evento. Si recursos criticos llegan a 0, se dispara automaticamente.
  5. RNG serializado: se guarda/restaura random.getstate() en ctx para
     reproducibilidad exacta al cargar partida.
  6. trigger_many se usa en la simulacion (secuencia de lanzamiento).

Usa TODAS las features de tramoya:
  Machine directa, MachineBuilder, SubMachine, Guards, Wildcards,
  Internal transitions, Observers, is_stuck(), trigger_many, Undo,
  Serialization con round-trip, to_mermaid().
"""

import copy
import json
import random
from tramoya import Machine, MachineBuilder, SubMachine, GuardRejected

random.seed(2026)


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  UTILIDADES (sin estado — solo lectura de ctx)                         ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def barra(valor, maximo=100, ancho=20):
    lleno = max(0, int(valor / maximo * ancho))
    return f"[{'#' * lleno}{'.' * (ancho - lleno)}] {valor}/{maximo}"

def panel(ctx):
    print(f"\n{'_' * 60}")
    print(f"  ESTACION TRAMOYA-1 | Dia {ctx['dia']}")
    print(f"  Energia:     {barra(ctx['energia'])}")
    print(f"  Oxigeno:     {barra(ctx['oxigeno'])}")
    print(f"  Combustible: {barra(ctx['combustible'])}")
    print(f"  Integridad:  {barra(ctx['integridad'])}")
    print(f"  Comida:      {barra(ctx['comida'])}")
    print(f"  Moral:       {barra(ctx['moral'])}")
    print(f"  Tripulacion: {ctx['tripulacion']}/{ctx['tripulacion_max']}")
    print(f"{'_' * 60}")

def log(ctx, msg):
    """Append to ctx log and print."""
    ctx.setdefault("eventos", []).append(f"[Dia {ctx.get('dia', '?')}] {msg}")
    print(f"  >> {msg}")

def clamp(ctx, key, lo=0, hi=100):
    ctx[key] = max(lo, min(hi, ctx[key]))


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  SISTEMA DE REPARACIONES (SubMachine)                                  ║
# ╚══════════════════════════════════════════════════════════════════════════╝

reparacion = Machine(
    states=["diagnostico", "desmontaje", "soldadura", "prueba", "completado", "fallido"],
    transitions=[
        ("analizar",    "diagnostico", "desmontaje"),
        ("desmontar",   "desmontaje",  "soldadura",   lambda c: c.get("herramientas", 0) > 0),
        ("desmontar",   "desmontaje",  "fallido",     lambda c: c.get("herramientas", 0) <= 0),
        ("soldar",      "soldadura",   "prueba"),
        ("verificar",   "prueba",      "completado",  lambda c: random.random() < 0.8),
        ("verificar",   "prueba",      "soldadura"),  # fallo -> reintentar
        ("escanear",    "diagnostico", None, None,
         lambda c: log(c, f"Escaneando modulo: {c.get('modulo', '?')}...")),
    ],
    initial="diagnostico",
    on_enter={
        "completado": lambda c: log(c, f"Reparacion de '{c.get('modulo', '?')}' completada!"),
        "fallido":    lambda c: log(c, "Reparacion fallida: sin herramientas"),
    },
)

sub_reparacion = SubMachine("reparando", reparacion, shared_keys=["herramientas"])


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  MAQUINA DE MISION PRINCIPAL                                           ║
# ╚══════════════════════════════════════════════════════════════════════════╝

ESTADOS_NAVEGACION = {"orbita_baja", "transito", "orbita_destino"}

mb = MachineBuilder("pre_lanzamiento")
mb.add_states(
    "pre_lanzamiento", "lanzamiento",
    "orbita_baja", "transito", "orbita_destino",
    "exploracion", "reparando", "emergencia",
    "regreso", "reentrada", "aterrizaje", "perdida",
)

# ── Flujo principal ──

mb.transition("lanzar",          "pre_lanzamiento", "lanzamiento")
mb.transition("alcanzar_orbita", "lanzamiento",     "orbita_baja")
mb.transition("iniciar_viaje",   "orbita_baja",     "transito")
mb.transition("llegar",          "transito",        "orbita_destino")
mb.transition("explorar",        "orbita_destino",  "exploracion")
mb.transition("regresar_orbita", "exploracion",     "orbita_destino")
mb.transition("iniciar_regreso", "orbita_destino",  "regreso")
mb.transition("reentrar",        "regreso",         "reentrada")
mb.transition("aterrizar",       "reentrada",       "aterrizaje")

# ── Reparacion: una sola transicion ida, una vuelta con guard ──

mb.transition("reparar",         "orbita_baja",     "reparando")
mb.transition("reparar",         "orbita_destino",  "reparando")
mb.transition("reparar",         "transito",        "reparando")

# FIX #1: Una transicion por destino, con guard que checa ubicacion_previa
mb.transition("fin_reparacion",  "reparando",       "orbita_baja")
mb.transition("fin_reparacion",  "reparando",       "orbita_destino")
mb.transition("fin_reparacion",  "reparando",       "transito")

# ── Emergencia: wildcard ida, vuelta guarded ──

mb.transition("emergencia",      "*",               "emergencia")
mb.transition("resolver",        "emergencia",      "orbita_baja")
mb.transition("resolver",        "emergencia",      "orbita_destino")
mb.transition("resolver",        "emergencia",      "transito")

# ── Destruccion y sensores ──

mb.transition("destruccion",     "*",               "perdida")
mb.transition("sensores",        "*",               None)

# ── Guards ──

@mb.guard("lanzar", "pre_lanzamiento", "lanzamiento")
def g_lanzar(ctx):
    return ctx["combustible"] >= 30 and ctx["tripulacion"] >= 2

@mb.guard("iniciar_viaje", "orbita_baja", "transito")
def g_viaje(ctx):
    return ctx["combustible"] >= 20 and ctx["integridad"] >= 40

@mb.guard("explorar", "orbita_destino", "exploracion")
def g_eva(ctx):
    return ctx["oxigeno"] >= 30 and ctx["tripulacion"] >= 2

@mb.guard("iniciar_regreso", "orbita_destino", "regreso")
def g_regreso(ctx):
    return ctx["combustible"] >= 25

@mb.guard("aterrizar", "reentrada", "aterrizaje")
def g_aterrizar(ctx):
    return ctx["integridad"] >= 20 and ctx["energia"] >= 10

@mb.guard("destruccion", "*", "perdida")
def g_destruccion(ctx):
    return (ctx["integridad"] <= 0 or ctx["oxigeno"] <= 0
            or ctx["tripulacion"] <= 0 or ctx["energia"] <= 0)

# FIX #1: Guards para volver al lugar correcto
@mb.guard("fin_reparacion", "reparando", "orbita_baja")
def g_rep_orbita(ctx):
    return ctx.get("ubicacion_previa") == "orbita_baja"

@mb.guard("fin_reparacion", "reparando", "orbita_destino")
def g_rep_destino(ctx):
    return ctx.get("ubicacion_previa") == "orbita_destino"

@mb.guard("fin_reparacion", "reparando", "transito")
def g_rep_transito(ctx):
    return ctx.get("ubicacion_previa") == "transito"

@mb.guard("resolver", "emergencia", "orbita_baja")
def g_res_orbita(ctx):
    return ctx.get("ubicacion_previa") == "orbita_baja"

@mb.guard("resolver", "emergencia", "orbita_destino")
def g_res_destino(ctx):
    return ctx.get("ubicacion_previa") == "orbita_destino"

@mb.guard("resolver", "emergencia", "transito")
def g_res_transito(ctx):
    return ctx.get("ubicacion_previa") == "transito"

# ── Actions (FIX #2: todas mutan ctx, no globals) ──

@mb.on("lanzar", "pre_lanzamiento", "lanzamiento")
def a_lanzar(ctx):
    ctx["combustible"] -= 25
    ctx["dia"] += 1
    log(ctx, "DESPEGUE! Consumo de combustible: -25")

@mb.on("iniciar_viaje", "orbita_baja", "transito")
def a_viaje(ctx):
    ctx["combustible"] -= 15
    log(ctx, "Motor principal encendido. Rumbo al objetivo.")

@mb.on("explorar", "orbita_destino", "exploracion")
def a_explorar(ctx):
    ctx["oxigeno"] -= 10
    muestras = random.randint(1, 5)
    ctx["muestras_recolectadas"] += muestras
    log(ctx, f"EVA completada. {muestras} muestras recolectadas.")

@mb.on("iniciar_regreso", "orbita_destino", "regreso")
def a_regreso(ctx):
    ctx["combustible"] -= 20
    log(ctx, "Iniciando secuencia de retorno a Tierra.")

@mb.on("reentrar", "regreso", "reentrada")
def a_reentrada(ctx):
    dmg = random.randint(5, 15)
    ctx["integridad"] -= dmg
    log(ctx, f"Reentrada atmosferica. Dano por friccion: -{dmg}")

# ── Enter/Exit hooks ──

@mb.enter("orbita_baja")
def h_orbita(ctx):
    log(ctx, "Orbita terrestre estable alcanzada.")

@mb.enter("orbita_destino")
def h_destino(ctx):
    log(ctx, "En orbita del planeta objetivo. Sistemas nominales.")

@mb.enter("emergencia")
def h_emergencia(ctx):
    log(ctx, "!! ALERTA ROJA !! Todos a estaciones de emergencia!")
    ctx["moral"] -= 15
    clamp(ctx, "moral")

@mb.enter("aterrizaje")
def h_aterrizaje(ctx):
    log(ctx, "ATERRIZAJE EXITOSO!")

@mb.enter("perdida")
def h_perdida(ctx):
    log(ctx, "Contacto perdido con la estacion.")

# FIX #1: guardar ubicacion_previa al entrar a reparando/emergencia

@mb.enter("reparando")
def h_enter_rep(ctx):
    sub_reparacion.enter(ctx)

@mb.exit("reparando")
def h_exit_rep(ctx):
    sub_reparacion.exit(ctx)

# on_transition to track ubicacion_previa for interruptible states
@mb.on_transition_handler
def track_ubicacion(trigger, src, dst, ctx):
    if dst in ("reparando", "emergencia") and src in ESTADOS_NAVEGACION:
        ctx["ubicacion_previa"] = src

# ── Observer: bitacora ──

bitacora = []

@mb.observe
def obs_bitacora(trigger, src, dst, ctx):
    entrada = f"Dia {ctx.get('dia', 0):>3} | {src:>18} --[{trigger}]--> {dst}"
    bitacora.append(entrada)


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  CONSTRUCCION                                                          ║
# ╚══════════════════════════════════════════════════════════════════════════╝

INITIAL_CTX = {
    "dia": 0,
    "energia": 100,
    "oxigeno": 100,
    "combustible": 80,
    "integridad": 100,
    "comida": 60,
    "moral": 80,
    "tripulacion": 4,
    "tripulacion_max": 6,
    "modulos_reparados": 0,
    "muestras_recolectadas": 0,
    "contacto_alien": False,
    "herramientas": 3,
    "ubicacion_previa": None,
    "eventos": [],
    "rng_state": None,
}

mision = mb.build(ctx=copy.deepcopy(INITIAL_CTX))


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  SIMULACION: FUNCIONES DE TURNO (FIX #2: todas mutan ctx)             ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def pasar_dia(ctx):
    """Consumo diario. Muta solo ctx."""
    ctx["dia"] += 1
    ctx["oxigeno"] -= 2
    ctx["comida"] -= 2 * ctx["tripulacion"]
    ctx["energia"] -= 3
    ctx["energia"] = min(100, ctx["energia"] + random.randint(3, 8))
    for k in ("energia", "oxigeno", "combustible", "integridad", "comida", "moral"):
        clamp(ctx, k)


def evento_aleatorio(ctx):
    """Genera evento y muta ctx. Retorna 'emergencia' o None."""
    tabla = [
        ("lluvia_meteoritos",    0.25),
        ("fallo_paneles",        0.20),
        ("fuga_oxigeno",         0.15),
        ("senal_extraterrestre", 0.10),
        ("moral_baja",           0.15),
        ("nada",                 0.15),
    ]
    roll = random.random()
    acumulado = 0
    evento = "nada"
    for nombre, prob in tabla:
        acumulado += prob
        if roll < acumulado:
            evento = nombre
            break

    if evento == "lluvia_meteoritos":
        dmg = random.randint(5, 20)
        ctx["integridad"] -= dmg
        clamp(ctx, "integridad")
        log(ctx, f"LLUVIA DE METEORITOS! Integridad -{dmg}")
        if ctx["integridad"] < 30:
            return "emergencia"
    elif evento == "fallo_paneles":
        ctx["energia"] -= random.randint(10, 25)
        clamp(ctx, "energia")
        log(ctx, "Fallo en paneles solares. Energia reducida.")
    elif evento == "fuga_oxigeno":
        ctx["oxigeno"] -= random.randint(8, 18)
        clamp(ctx, "oxigeno")
        log(ctx, "Fuga de oxigeno detectada en modulo 3!")
    elif evento == "senal_extraterrestre":
        if not ctx["contacto_alien"]:
            ctx["contacto_alien"] = True
            ctx["moral"] = min(100, ctx["moral"] + 10)
            log(ctx, "SENAL EXTRATERRESTRE DETECTADA!")
    elif evento == "moral_baja":
        ctx["moral"] -= random.randint(5, 12)
        clamp(ctx, "moral")
        log(ctx, "La tripulacion muestra signos de estres.")
    else:
        log(ctx, "Dia tranquilo. Sin incidentes.")
    return None


# FIX #4: verificacion automatica de destruccion
def check_vital(m):
    """Dispara destruccion si recursos criticos llegan a 0."""
    if m.can("destruccion") and m.state != "perdida":
        m.trigger("destruccion")
        return True
    return False


def ejecutar_reparacion(m, modulo):
    """Flujo completo de reparacion en SubMachine."""
    sub_reparacion.machine.ctx["modulo"] = modulo
    sub_reparacion.machine.ctx["herramientas"] = m.ctx.get("herramientas", 0)

    sub_reparacion.trigger("escanear")
    sub_reparacion.trigger("analizar")

    if sub_reparacion.machine.can("desmontar"):
        sub_reparacion.trigger("desmontar")
        sub_reparacion.trigger("soldar")
        intentos = 0
        while sub_reparacion.machine.state in ("prueba", "soldadura"):
            if sub_reparacion.machine.state == "soldadura" and intentos > 0:
                log(m.ctx, f"Reintentando soldadura (intento {intentos + 1})...")
                sub_reparacion.trigger("soldar")
            sub_reparacion.trigger("verificar")
            intentos += 1
            if intentos > 3:
                log(m.ctx, "Demasiados intentos fallidos.")
                break
        if sub_reparacion.machine.state == "completado":
            m.ctx["integridad"] = min(100, m.ctx["integridad"] + 15)
            m.ctx["modulos_reparados"] += 1
            m.ctx["herramientas"] -= 1
            return True
    else:
        sub_reparacion.trigger("desmontar")
    return False


def turno(m):
    """Pasa un dia + evento + check vital. Retorna True si sigue viva."""
    pasar_dia(m.ctx)
    ev = evento_aleatorio(m.ctx)
    if check_vital(m):
        return False
    if ev == "emergencia" and m.can("emergencia"):
        m.trigger("emergencia")
        log(m.ctx, "Equipo de emergencia desplegado.")
        m.ctx["integridad"] = max(m.ctx["integridad"], 25)
        pasar_dia(m.ctx)
        if check_vital(m):
            return False
        m.trigger("resolver")
        log(m.ctx, "Emergencia resuelta. Retomando curso.")
    return True


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  SIMULACION PRINCIPAL                                                  ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def main():
    print("=" * 60)
    print("  ESTACION ESPACIAL TRAMOYA-1 -- SIMULACION DE MISION v2")
    print("=" * 60)

    panel(mision.ctx)

    # ── FASE 1: Pre-lanzamiento ──────────────────────────────────────────

    print("\n  FASE 1: PRE-LANZAMIENTO")
    mision.trigger("sensores")  # internal
    log(mision.ctx, "Sensores: todos los sistemas nominales.")
    print(f"  Puede lanzar? {mision.can('lanzar')}")

    # FIX #6: trigger_many para secuencia de lanzamiento
    mision.trigger_many("lanzar", "alcanzar_orbita")
    panel(mision.ctx)

    # ── FASE 2: Orbita baja + reparacion ─────────────────────────────────

    print("\n  FASE 2: ORBITA TERRESTRE")
    if not turno(mision):
        return panel(mision.ctx)

    print("\n  Reparacion preventiva del escudo termico...")
    mision.trigger("reparar")
    exito = ejecutar_reparacion(mision, "escudo_termico")
    print(f"  Resultado: {'EXITO' if exito else 'FALLO'}")
    mision.trigger("fin_reparacion")
    panel(mision.ctx)

    # ── FASE 3: Transito ─────────────────────────────────────────────────

    print("\n  FASE 3: TRANSITO INTERPLANETARIO")
    mision.trigger("iniciar_viaje")

    for _ in range(3):
        if not turno(mision):
            return panel(mision.ctx)

    panel(mision.ctx)

    # FIX #5: guardar RNG state junto con la partida
    print("\n  [GUARDANDO PARTIDA...]")
    mision.ctx["rng_state"] = list(random.getstate()[1])  # serializable
    mision.ctx["rng_pos"] = random.getstate()[2]
    snapshot = mision.to_json()
    print(f"  Snapshot: {len(snapshot)} bytes (incluye RNG)")

    # ── FASE 4: Orbita destino ───────────────────────────────────────────

    print("\n  FASE 4: ORBITA DEL PLANETA OBJETIVO")
    mision.trigger("llegar")
    if not turno(mision):
        return panel(mision.ctx)
    panel(mision.ctx)

    # ── FASE 5: Exploracion ──────────────────────────────────────────────

    print("\n  FASE 5: EXPLORACION")
    num_evas = 0
    while mision.can("explorar") and num_evas < 3:
        mision.trigger("explorar")
        num_evas += 1
        if not turno(mision):
            return panel(mision.ctx)
        mision.trigger("regresar_orbita")

    print(f"\n  Total EVAs: {num_evas}")
    print(f"  Muestras: {mision.ctx['muestras_recolectadas']}")
    panel(mision.ctx)

    print(f"\n  is_stuck? {mision.is_stuck()}")
    if not mision.can("iniciar_regreso"):
        log(mision.ctx, "Combustible insuficiente! Intentando reparar...")
        if mision.can("reparar"):
            mision.trigger("reparar")
            ejecutar_reparacion(mision, "motor_principal")
            mision.trigger("fin_reparacion")

    # ── FASE 6: Regreso ──────────────────────────────────────────────────

    print("\n  FASE 6: REGRESO A TIERRA")
    mision.trigger("iniciar_regreso")

    for _ in range(2):
        if not turno(mision):
            return panel(mision.ctx)

    panel(mision.ctx)

    # ── FASE 7: Reentrada + aterrizaje ───────────────────────────────────

    print("\n  FASE 7: REENTRADA ATMOSFERICA")
    # FIX #6: trigger_many para secuencia final
    mision.trigger("reentrar")
    print(f"  Puede aterrizar? {mision.can('aterrizar')}")

    if mision.can("aterrizar"):
        mision.trigger("aterrizar")
        print("\n" + "=" * 60)
        print("  MISION COMPLETADA")
        print(f"  Muestras: {mision.ctx['muestras_recolectadas']}")
        print(f"  Reparaciones: {mision.ctx['modulos_reparados']}")
        print(f"  Tripulacion: {mision.ctx['tripulacion']}/{mision.ctx['tripulacion_max']}")
        print(f"  Moral: {mision.ctx['moral']}")
        print("=" * 60)
    else:
        log(mision.ctx, "Integridad insuficiente para aterrizaje!")

    panel(mision.ctx)

    # ── FIX #3: UNDO revierte mundo completo ─────────────────────────────

    print("\n  [UNDO: Que hubiera pasado sin aterrizar?]")
    pre = mision.state
    mision.undo()
    print(f"  {pre} -> {mision.state}")
    print(f"  Integridad restaurada: {mision.ctx['integridad']}")
    print(f"  Dia restaurado: {mision.ctx['dia']}")
    # Avanzar de nuevo
    mision.trigger("aterrizar")
    print(f"  Re-aterrizado: {mision.state}")

    # ── FIX #5: CARGAR PARTIDA con RNG ───────────────────────────────────

    print("\n  [CARGANDO PARTIDA GUARDADA...]")
    saved = json.loads(snapshot)
    mision2 = mb.build(ctx=copy.deepcopy(INITIAL_CTX))
    mision2.load_dict(saved)

    # Restaurar RNG
    rng_internalstate = tuple(mision2.ctx.get("rng_state", []))
    rng_pos = mision2.ctx.get("rng_pos", 0)
    random.setstate((3, rng_internalstate, rng_pos))

    print(f"  Estado: {mision2.state}, Dia: {mision2.ctx['dia']}")
    print(f"  Combustible: {mision2.ctx['combustible']}")
    print(f"  RNG restaurado: siguiente random = {random.random():.4f}")

    # ── BITACORA ─────────────────────────────────────────────────────────

    print(f"\n{'=' * 60}")
    print("  BITACORA DE MISION")
    print(f"{'=' * 60}")
    for entrada in bitacora:
        print(f"  {entrada}")

    # ── LOG DE EVENTOS ───────────────────────────────────────────────────

    eventos = mision.ctx.get("eventos", [])
    print(f"\n{'=' * 60}")
    print(f"  LOG DE EVENTOS ({len(eventos)} entradas)")
    print(f"{'=' * 60}")
    for ev in eventos:
        print(f"  {ev}")

    # ── ESTADISTICAS ─────────────────────────────────────────────────────

    c = mision.ctx
    print(f"\n{'=' * 60}")
    print("  ESTADISTICAS FINALES")
    print(f"{'=' * 60}")
    print(f"  Duracion:          {c['dia']} dias")
    print(f"  Muestras:          {c['muestras_recolectadas']}")
    print(f"  Reparaciones:      {c['modulos_reparados']}")
    print(f"  Contacto alien:    {'Si' if c['contacto_alien'] else 'No'}")
    print(f"  Tripulacion:       {c['tripulacion']}/{c['tripulacion_max']}")
    print(f"  Moral:             {c['moral']}")
    print(f"  Transiciones:      {len(bitacora)}")
    print(f"  Estado final:      {mision.state}")
    print(f"  is_final:          {mision.is_final}")
    print(f"  is_stuck:          {mision.is_stuck()}")

    # ── MERMAID ──────────────────────────────────────────────────────────

    print(f"\n{'=' * 60}")
    print("  DIAGRAMA MERMAID")
    print(f"{'=' * 60}")
    print(mision.to_mermaid())


if __name__ == "__main__":
    main()
