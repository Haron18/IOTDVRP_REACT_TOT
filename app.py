"""
Système Intelligent de Tracking GPS & DVRP Dynamique — Alger
==============================================================

Corrections apportées par rapport à la version générée initialement :

1. BUG BLOQUANT : `np.random.choice(events_pool, p=...)` sur une liste de dicts
   plantait toujours. → corrigé dans event_handler.py (tirage par index).
2. BUG DE SÉCURITÉ DES THREADS : le callback MQTT écrivait directement dans
   st.session_state depuis un thread réseau. → corrigé via une queue.Queue
   thread-safe, vidée uniquement depuis le thread principal (mqtt_manager.py).
3. BUG D'ALIGNEMENT D'INDEX : après avoir filtré `active_orders` par le temps
   écoulé, l'ancien index du DataFrame ne correspondait plus aux positions
   utilisées par OR-Tools (`route[i]`). → corrigé avec `.reset_index(drop=True)`.
4. MANQUE : aucune contrainte de capacité — un camion pouvait se voir assigner
   un poids illimité. → ajout d'une vraie contrainte de capacité (kg).
5. PERFORMANCE : chaque interaction relançait des appels réseau OSRM.
   → mise en cache (st.cache_data, TTL 5 min) dans dvrp_engine.py.
6. ROBUSTESSE : gestion du cas où OR-Tools ne trouve aucune solution
   (capacité totale insuffisante) au lieu de faire planter l'affichage.
7. MQTT désormais optionnel (case à cocher) plutôt que connecté d'office à
   chaque rechargement de page, ce qui évitait d'accumuler des connexions.
8. ÉVÉNEMENTS RÉELLEMENT ACTIFS : la version précédente se contentait de
   JOURNALISER les événements dynamiques (message informatif, sans effet sur
   les données). Ils modifient désormais vraiment l'état de la simulation
   (event_handler.apply_event_effect) — nouvelle commande, annulation, panne
   véhicule, pénalité de trafic, priorité relevée — ce qui déclenche une vraie
   réoptimisation OR-Tools au calcul suivant, au lieu d'un simple message.
9. ROBUSTESSE SUPPLÉMENTAIRE : bouton de réinitialisation des événements
   cumulés, résumé des effets actifs visible en permanence dans la barre
   latérale, et prise en compte du nombre de véhicules réellement disponibles
   (après pannes) dans les indicateurs et les messages d'erreur.
10. ROTATIONS MULTIPLES (capacité insuffisante) : un camion peut désormais
    effectuer plusieurs allers-retours au dépôt si la capacité totale ne
    suffit pas en un seul passage (nombre de rotations calculé automatiquement).
    Modélisé via des véhicules « virtuels » (camion x trajets max) pour
    OR-Tools, regroupés ensuite par camion physique (dvrp_engine.py :
    group_multi_trip_routes). Le temps de rechargement au dépôt entre deux
    rotations n'est pas modélisé (supposé instantané).
11. TRACKING & SIMULATION 100% CÔTÉ NAVIGATEUR (dvrp_map_component/) : Python calcule
    les itinéraires une seule fois par action réelle (démarrage, événement, changement
    de paramètre) et transmet toutes les commandes (avec leur release_time) au composant
    React. Ensuite, l'horloge, le déplacement des camions, l'apparition progressive des
    commandes, les KPI et le tableau sont entièrement calculés et affichés côté
    navigateur — aucune resynchronisation Python périodique pendant que ça tourne. Le
    composant ne renvoie une valeur à Python que lorsqu'une commande est réellement
    livrée (pour la liste des commandes annulables), jamais par sondage.
"""

import math
import time
from datetime import datetime

import numpy as np
import pandas as pd
import streamlit as st

from dvrp_map_component import dvrp_map
from benchmark_loader import COLUMNS, get_real_algiers_dataset, generate_solomon_benchmark
from dvrp_engine import (
    get_osrm_distance_matrix,
    get_osrm_route_shape,
    group_multi_trip_routes,
    naive_baseline_routes,
    route_distance,
    solve_dvrp_ortools,
)
from event_handler import apply_event_effect, process_dynamic_event
from mqtt_manager import MQTTBridge

