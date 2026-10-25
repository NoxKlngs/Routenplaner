import streamlit as st
import pandas as pd
import openrouteservice as ors
from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp
import time
import folium
from streamlit_folium import st_folium

st.set_page_config(page_title="Routenplaner", layout="wide")

# --- PASSWORTSCHUTZ ---
if "authentifiziert" not in st.session_state:
    st.session_state.authentifiziert = False

if not st.session_state.authentifiziert:
    st.warning("🔒 Diese App ist geschützt.")
    passwort = st.text_input("Passwort eingeben", type="password")
    if st.button("Einloggen"):
        if passwort == "MeinFreundIstDerBeste":  # Hier das gewünschte Passwort eintragen
            st.session_state.authentifiziert = True
            st.rerun()
        else:
            st.error("Falsches Passwort!")
    st.stop() # Verhindert, dass der restliche Code geladen wird
# ----------------------

# --- CACHING FUNKTIONEN (Schont das API-Limit) ---
@st.cache_data(show_spinner=False)
def get_koordinaten(api_key, adresse):
    client = ors.Client(key=api_key)
    try:
        res = client.pelias_search(text=adresse)
        if res and res['features']:
            return res['features'][0]['geometry']['coordinates']
    except Exception:
        pass
    return None

@st.cache_data(show_spinner=False)
def get_distanzmatrix(api_key, koordinaten_tuple):
    client = ors.Client(key=api_key)
    matrix_res = client.distance_matrix(
        locations=list(koordinaten_tuple),
        profile='driving-car',
        metrics=['duration']
    )
    return [[int(val) for val in row] for row in matrix_res['durations']]

@st.cache_data(show_spinner=False)
def get_route_geometry(api_key, coords_tuple):
    client = ors.Client(key=api_key)
    return client.directions(
        coordinates=list(coords_tuple),
        profile='driving-car',
        format='geojson'
    )
# ------------------------------------------------

st.title("🚚 Automatischer Routenplaner")

# --- SEITENLEISTE FÜR EINSTELLUNGEN ---
st.sidebar.header("Einstellungen")
api_key = st.sidebar.text_input("OpenRouteService API-Key (hier einfügen)", type="password")
num_vehicles = st.sidebar.number_input("Anzahl der Fahrzeuge", min_value=1, max_value=10, value=2)
depot_index = 0 

# --- DATENEINGABE ---
st.write("Ziehe eine Datei per Drag & Drop hier rein oder tippe die Adressen direkt in die leere Tabelle.")
uploaded_file = st.file_uploader("CSV-Datei hochladen", type=["csv"])

if uploaded_file is not None:
    start_daten = pd.read_csv(uploaded_file, sep=";")
else:
    start_daten = pd.DataFrame({
        "Name": ["Depot", "", ""],
        "Adresse": ["", "", ""]
    })

kundendaten = st.data_editor(start_daten, num_rows="dynamic", use_container_width=True)

# --- GEDÄCHTNIS FÜR DEN BUTTON ---
if "berechnen_geklickt" not in st.session_state:
    st.session_state.berechnen_geklickt = False

if st.button("Routen berechnen", type="primary"):
    st.session_state.berechnen_geklickt = True

