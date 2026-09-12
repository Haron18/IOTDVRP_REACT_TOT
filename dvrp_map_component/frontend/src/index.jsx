import React, { useEffect, useRef, useState } from "react";
import ReactDOM from "react-dom/client";
import { Streamlit, withStreamlitConnection } from "streamlit-component-lib";
import L from "leaflet";
import "leaflet/dist/leaflet.css";

const PRIORITY_COLOR = { URGENTE: "red", HAUTE: "orange", NORMALE: "blue" };

function haversineKm(lat1, lon1, lat2, lon2) {
  const R = 6371.0;
  const toRad = (d) => (d * Math.PI) / 180;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
}

function interpolate(shape, cumKm, targetKm) {
  if (!shape || shape.length === 0) return null;
  if (targetKm <= 0) return shape[0];
  const total = cumKm[cumKm.length - 1];
  if (targetKm >= total) return shape[shape.length - 1];
  for (let i = 1; i < cumKm.length; i++) {
    if (cumKm[i] >= targetKm) {
      const segLen = cumKm[i] - cumKm[i - 1];
      const ratio = segLen === 0 ? 0 : (targetKm - cumKm[i - 1]) / segLen;
      return [
        shape[i - 1][0] + (shape[i][0] - shape[i - 1][0]) * ratio,
        shape[i - 1][1] + (shape[i][1] - shape[i - 1][1]) * ratio,
      ];
    }
  }
  return shape[shape.length - 1];
}

function fmtMin(min) {
  const m = Math.max(0, Math.round(min));
  return Math.floor(m / 60) + "h" + String(m % 60).padStart(2, "0");
}

/**
 * Composant Streamlit (React) : carte des tournées DVRP + tableau de bord complet,
 * TOUT calculé et affiché côté navigateur (horloge, position des camions, commandes
 * révélées au fil du temps, commandes livrées, KPI, tableau). Python ne fait AUCUNE
 * resynchronisation périodique pendant que la simulation tourne — le composant reçoit
 * une fois toutes les données nécessaires (itinéraires + toutes les commandes avec leur
 * `release_time`) et fait vivre la simulation lui-même.
 *
 * Un seul retour vers Python (`Streamlit.setComponentValue`), et seulement quand
 * l'ensemble des commandes livrées change réellement — sert à alimenter la liste des
 * commandes annulables côté Python (barre latérale). Ce n'est pas du polling : c'est un
 * événement, déclenché uniquement quand quelque chose se produit réellement.
 */