st.set_page_config(page_title="DVRP Logistique & Tracking Alger", page_icon="🚚", layout="wide")

# ----------------------------------------------------------------------------
# 1. ÉTAT DE SESSION
# ----------------------------------------------------------------------------
DEFAULTS = {
    "logs": [],
    "mqtt_events": [],
    "mqtt_bridge": None,
    "extra_orders": [],       # commandes ajoutées par des événements NOUVELLE_COMMANDE
    "cancelled_ids": set(),   # commandes retirées (ANNULATION_COMMANDE / CLIENT_ABSENT)
    "priority_overrides": {}, # id -> priorité forcée (ALERTE_TEMPERATURE)
    "vehicle_breakdown_count": 0,  # nb de camions mis hors service (PANNE_VEHICULE)
    "traffic_penalty": 1.0,   # multiplicateur appliqué à la matrice de distances
    "delivered_ids": set(),   # commandes livrées, reçues depuis le composant React
                               # (calculées côté navigateur, pas par Python)
    "sim_clock_min": 0.0,     # point de départ de l'horloge transmis au composant React,
                               # qui la fait ensuite avancer lui-même (voir dvrp_map_component)
    "simulation_started": False,  # l'horloge ne bouge pas tant que ce n'est pas True
    "initial_snapshot": None, # paramètres au démarrage (capturés une fois)
}
for key, default in DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = default


def log_event(message: str) -> None:
    st.session_state.logs.insert(0, f"[{datetime.now().strftime('%H:%M:%S')}] {message}")
    st.session_state.logs = st.session_state.logs[:30]


# ----------------------------------------------------------------------------
# 2. EN-TÊTE
# ----------------------------------------------------------------------------
st.title("🚚 Tracking en temps réel pour l'optimisation logistique : cas les tournées dynamiques DVRP")
st.caption("Alger — livraison de produits frais / express — OSRM + Google OR-Tools + MQTT")
st.markdown("---")

# ----------------------------------------------------------------------------
# 3. BARRE LATÉRALE — DONNÉES
# ----------------------------------------------------------------------------
st.sidebar.header("📁 Jeu de données")
dataset_choice = st.sidebar.selectbox(
    "Source de données",
    ["Cas réel : livraisons Grand Alger", "Benchmark synthétique (type Solomon)"],
)

if dataset_choice.startswith("Cas réel"):
    depot_coords, df_orders = get_real_algiers_dataset()
else:
    depot_coords, df_orders = generate_solomon_benchmark()

st.sidebar.header("🕹 Contrôle de la simulation")
num_vehicles = st.sidebar.slider("Camions frigorifiques disponibles", 1, 4, 2)
vehicle_capacity = st.sidebar.slider("Capacité par camion (kg)", 100, 1000, 400, step=50)
st.sidebar.caption(
    "🔁 Rotations : calculées automatiquement selon la demande totale et la capacité "
    "disponible (pas de réglage manuel nécessaire)."
)

st.sidebar.markdown("---")
st.sidebar.header("📍 Horloge de simulation & tracking des camions")
st.sidebar.caption(
    "Une seule horloge de simulation pilote à la fois l'apparition des commandes et le "
    "déplacement réel des camions (vitesse moyenne, sur le tracé réel de chaque tournée)."
)
truck_speed_kmh = st.sidebar.slider(
    "Vitesse moyenne des camions (km/h)", 15, 60, 30,
    help="Vitesse commerciale en zone urbaine (arrêts, feux, trafic inclus). "
         "Détermine le temps réel nécessaire pour parcourir chaque tournée.",
)
time_accel_options = {
    "Temps réel (1h simulée = 60 min réelles)": 60,
    "Rapide (1h simulée = 10 min réelles)": 10,
    "Très rapide (1h simulée = 5 min réelles)": 5,
    "Ultra rapide (1h simulée = 1 min réelle)": 1,
}
time_accel_label = st.sidebar.selectbox(
    "Vitesse d'accélération du temps", list(time_accel_options.keys()), index=2,
)
real_minutes_per_sim_hour = time_accel_options[time_accel_label]
sim_minutes_per_real_second = 60 / (real_minutes_per_sim_hour * 60)