# Hier prüfen wir das Gedächtnis, nicht nur den Button-Klick
if st.session_state.berechnen_geklickt:
    if not api_key:
        st.error("Bitte gib links in der Seitenleiste deinen OpenRouteService API-Key ein.")
        st.stop()
        
    adressen_liste = kundendaten["Adresse"].dropna().tolist()
    namen_liste = kundendaten["Name"].dropna().tolist()
    
    if len(adressen_liste) < 3:
        st.error("Bitte gib mindestens das Depot und zwei Kunden ein.")
        st.stop()

    try:
        koordinaten = []
        
        st.info("Wandle Adressen in Koordinaten um...")
        for adresse in adressen_liste:
            coords = get_koordinaten(api_key, adresse)
            if coords:
                koordinaten.append(coords)
            else:
                st.error(f"Adresse nicht gefunden: {adresse}")
                st.stop()
            time.sleep(0.2)

        st.info("Berechne Fahrzeiten zwischen allen Punkten...")
        koordinaten_tuple = tuple(tuple(c) for c in koordinaten)
        duration_matrix = get_distanzmatrix(api_key, koordinaten_tuple)
        
        st.info("Optimiere Routen...")
        manager = pywrapcp.RoutingIndexManager(len(duration_matrix), num_vehicles, depot_index)
        routing = pywrapcp.RoutingModel(manager)

        def distance_callback(from_index, to_index):
            from_node = manager.IndexToNode(from_index)
            to_node = manager.IndexToNode(to_index)
            return duration_matrix[from_node][to_node]

        transit_callback_index = routing.RegisterTransitCallback(distance_callback)
        routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)

        def demand_callback(from_index):
            from_node = manager.IndexToNode(from_index)
            return 0 if from_node == depot_index else 1
        
        demand_callback_index = routing.RegisterUnaryTransitCallback(demand_callback)
        max_stops = max(1, (len(adressen_liste) - 1) // num_vehicles + 1)
        routing.AddDimension(demand_callback_index, 0, max_stops, True, 'Capacity')

        search_parameters = pywrapcp.DefaultRoutingSearchParameters()
        search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
        search_parameters.time_limit.seconds = 3

        solution = routing.SolveWithParameters(search_parameters)

        if solution:
            st.success("Routen erfolgreich berechnet!")
            
            depot_lat, depot_lon = koordinaten[depot_index][1], koordinaten[depot_index][0]
            colors = ['blue', 'red', 'green', 'purple', 'orange', 'darkred', 'lightred', 'beige', 'darkblue', 'darkgreen']
            
            st.subheader("📝 Routen-Details pro Fahrzeug")
            
            for vehicle_id in range(num_vehicles):
                st.markdown(f"### 🚚 Fahrzeug {vehicle_id + 1}")
                col1, col2 = st.columns([1, 1])
                
                index = routing.Start(vehicle_id)
                route_text = ""
                route_time = 0
                route_indices = []
                
                while not routing.IsEnd(index):
                    node_index = manager.IndexToNode(index)
                    route_indices.append(node_index)
                    route_text += f"- {namen_liste[node_index]}\n"
                    previous_index = index
                    index = solution.Value(routing.NextVar(index))
                    route_time += routing.GetArcCostForVehicle(previous_index, index, vehicle_id)
                
                node_index = manager.IndexToNode(index)
                route_indices.append(node_index)
                route_text += f"- Zurück zum {namen_liste[node_index]}\n\n"
                route_text += f"*Fahrzeit: {route_time // 60} Min.*"
                
                with col1:
                    st.markdown(route_text)
                    
                with col2:
                    m = folium.Map(location=[depot_lat, depot_lon], zoom_start=11)
                    
                    if len(route_indices) > 1:
                        route_coords = [koordinaten[i] for i in route_indices]
                        color = colors[vehicle_id % len(colors)]
                        
                        try:
                            coords_tuple = tuple(tuple(c) for c in route_coords)
                            route_geom = get_route_geometry(api_key, coords_tuple)
                            
                            folium.GeoJson(
                                route_geom,
                                style_function=lambda x, c=color: {'color': c, 'weight': 4}
                            ).add_to(m)
                            
                            for i, node_idx in enumerate(route_indices[:-1]):
                                lat, lon = koordinaten[node_idx][1], koordinaten[node_idx][0]
                                if node_idx == depot_index:
                                    folium.Marker([lat, lon], popup="Depot", icon=folium.Icon(color='black', icon='home')).add_to(m)
                                else:
                                    folium.Marker([lat, lon], popup=namen_liste[node_idx], icon=folium.Icon(color=color, icon='info-sign')).add_to(m)
                        
                        except Exception:
                            st.warning(f"Konnte Karte für Fahrzeug {vehicle_id + 1} nicht zeichnen.")
                            
                    st_folium(m, width=500, height=400, returned_objects=[], key=f"map_{vehicle_id}")
                
                st.divider()
                
        else:
            st.error("Es konnte keine Lösung gefunden werden.")
            
    except Exception as e:
        st.error(f"Es ist ein Fehler aufgetreten: {e}")