function DvrpMap({ args }) {
  const {
    depot,
    orders = [],
    cancelled_orders: cancelledOrders = [],
    trucks = [],
    height = 520,
    sim_clock_start_min: simClockStartMin = 0,
    sim_minutes_per_real_second: simMinutesPerRealSecond = 0,
    auto_run: autoRun = false,
    planned_distance_km: plannedDistanceKm = 0,
    baseline_distance_km: baselineDistanceKm = 0,
    gain_pct: gainPct = 0,
    num_vehicles_used: numVehiclesUsed = 0,
    num_vehicles_total: numVehiclesTotal = 0,
  } = args;

  const wrapperRef = useRef(null);
  const mapContainerRef = useRef(null);
  const mapRef = useRef(null);
  const truckStateRef = useRef({});
  const orderMarkersRef = useRef({});
  const lastReportedRef = useRef(null);

  const [kpi, setKpi] = useState({
    simClockMin: simClockStartMin,
    visibleCount: 0,
    totalOrders: orders.length,
    deliveredCount: 0,
    distanceParcourue: 0,
    allFinished: false,
  });

  const structuralKey = JSON.stringify({
    depot,
    orders: orders.map((o) => o.id),
    trucks: trucks.map((t) => ({ label: t.label, used: t.used, shape: t.shape, trip_shapes: t.trip_shapes })),
    simClockStartMin,
    autoRun,
  });

  useEffect(() => {
    if (mapRef.current) {
      mapRef.current.remove();
      mapRef.current = null;
    }
    if (!mapContainerRef.current) return undefined;

    const map = L.map(mapContainerRef.current).setView(depot, 11);
    mapRef.current = map;
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: "&copy; OpenStreetMap contributors",
      maxZoom: 19,
    }).addTo(map);

    L.marker(depot, {
      icon: L.divIcon({ className: "depot-icon", html: "🏠", iconSize: [24, 24] }),
    })
      .addTo(map)
      .bindPopup("<b>Dépôt Central — Oued Smar</b>");

    const orderMarkers = {};
    orders.forEach((o) => {
      const color = PRIORITY_COLOR[o.priority] || "blue";
      const marker = L.circleMarker([o.lat, o.lon], {
        radius: 8, color, fillColor: color, fillOpacity: 0.85, weight: 2,
      }).bindPopup(
        "<b>" + o.client + "</b><br>Charge: " + o.demand_kg + " kg<br>" +
          "Temp. max: " + o.temp_max + "°C<br>Fenêtre: " + o.time_window
      ).bindTooltip(o.id);
      orderMarkers[o.id] = { marker, shown: false };
    });
    orderMarkersRef.current = orderMarkers;

    const truckState = {};
    let usedCount = 0;
    trucks.forEach((t) => {
      const shape = t.shape || [];
      const cumKm = [0];
      for (let i = 1; i < shape.length; i++) {
        cumKm.push(
          cumKm[cumKm.length - 1] +
            haversineKm(shape[i - 1][0], shape[i - 1][1], shape[i][0], shape[i][1])
        );
      }
      if (t.used && shape.length && t.trip_shapes) {
        t.trip_shapes.forEach((path) => {
          L.polyline(path, { color: t.color, weight: 4, opacity: 0.85 }).addTo(map);
        });
      }
      const startPos = t.used && shape.length ? interpolate(shape, cumKm, 0) : depot;
      const marker = L.marker(startPos, {
        icon: L.divIcon({ className: "truck-icon", html: "🚚", iconSize: [24, 24] }),
      })
        .addTo(map)
        .bindTooltip(t.label);
      truckState[t.label] = {
        marker, shape, cumKm, used: t.used,
        stops: t.stops || [], totalKm: t.total_km || 0, speedKmh: t.speed_kmh || 0,
      };
      if (t.used) usedCount += 1;
    });
    truckStateRef.current = truckState;

    const startTime = performance.now();
    let frameId;
    let lastUiUpdate = 0;

    function animate(now) {
      const elapsedSec = (now - startTime) / 1000;
      const simClockMin = autoRun
        ? simClockStartMin + elapsedSec * simMinutesPerRealSecond
        : simClockStartMin;

      orders.forEach((o) => {
        const entry = orderMarkersRef.current[o.id];
        if (!entry) return;
        const shouldShow = o.release_time <= simClockMin;
        if (shouldShow && !entry.shown) {
          entry.marker.addTo(map);
          entry.shown = true;
        } else if (!shouldShow && entry.shown) {
          entry.marker.remove();
          entry.shown = false;
        }
      });

      const deliveredSet = new Set();
      let distanceParcourue = 0;
      let allUsedFinished = usedCount > 0;
      Object.values(truckStateRef.current).forEach((s) => {
        if (!s.used) return;
        const traveledKm = Math.min(s.totalKm, (s.speedKmh * (simClockMin - simClockStartMin)) / 60);
        distanceParcourue += traveledKm;
        if (traveledKm < s.totalKm) allUsedFinished = false;
        const pos = interpolate(s.shape, s.cumKm, traveledKm);
        if (pos) s.marker.setLatLng(pos);
        s.stops.forEach((stop) => {
          if (traveledKm >= stop.cum_km) deliveredSet.add(stop.order_id);
        });
      });

      if (now - lastUiUpdate > 250) {
        lastUiUpdate = now;
        const visibleCount = orders.filter((o) => o.release_time <= simClockMin).length;
        setKpi({
          simClockMin, visibleCount, totalOrders: orders.length,
          deliveredCount: deliveredSet.size, distanceParcourue, allFinished: allUsedFinished,
        });
        const reportKey = JSON.stringify(Array.from(deliveredSet).sort());
        if (reportKey !== lastReportedRef.current) {
          lastReportedRef.current = reportKey;
          Streamlit.setComponentValue({ delivered_ids: Array.from(deliveredSet), all_finished: allUsedFinished });
        }
      }
      frameId = requestAnimationFrame(animate);
    }
    frameId = requestAnimationFrame(animate);

    return () => {
      cancelAnimationFrame(frameId);
      if (mapRef.current) {
        mapRef.current.remove();
        mapRef.current = null;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [structuralKey]);

  useEffect(() => {
    if (wrapperRef.current) {
      Streamlit.setFrameHeight(wrapperRef.current.scrollHeight + 20);
    }
  });

  const tableRows = orders
    .map((o) => ({
      id: o.id, client: o.client, demand_kg: o.demand_kg, priority: o.priority,
      status: o.is_new ? "🆕 Nouvelle" : "✅ Normale",
      visible: o.release_time <= kpi.simClockMin,
    }))
    .concat(
      cancelledOrders.map((o) => ({
        id: o.id, client: o.client, demand_kg: o.demand_kg, priority: o.priority,
        status: "❌ Retirée", visible: true,
      }))
    );

  return (
    <div ref={wrapperRef} style={{ fontFamily: "sans-serif" }}>
      <style>{
        ".truck-icon { font-size: 22px; line-height: 22px; text-align: center;" +
        "  filter: drop-shadow(0 0 2px rgba(0,0,0,.6)); }" +
        ".depot-icon { font-size: 22px; line-height: 22px; text-align: center; }" +
        ".dvrp-kpis { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }" +
        ".dvrp-kpi-card { flex: 1; min-width: 130px; background: #f0f2f6; border-radius: 8px; padding: 8px 12px; }" +
        ".dvrp-kpi-label { font-size: 12px; color: #555; }" +
        ".dvrp-kpi-value { font-size: 20px; font-weight: 600; }" +
        ".dvrp-final { margin-top: 10px; background: #e8f5e9; border-radius: 8px; padding: 10px 14px; }" +
        ".dvrp-table-wrap { margin-top: 10px; max-height: 240px; overflow-y: auto; border: 1px solid #eee; border-radius: 8px; }" +
        ".dvrp-table { width: 100%; border-collapse: collapse; font-size: 13px; }" +
        ".dvrp-table th, .dvrp-table td { text-align: left; padding: 4px 8px; border-bottom: 1px solid #f0f0f0; }" +
        ".dvrp-table th { position: sticky; top: 0; background: #fafafa; }"
      }</style>

      <div ref={mapContainerRef} style={{ width: "100%", height: height + "px", borderRadius: 8 }} />

      <div className="dvrp-kpis">
        <div className="dvrp-kpi-card">
          <div className="dvrp-kpi-label">⏱️ Temps de simulation</div>
          <div className="dvrp-kpi-value">{fmtMin(kpi.simClockMin)}</div>
        </div>
        <div className="dvrp-kpi-card">
          <div className="dvrp-kpi-label">📋 Commandes visibles</div>
          <div className="dvrp-kpi-value">{kpi.visibleCount} / {kpi.totalOrders}</div>
        </div>
        <div className="dvrp-kpi-card">
          <div className="dvrp-kpi-label">✅ Commandes livrées</div>
          <div className="dvrp-kpi-value">{kpi.deliveredCount}</div>
        </div>
        <div className="dvrp-kpi-card">
          <div className="dvrp-kpi-label">📏 Distance parcourue</div>
          <div className="dvrp-kpi-value">{kpi.distanceParcourue.toFixed(1)} km</div>
        </div>
        <div className="dvrp-kpi-card">
          <div className="dvrp-kpi-label">🚛 Camions en tournée</div>
          <div className="dvrp-kpi-value">{numVehiclesUsed} / {numVehiclesTotal}</div>
        </div>
      </div>

      {kpi.allFinished ? (
        <div className="dvrp-final">
          <b>📊 Données finales — tournée terminée</b><br />
          Distance planifiée : {plannedDistanceKm.toFixed(1)} km (vs {baselineDistanceKm.toFixed(1)} km sans
          optimisation, soit -{gainPct.toFixed(0)} %) · Commandes livrées : {kpi.deliveredCount} / {kpi.totalOrders}
          {" "}· Temps simulé écoulé : {fmtMin(kpi.simClockMin)}
        </div>
      ) : (
        <div className="dvrp-final" style={{ background: "#fff8e1" }}>
          ⏳ Simulation en cours — {kpi.deliveredCount} commande(s) livrée(s) sur {kpi.totalOrders}.
        </div>
      )}

      <div className="dvrp-table-wrap">
        <table className="dvrp-table">
          <thead>
            <tr><th>ID</th><th>Client</th><th>Kg</th><th>Priorité</th><th>État</th><th>Visible</th></tr>
          </thead>
          <tbody>
            {tableRows.map((r) => (
              <tr key={r.id}>
                <td>{r.id}</td><td>{r.client}</td><td>{r.demand_kg}</td><td>{r.priority}</td>
                <td>{r.status}</td><td>{r.visible ? "Oui" : "À venir"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

const ConnectedDvrpMap = withStreamlitConnection(DvrpMap);
const root = ReactDOM.createRoot(document.getElementById("root"));
root.render(<ConnectedDvrpMap />);