if not st.session_state.simulation_started:
    if st.sidebar.button("🚀 Démarrer la simulation", type="primary"):
        st.session_state.simulation_started = True
    st.sidebar.caption("⏸️ Horloge à 0 min — cliquez pour démarrer la tournée.")
else:
    st.sidebar.success("▶️ Simulation démarrée")

col_track1, col_track2 = st.sidebar.columns(2)
manual_advance_clicked = col_track1.button("➡️ +10 min simulées") and st.session_state.simulation_started
reset_clicked = col_track2.button("🔄 Réinitialiser l'horloge")
if reset_clicked:
    st.session_state.sim_clock_min = 0.0
    st.session_state.simulation_started = False
if manual_advance_clicked:
    st.session_state.sim_clock_min = min(1440.0, st.session_state.sim_clock_min + 10.0)

auto_run = st.sidebar.toggle("▶️ Simulation temps réel (auto-refresh)", value=False)
auto_run = auto_run and st.session_state.simulation_started

sim_time = st.sidebar.slider(
    "🕐 Temps de simulation (point de départ transmis au navigateur)", 0.0, 1440.0, step=5.0,
    format="%.0f", key="sim_clock_min",
    help="Une fois transmise au composant de carte, l'horloge avance ensuite SEULE, "
         "côté navigateur (React), tant que « Simulation temps réel » est actif — "
         "aucune resynchronisation Python périodique. Ce curseur ne bouge donc que "
         "lorsque vous agissez vous-même (+10 min, reset, événement).",
)
sim_time = int(sim_time)
st.sidebar.caption(f"⏱️ Point de départ : {sim_time // 60}h{sim_time % 60:02d}")

# État initial ("avant démarrage") : capturé une seule fois (premier chargement de l'app,
# ou clic sur "🔄 Réinitialiser l'horloge"). Reste figé pendant toute la simulation, pour
# pouvoir comparer les paramètres de départ aux données finales une fois la tournée finie.
if st.session_state.initial_snapshot is None or reset_clicked:
    st.session_state.initial_snapshot = {
        "dataset": dataset_choice,
        "num_vehicles": num_vehicles,
        "vehicle_capacity": vehicle_capacity,
        "truck_speed_kmh": truck_speed_kmh,
        "time_accel": time_accel_label,
        "nb_commandes": len(df_orders),
        "demande_totale": int(df_orders["demand_kg"].sum()),
    }

with st.expander("📋 État avant démarrage (paramètres initiaux)", expanded=False):
    snap = st.session_state.initial_snapshot
    c1, c2, c3 = st.columns(3)
    c1.metric("Jeu de données", snap["dataset"].split(" :")[0])
    c1.metric("Camions disponibles", snap["num_vehicles"])
    c2.metric("Capacité / camion", f"{snap['vehicle_capacity']} kg")
    c2.metric("Vitesse moyenne", f"{snap['truck_speed_kmh']} km/h")
    c3.metric("Commandes au départ", snap["nb_commandes"])
    c3.metric("Demande totale initiale", f"{snap['demande_totale']} kg")
    st.caption(f"Accélération du temps choisie : {snap['time_accel']}")

# ----------------------------------------------------------------------------
# 4. BARRE LATÉRALE — ÉVÉNEMENTS DYNAMIQUES (désormais réellement actifs)
# ----------------------------------------------------------------------------
st.sidebar.markdown("---")
st.sidebar.header("🚨 Événements dynamiques")
st.sidebar.caption("Chaque événement modifie réellement les données et relance l'optimisation.")

manual_event_type = st.sidebar.selectbox(
    "Déclencher un événement",
    ["AUCUN", "NOUVELLE_COMMANDE", "ANNULATION_COMMANDE", "EMBOUTEILLAGE", "ROUTE_FERMEE",
     "PANNE_VEHICULE", "ALERTE_TEMPERATURE", "SORTIE_ZONE", "CLIENT_ABSENT"],
)

manual_cancel_target = None
if manual_event_type == "ANNULATION_COMMANDE":
    known_ids_now = pd.concat(
        [df_orders["id"], pd.Series([o["id"] for o in st.session_state.extra_orders], dtype=str)]
    )
    # Seules les commandes pas encore livrées (ni déjà annulées) peuvent être choisies —
    # "delivered_ids" est recalculé à chaque affichage de la carte à partir de la
    # progression réelle des camions sur leur tournée.
    cancellable_ids = [
        i for i in known_ids_now
        if i not in st.session_state.cancelled_ids and i not in st.session_state.delivered_ids
    ]
    if cancellable_ids:
        manual_cancel_target = st.sidebar.selectbox(
            "Commande à annuler (non encore livrée)", cancellable_ids,
        )
    else:
        st.sidebar.caption("Aucune commande annulable : toutes livrées ou déjà annulées.")

if st.sidebar.button("⚠️ Appliquer l'événement") and manual_event_type != "AUCUN":
    decision = process_dynamic_event(manual_event_type, {"vehicle_id": "V1"})
    known_ids = pd.concat(
        [df_orders["id"], pd.Series([o["id"] for o in st.session_state.extra_orders], dtype=str)]
    )
    candidate_ids = [i for i in known_ids if i not in st.session_state.cancelled_ids]
    detail = apply_event_effect(
        manual_event_type, st.session_state, depot_coords, sim_time, candidate_ids,
        manual_target=manual_cancel_target,
    )
    message = decision["message"] + (f" — {detail}" if detail else "")
    log_event(f"{manual_event_type} → {decision['action']} ({message})")
    st.rerun()

if st.sidebar.button("🔄 Réinitialiser les événements"):
    st.session_state.extra_orders = []
    st.session_state.cancelled_ids = set()
    st.session_state.priority_overrides = {}
    st.session_state.vehicle_breakdown_count = 0
    st.session_state.traffic_penalty = 1.0
    log_event("Réinitialisation manuelle des événements dynamiques.")
    st.rerun()

active_effects = []
if st.session_state.vehicle_breakdown_count:
    active_effects.append(f"🚧 {st.session_state.vehicle_breakdown_count} véhicule(s) en panne")
if st.session_state.traffic_penalty > 1.0:
    active_effects.append(f"🚦 trafic dégradé (x{st.session_state.traffic_penalty:.1f})")
if st.session_state.cancelled_ids:
    active_effects.append(f"❌ {len(st.session_state.cancelled_ids)} commande(s) retirée(s)")
if st.session_state.extra_orders:
    active_effects.append(f"🆕 {len(st.session_state.extra_orders)} commande(s) ajoutée(s)")
if active_effects:
    st.sidebar.caption(" · ".join(active_effects))

# ----------------------------------------------------------------------------
# 5. BARRE LATÉRALE — MQTT (télémétrie temps réel)
# ----------------------------------------------------------------------------
st.sidebar.markdown("---")
st.sidebar.header("📡 MQTT (télémétrie temps réel)")
st.sidebar.caption("Broker public de démonstration — non sécurisé, à usage pédagogique uniquement.")
mqtt_enabled = st.sidebar.checkbox("Activer la connexion MQTT", value=False)

if mqtt_enabled and st.session_state.mqtt_bridge is None:
    bridge = MQTTBridge()
    if bridge.start():
        st.session_state.mqtt_bridge = bridge
        st.sidebar.success("Connecté au broker MQTT.")
    else:
        st.sidebar.error("Connexion MQTT impossible (réseau indisponible).")
elif not mqtt_enabled and st.session_state.mqtt_bridge is not None:
    st.session_state.mqtt_bridge.stop()
    st.session_state.mqtt_bridge = None

if st.session_state.mqtt_bridge is not None:
    # On vide la queue thread-safe UNIQUEMENT ici, dans le thread principal Streamlit.
    new_events = st.session_state.mqtt_bridge.drain_events()
    st.session_state.mqtt_events.extend(new_events)
    st.session_state.mqtt_events = st.session_state.mqtt_events[-50:]

    st.sidebar.subheader("Envoyer une commande")
    target_v = st.sidebar.selectbox("Véhicule cible", [f"V{i + 1}" for i in range(num_vehicles)])
    cmd_type = st.sidebar.selectbox("Commande", ["MODIFIER_TOURNEE", "ARRETER_ALERTE", "RECALCULER_ITINERAIRE"])
    if st.sidebar.button("Envoyer la commande MQTT"):
        st.session_state.mqtt_bridge.send_command(target_v, {"command": cmd_type, "timestamp": time.time()})
        st.sidebar.info(f"Commande {cmd_type} envoyée à {target_v}")

st.sidebar.markdown("---")

# ----------------------------------------------------------------------------
# 6-9. CALCUL DVRP, CARTE & TABLEAU DE BORD — tout est délégué au composant React
#      (dvrp_map_component) une fois les tournées calculées : Python ne resynchronise
#      PAS périodiquement pendant la simulation. L'horloge, le déplacement des camions,
#      l'apparition des commandes, les KPI et le tableau vivent entièrement côté
#      navigateur. Cette fonction n'est donc appelée qu'une fois par rerun Streamlit —
#      qui n'a lieu que sur une action réelle (bouton, changement de paramètre,
#      événement, ou un retour du composant quand une commande est livrée).
# ----------------------------------------------------------------------------
def render_simulation():
    if not st.session_state.simulation_started:
        st.info(
            "🚀 Cliquez sur **« Démarrer la simulation »** dans la barre latérale pour "
            "calculer et afficher les tournées optimisées, la carte et le suivi des camions."
        )
        return

    if st.session_state.extra_orders:
        new_ids = {o["id"] for o in st.session_state.extra_orders}
        orders_df = pd.concat(
            [df_orders, pd.DataFrame(st.session_state.extra_orders, columns=COLUMNS)],
            ignore_index=True,
        )
    else:
        new_ids = set()
        orders_df = df_orders.copy()

    if st.session_state.priority_overrides:
        orders_df["priority"] = orders_df.apply(
            lambda r: st.session_state.priority_overrides.get(r["id"], r["priority"]), axis=1
        )

    # Toutes les commandes connues et non annulées sont ROUTABLES dès le départ (le
    # solveur optimise pour l'ensemble complet, comme en planification réelle) — c'est
    # le composant React qui simule ensuite leur apparition progressive sur la carte
    # selon `release_time`, sans qu'aucun recalcul serveur ne soit nécessaire au fil du
    # temps. Seuls les événements (annulation, nouvelle commande...) déclenchent un
    # vrai recalcul, puisqu'ils changent réellement le problème à résoudre.
    active_orders = orders_df[
        ~orders_df["id"].isin(st.session_state.cancelled_ids)
    ].reset_index(drop=True)

    if len(active_orders) == 0:
        st.info("Aucune commande disponible — vérifiez les événements appliqués.")
        return

    effective_vehicles = max(1, num_vehicles - st.session_state.vehicle_breakdown_count)
    if effective_vehicles < num_vehicles:
        st.warning(
            f"🚧 {st.session_state.vehicle_breakdown_count} camion(s) en panne : "
            f"{effective_vehicles}/{num_vehicles} véhicule(s) réellement disponibles."
        )

    coords_list = [depot_coords] + list(zip(active_orders["lat"], active_orders["lon"]))
    demands = [0] + active_orders["demand_kg"].astype(int).tolist()

    # Nombre de rotations par camion calculé automatiquement à partir de la demande totale
    # et de la capacité réellement disponible (plus besoin de régler un curseur manuel).
    # +1 rotation de marge : laisse au solveur une capacité légèrement excédentaire pour
    # répartir les tournées efficacement (sinon, avec le compte pile, il peut n'exister
    # aucune répartition valide même quand la capacité totale suffit tout juste).
    total_demand = int(active_orders["demand_kg"].sum())
    max_trips_per_vehicle = max(
        1, math.ceil(total_demand / (effective_vehicles * vehicle_capacity)) + 1
    )

    # Le solveur reçoit des véhicules "virtuels" (camion physique x trajets max autorisés),
    # tous de même capacité : ça lui permet de répartir une commande sur plusieurs rotations
    # d'un même camion si la capacité en un seul passage ne suffit pas.
    virtual_vehicle_count = effective_vehicles * max_trips_per_vehicle
    vehicle_capacities = [vehicle_capacity] * virtual_vehicle_count

    # ----------------------------------------------------------------------------
    # 7. CALCUL DVRP (OSRM + OR-Tools, avec capacité, rotations et pénalité de trafic)
    # ----------------------------------------------------------------------------
    with st.spinner("Calcul des distances et optimisation des tournées..."):
        raw_dist_matrix = get_osrm_distance_matrix(tuple(coords_list))  # distances réelles (km affichés)
        solver_dist_matrix = raw_dist_matrix
        if st.session_state.traffic_penalty > 1.0:
            # La pénalité de trafic influence UNIQUEMENT la décision d'OR-Tools (pour qu'il évite
            # la zone concernée) ; les distances affichées restent les vraies distances physiques.
            solver_dist_matrix = (np.array(raw_dist_matrix) * st.session_state.traffic_penalty).tolist()
        virtual_routes = solve_dvrp_ortools(solver_dist_matrix, demands, vehicle_capacities)

    total_capacity = vehicle_capacity * virtual_vehicle_count
    if not virtual_routes:
        st.error(
            f"⚠️ Aucune tournée réalisable : {total_demand} kg de commandes pour "
            f"{total_capacity} kg de capacité totale disponible ({effective_vehicles} véhicule(s) "
            f"x {max_trips_per_vehicle} rotation(s), calculées automatiquement). "
            f"Augmentez le nombre de véhicules ou leur capacité, ou réinitialisez les événements."
        )
        st.stop()

    # Regroupe les tournées virtuelles en rotations successives par camion physique.
    truck_trips = group_multi_trip_routes(virtual_routes, effective_vehicles)
    optimized_routes = [route for trips in truck_trips.values() for route in trips]  # pour les KPI globaux
    total_trips = len(optimized_routes)
    multi_trip_trucks = sum(1 for trips in truck_trips.values() if len(trips) > 1)

    # ----------------------------------------------------------------------------
    # 7bis. PREUVE DE L'OPTIMISATION : distance réelle vs référence non optimisée
    # ----------------------------------------------------------------------------
    # On mesure la distance physique (raw_dist_matrix, sans la pénalité de trafic qui ne sert
    # qu'à orienter le solveur) des tournées OR-Tools, et on la compare à une tournée « naïve »
    # qui affecte les commandes dans leur ordre d'apparition, sans aucune optimisation.
    optimized_distance_km = sum(route_distance(r, raw_dist_matrix) for r in optimized_routes) / 1000
    baseline_routes = naive_baseline_routes(demands, vehicle_capacities)
    baseline_distance_km = sum(route_distance(r, raw_dist_matrix) for r in baseline_routes) / 1000
    gain_km = baseline_distance_km - optimized_distance_km
    gain_pct = (gain_km / baseline_distance_km * 100) if baseline_distance_km > 0 else 0

    # ----------------------------------------------------------------------------
    # 8. INDICATEURS STATIQUES (ne changent pas avec le temps simulé — donc Python
    #    peut les afficher une fois pour toutes, sans resynchronisation périodique)
    # ----------------------------------------------------------------------------
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("📦 Quantité totale à livrer", f"{total_demand} kg", delta=f"capacité totale {total_capacity} kg")
    k2.metric("🚛 Camions utilisés", f"{len(truck_trips)} / {num_vehicles}")
    k3.metric("🔁 Trajets prévus", f"{total_trips}",
              delta=f"{multi_trip_trucks} en rotation multiple" if multi_trip_trucks else None)
    k4.metric("📋 Commandes au total", f"{len(active_orders)}")

    if multi_trip_trucks:
        st.info(
            f"🔁 {multi_trip_trucks} camion(s) doivent effectuer **plusieurs rotations** "
            f"(retour au dépôt puis nouveau départ) pour livrer toutes les commandes : "
            f"la capacité en un seul passage est insuffisante avec les paramètres actuels."
        )

    st.markdown("##### 📏 Preuve d'optimisation — distance réellement parcourue")
    d1, d2, d3 = st.columns(3)
    d1.metric("Distance après l'optimisation (OR-Tools)", f"{optimized_distance_km:.1f} km")
    d2.metric("Distance avant l'optimisation", f"{baseline_distance_km:.1f} km")
    d3.metric("Gain apporté par l'optimisation", f"-{gain_pct:.0f} %", delta=f"-{gain_km:.1f} km", delta_color="normal")
    st.caption(
        "La « référence » affecte les commandes aux véhicules dans leur ordre d'apparition, "
        "sans aucune optimisation de séquence ni de répartition. La différence avec la colonne "
        "de gauche mesure ce qu'OR-Tools apporte réellement (même contrainte de capacité, "
        "mêmes distances routières réelles OSRM pour les deux)."
    )

    if st.session_state.logs:
        st.info(f"Dernier événement : {st.session_state.logs[0]}")

    st.markdown("---")
    col_map, col_details = st.columns([2, 1])

    with col_map:
        st.subheader("🗺 Carte, KPI temps réel et suivi — tout est calculé côté navigateur")

        route_colors = ["blue", "green", "purple", "orange", "darkred", "cadetblue"]

        # full_shapes[p] = tracé combiné de TOUTE la rotation du camion physique p (tous
        # trajets mis bout à bout) ; trip_shapes[p] = tracés séparés par trajet (polylines).
        full_shapes: dict[int, list[list[float]]] = {}
        trip_shapes: dict[int, list[list[list[float]]]] = {}

        for p_idx, trips in truck_trips.items():
            full_shapes[p_idx] = []
            trip_shapes[p_idx] = []
            for route in trips:
                trip_shape: list[list[float]] = []
                for i in range(len(route) - 1):
                    p1 = coords_list[route[i]]
                    p2 = coords_list[route[i + 1]]
                    path = get_osrm_route_shape(tuple(p1), tuple(p2))
                    trip_shape.extend(path)
                full_shapes[p_idx].extend(trip_shape)
                trip_shapes[p_idx].append(trip_shape)

        # Un camion par index physique (0 à effective_vehicles-1), y compris ceux sans
        # tournée assignée (immobiles au dépôt) — voir historique de cette décision plus haut.
        trucks_payload = []
        for p_idx in range(effective_vehicles):
            color = route_colors[p_idx % len(route_colors)]
            shape = full_shapes.get(p_idx, [])
            if not shape:
                trucks_payload.append({"label": f"V{p_idx + 1}", "color": color, "used": False})
                continue

            # `stops` : distance cumulée (km) parcourue par CE camion jusqu'à chaque arrêt,
            # dans l'ordre de sa rotation — c'est ce qui permet au composant React de
            # déterminer, à partir de la distance parcourue qu'il calcule lui-même, quelles
            # commandes sont livrées, sans aucune aide de Python pendant la simulation.
            stops = []
            cum_km = 0.0
            for route in truck_trips[p_idx]:
                for i in range(len(route) - 1):
                    cum_km += raw_dist_matrix[route[i]][route[i + 1]] / 1000
                    node = route[i + 1]
                    if node != 0:
                        stops.append({"order_id": active_orders.iloc[node - 1]["id"], "cum_km": cum_km})

            trucks_payload.append({
                "label": f"V{p_idx + 1}", "color": color, "used": True,
                "shape": shape, "trip_shapes": trip_shapes[p_idx],
                "stops": stops, "total_km": cum_km, "speed_kmh": truck_speed_kmh,
            })

        orders_payload = active_orders[
            ["id", "client", "lat", "lon", "demand_kg", "temp_max", "time_window", "priority", "release_time"]
        ].to_dict("records")
        for o in orders_payload:
            o["is_new"] = o["id"] in new_ids

        cancelled_payload = orders_df[
            orders_df["id"].isin(st.session_state.cancelled_ids)
        ][["id", "client", "demand_kg", "priority"]].to_dict("records")

        # Vrai composant Streamlit en React (dvrp_map_component/) : reçoit TOUT en une
        # fois (itinéraires + toutes les commandes avec leur release_time) et fait vivre
        # la simulation entièrement côté navigateur — horloge, déplacement des camions,
        # apparition des commandes, KPI, tableau. Python ne resynchronise plus rien
        # pendant que ça tourne. Seul un retour ponctuel (quand une commande est livrée)
        # remonte vers Python, pour la liste des commandes annulables ci-dessous.
        result = dvrp_map(
            depot_coords, orders_payload, cancelled_payload, trucks_payload,
            sim_clock_start_min=float(sim_time),
            sim_minutes_per_real_second=sim_minutes_per_real_second,
            auto_run=auto_run,
            planned_distance_km=optimized_distance_km,
            baseline_distance_km=baseline_distance_km,
            gain_pct=gain_pct,
            num_vehicles_used=len(truck_trips),
            num_vehicles_total=num_vehicles,
            height=480,
            key="dvrp_map",
        )
        if result:
            st.session_state.delivered_ids = set(result.get("delivered_ids", []))

        st.caption(
            f"🕒 Accélération : {time_accel_label.lower()} · 🚚 Vitesse moyenne assumée : "
            f"{truck_speed_kmh} km/h — le temps de trajet de chaque camion est calculé à "
            f"partir de sa distance réelle et de cette vitesse."
        )

    with col_details:
        st.subheader("🗺️ Séquence de l'itinéraire construite")
        for p_idx, trips in truck_trips.items():
            truck_total_km = sum(route_distance(r, raw_dist_matrix) for r in trips) / 1000
            truck_total_load = sum(
                active_orders.iloc[node - 1]["demand_kg"] for r in trips for node in r if node != 0
            )
            header = f"**Camion V{p_idx + 1}**"
            if len(trips) > 1:
                header += f" — 🔁 {len(trips)} rotations — {truck_total_load} kg au total — {truck_total_km:.1f} km"
            else:
                header += f" — {truck_total_load}/{vehicle_capacity} kg — {truck_total_km:.1f} km"
            st.markdown(header)
            for t_idx, route in enumerate(trips):
                stops_names = [active_orders.iloc[node - 1]["client"] for node in route if node != 0]
                load = sum(active_orders.iloc[node - 1]["demand_kg"] for node in route if node != 0)
                trip_km = route_distance(route, raw_dist_matrix) / 1000
                prefix = f"Trajet {t_idx + 1}/{len(trips)} " if len(trips) > 1 else ""
                st.caption(
                    f"{prefix}({load}/{vehicle_capacity} kg, {trip_km:.1f} km) : "
                    + " → ".join(["Dépôt"] + stops_names + ["Dépôt"])
                )

        st.subheader("📡 Télémétrie MQTT")
        if st.session_state.mqtt_bridge is None:
            st.caption("MQTT désactivé — cochez la case dans la barre latérale pour l'activer.")
        elif st.session_state.mqtt_events:
            for event in reversed(st.session_state.mqtt_events[-5:]):
                st.json(event, expanded=False)
        else:
            st.caption("En attente de télémétrie sur `fleet/telemetry/+`...")

        st.subheader("📋 Journal des évènements")
        st.text_area("Historique", value="\n".join(st.session_state.logs[:10]), height=150, label_visibility="collapsed")


render_simulation()
