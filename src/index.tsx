import {
  ButtonItem,
  DialogButton,
  Dropdown,
  Field,
  Focusable,
  ModalRoot,
  PanelSection,
  PanelSectionRow,
  SliderField,
  TextField,
  ToggleField,
  showModal,
  staticClasses,
} from "@decky/ui";
import { callable, definePlugin, fetchNoCors, toaster } from "@decky/api";
import { useEffect, useState, type CSSProperties } from "react";
import { FaSkullCrossbones } from "react-icons/fa";

// --------------------------------------------------------------------------
// Types & backend calls
// --------------------------------------------------------------------------

interface Settings {
  overlay_enabled: boolean;
  position: string;
  only_in_game: boolean;
  hide_without_storm: boolean;
  sort_by_time: boolean;
  show_under_minutes: number;
  rain_scale: number;
  offset_seconds: number;
  islands: string[];
  overlay_install_prompted: boolean;
}

interface IslandStatus {
  id: string;
  name: string;
  index: number;
  state: "now" | "soon" | "none" | "unknown";
  at?: number;
  until?: number | null;
}

interface Island {
  id: string;
  name: string;
}

interface State {
  now: number;
  settings: Settings;
  statuses: IslandStatus[];
  forecast_at: number;
  forecast_until: number;
  forecast_error: string;
  islands_count: number;
  islands_error: string;
  game_running: boolean;
  overlay_status: "connected" | "not_running" | "not_installed";
  overlay_error: string;
}

const getState = callable<[], State>("get_state");
const getStartupState = callable<[], State>("get_startup_state");
const getVersion = callable<[], string>("get_version");
const getIslands = callable<[], Island[]>("get_islands");
const refreshForecast = callable<[], State>("refresh_forecast");
const addIsland = callable<[id: string], State>("add_island");
const removeIsland = callable<[id: string], State>("remove_island");
const moveIsland = callable<[id: string, delta: number], State>("move_island");
const saveSettings = callable<[settings: Partial<Settings>], State>("save_settings");
const previewOverlay = callable<[], State>("preview_overlay");
const overlayInstallPrompted = callable<[], State>("overlay_install_prompted");
const calibrate = callable<[id: string], State & { calibrated: number | null }>("calibrate");

// Must match MAX_ISLANDS in main.py (one overlay line per island).
const MAX_ISLANDS = 12;

// The backend clock is the reference (the overlay uses it too).
let clockOffset = 0;
const syncClock = (s: State) => {
  clockOffset = s.now - Date.now() / 1000;
  return s;
};
const backendNow = () => Date.now() / 1000 + clockOffset;

function useTick(ms = 1000) {
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick((n) => n + 1), ms);
    return () => clearInterval(id);
  }, [ms]);
}

const pad = (n: number) => String(n).padStart(2, "0");

function formatCountdown(seconds: number): string {
  const total = Math.max(0, Math.ceil(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

// "+9 min 30 s", "-2 min", "aucun"
function formatOffset(seconds: number): string {
  if (!seconds) return "aucun";
  const sign = seconds > 0 ? "+" : "-";
  const abs = Math.abs(Math.round(seconds));
  const m = Math.floor(abs / 60);
  const s = abs % 60;
  return `${sign}${m ? `${m} min` : ""}${m && s ? " " : ""}${s ? `${s} s` : ""}`;
}

function formatClock(unix: number): string {
  const d = new Date(unix * 1000);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// Only this small component re-renders every second.
function LiveStatus({ s }: { s: IslandStatus }) {
  useTick();
  const style: CSSProperties = { fontFamily: "monospace", fontWeight: "bold", marginLeft: 8, whiteSpace: "nowrap" };
  if (s.state === "now") {
    const left = s.until ? s.until - backendNow() : 0;
    return (
      <span style={{ ...style, color: "#ff5c5c" }}>
        Pluie{left > 0 && <span style={{ fontWeight: "normal" }}> · fin {formatCountdown(left)}</span>}
      </span>
    );
  }
  if (s.state === "soon" && s.at) {
    const left = s.at - backendNow();
    if (left <= 0) return <span style={{ ...style, color: "#ff5c5c" }}>Pluie</span>;
    return (
      <span style={{ ...style, color: left < 600 ? "#ffc84d" : "#fff" }}>
        {formatCountdown(left)} <span style={{ color: "#999", fontWeight: "normal" }}>({formatClock(s.at)})</span>
      </span>
    );
  }
  if (s.state === "none") return <span style={{ ...style, color: "#999", fontWeight: "normal" }}>Pas de tempête</span>;
  return <span style={{ ...style, color: "#ffc84d", fontWeight: "normal" }}>Inconnu</span>;
}

// --------------------------------------------------------------------------
// Decky Overlay (shared in-game overlay, powange/decky-overlay)
// --------------------------------------------------------------------------

const OVERLAY_REPO = "powange/decky-overlay";
const OVERLAY_ASSET = "decky-overlay.zip";
const OVERLAY_PLUGIN_NAME = "Decky Overlay"; // "name" in its plugin.json
const OVERLAY_MANUAL_URL = `https://github.com/${OVERLAY_REPO}/releases/latest/download/${OVERLAY_ASSET}`;
const INSTALL_TYPE_INSTALL = 0; // Decky's PluginInstallType.INSTALL

// Decky has no plugin dependencies: Decky SoT asks Decky to install the
// overlay plugin itself (Decky shows its usual confirmation). Returns an
// error message, or "" when the install dialog was opened.
async function installDeckyOverlay(): Promise<string> {
  const backend = (window as any).DeckyBackend;
  if (typeof backend?.call !== "function") {
    return "Installation automatique indisponible avec cette version de Decky.";
  }
  try {
    let version = "latest";
    try {
      const res = await fetchNoCors(`https://api.github.com/repos/${OVERLAY_REPO}/releases/latest`, {
        headers: { Accept: "application/vnd.github+json" },
      });
      if (res.ok) version = String((await res.json()).tag_name ?? "latest").replace(/^v/, "");
    } catch {
      // keep "latest": the download URL below always points to the newest release
    }
    await backend.call("utilities/install_plugin", OVERLAY_MANUAL_URL, OVERLAY_PLUGIN_NAME, version, "",
      INSTALL_TYPE_INSTALL);
    return "";
  } catch (e: any) {
    return `Échec de l'installation : ${e?.message ?? e}`;
  }
}

function OverlayStatus({ state, onState }: { state: State; onState: (s: State) => void }) {
  const [message, setMessage] = useState("");
  const status = state.overlay_status;
  return (
    <>
      <PanelSectionRow>
        <div style={{ fontSize: 12, lineHeight: 1.4 }}>
          {status === "connected" && (
            <span style={{ color: "#7c7" }}>Affiché via Decky Overlay (taille et opacité se règlent dans ce plugin).</span>
          )}
          {status === "not_running" && (
            <span style={{ color: "#ffc84d" }}>Decky Overlay est installé mais pas encore actif. Il se connectera tout seul.</span>
          )}
          {status === "not_installed" && (
            <span style={{ color: "#ff8080" }}>
              L'affichage en jeu passe par le plugin Decky Overlay, partagé avec d'autres plugins. Il n'est pas installé.
            </span>
          )}
          {state.overlay_error && <div style={{ color: "#ff8080" }}>{state.overlay_error}</div>}
          {message && (
            <div style={{ color: "#ff8080", wordBreak: "break-all" }}>
              {message} Installe-le depuis Decky › Développeur › Installer depuis une URL :
              <div style={{ fontFamily: "monospace", color: "#ddd" }}>{OVERLAY_MANUAL_URL}</div>
            </div>
          )}
        </div>
      </PanelSectionRow>
      {status === "not_installed" && (
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={async () => {
              setMessage(await installDeckyOverlay());
              onState(syncClock(await overlayInstallPrompted()));
            }}
          >
            Installer Decky Overlay
          </ButtonItem>
        </PanelSectionRow>
      )}
    </>
  );
}

// --------------------------------------------------------------------------
// Island picker
// --------------------------------------------------------------------------

// Accents, case and punctuation ignored: "melee" finds "Mêlée Island".
const normalize = (s: string) =>
  s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();

function IslandPickerModal({
  chosen,
  closeModal,
  onState,
}: {
  chosen: string[];
  closeModal?: () => void;
  onState: (s: State) => void;
}) {
  const [islands, setIslands] = useState<Island[] | null>(null);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string[]>(chosen);

  useEffect(() => {
    getIslands()
      .then((list) => {
        setIslands(list);
        if (list.length === 0) setError("Liste des îles indisponible (pas de connexion ?).");
      })
      .catch((e) => setError(`Liste des îles indisponible : ${e?.message ?? e}`));
  }, []);

  const q = normalize(query);
  const shown = (islands ?? []).filter((i) => !q || normalize(i.name).includes(q));
  const full = selected.length >= MAX_ISLANDS;

  const toggle = async (island: Island) => {
    const isChosen = selected.includes(island.id);
    if (!isChosen && full) return;
    const s = syncClock(await (isChosen ? removeIsland(island.id) : addIsland(island.id)));
    setSelected(s.settings.islands);
    onState(s);
  };

  return (
    <ModalRoot onCancel={closeModal}>
      <div style={{ fontSize: 18, fontWeight: "bold", marginBottom: 4 }}>Îles à surveiller</div>
      <div style={{ fontSize: 12, color: "#999", marginBottom: 8 }}>
        {selected.length}/{MAX_ISLANDS} îles choisies · noms anglais, comme dans le jeu en anglais
      </div>
      <TextField label="Rechercher" value={query} onChange={(e) => setQuery(e.target.value)} />
      {error && <div style={{ color: "#ff8080", fontSize: 12, marginTop: 8 }}>{error}</div>}
      {!islands && !error && <div style={{ marginTop: 8 }}>Chargement…</div>}
      <Focusable style={{ display: "flex", flexDirection: "column", gap: 4, marginTop: 8, maxHeight: 340, overflowY: "auto" }}>
        {shown.map((island) => {
          const isChosen = selected.includes(island.id);
          return (
            <DialogButton
              key={island.id}
              disabled={!isChosen && full}
              style={{ textAlign: "left", padding: "6px 12px" }}
              onClick={() => toggle(island)}
            >
              {isChosen ? "✔ " : "+ "}
              {island.name}
            </DialogButton>
          );
        })}
        {islands && shown.length === 0 && !error && <div style={{ color: "#999" }}>Aucune île ne correspond.</div>}
      </Focusable>
      <Focusable style={{ marginTop: 12 }}>
        <DialogButton onClick={closeModal}>Terminé</DialogButton>
      </Focusable>
    </ModalRoot>
  );
}

// --------------------------------------------------------------------------
// Updates (same mechanism as Decky Timer)
// --------------------------------------------------------------------------

// Releases are published by .github/workflows/release.yml.
const UPDATE_REPO = "powange/decky-sot";
const UPDATE_ASSET = "decky-sot.zip";
const MANUAL_INSTALL_URL = `https://github.com/${UPDATE_REPO}/releases/latest/download/${UPDATE_ASSET}`;
// Must match "name" in plugin.json so Decky replaces the installed plugin.
const PLUGIN_NAME = "Decky SoT";
const INSTALL_TYPE_UPDATE = 2; // Decky's PluginInstallType.UPDATE

let updateCheckedThisSession = false;

// Compares "v1.2.3" / "1.2.3" numerically: >0 when a is newer than b.
function compareVersions(a: string, b: string): number {
  const parse = (v: string) => v.replace(/^v/, "").split(/[.-]/).map((x) => parseInt(x, 10) || 0);
  const pa = parse(a);
  const pb = parse(b);
  for (let i = 0; i < Math.max(pa.length, pb.length, 3); i++) {
    const d = (pa[i] ?? 0) - (pb[i] ?? 0);
    if (d !== 0) return d;
  }
  return 0;
}

function UpdateSection() {
  const [current, setCurrent] = useState<string>("");
  const [latest, setLatest] = useState<{ tag: string; url: string } | null>(null);
  const [status, setStatus] = useState<string>("");
  const [busy, setBusy] = useState<boolean>(false);
  const [manualInstall, setManualInstall] = useState<boolean>(false);

  const check = async (version: string, silent = false) => {
    setBusy(true);
    if (!silent) setStatus("Vérification sur GitHub…");
    setLatest(null);
    try {
      const res = await fetchNoCors(`https://api.github.com/repos/${UPDATE_REPO}/releases/latest`, {
        headers: { Accept: "application/vnd.github+json" },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const rel = await res.json();
      const asset = (rel.assets || []).find((a: any) => a.name === UPDATE_ASSET);
      if (!asset) throw new Error(`pas de ${UPDATE_ASSET} dans la dernière release`);
      if (compareVersions(rel.tag_name, version) <= 0) {
        setStatus(`À jour (${rel.tag_name})`);
      } else {
        setLatest({ tag: rel.tag_name, url: asset.browser_download_url });
        setStatus(`Mise à jour disponible : ${rel.tag_name}`);
      }
    } catch (e: any) {
      if (!silent) setStatus(`Échec de la vérification : ${e?.message ?? e}`);
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    getVersion()
      .then((v) => {
        setCurrent(v);
        // One silent check per plugin load, so an update shows up by itself.
        if (!updateCheckedThisSession) {
          updateCheckedThisSession = true;
          check(v, true);
        }
      })
      .catch(() => setCurrent("?"));
  }, []);

  const install = async () => {
    if (!latest) return;
    // Internal Decky API (undocumented): fall back to the manual URL if it breaks.
    const backend = (window as any).DeckyBackend;
    if (typeof backend?.call !== "function") {
      setManualInstall(true);
      setStatus("Mise à jour automatique indisponible avec cette version de Decky.");
      return;
    }
    try {
      setBusy(true);
      await backend.call("utilities/install_plugin", latest.url, PLUGIN_NAME, latest.tag.replace(/^v/, ""), "",
        INSTALL_TYPE_UPDATE);
    } catch (e: any) {
      setManualInstall(true);
      setStatus(`Échec de l'installation : ${e?.message ?? e}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <PanelSection title="Mises à jour">
      <PanelSectionRow>
        <div style={{ fontSize: 12, color: "#bbb" }}>
          Installée : <b>{current || "…"}</b>
          {status && <div>{status}</div>}
          {manualInstall && (
            <div style={{ marginTop: 4, wordBreak: "break-all" }}>
              Installe-la depuis Decky › Développeur › Installer depuis une URL :
              <div style={{ fontFamily: "monospace", color: "#ddd" }}>{MANUAL_INSTALL_URL}</div>
            </div>
          )}
        </div>
      </PanelSectionRow>
      <PanelSectionRow>
        {latest ? (
          <ButtonItem layout="below" onClick={install}>
            Mettre à jour vers {latest.tag}
          </ButtonItem>
        ) : (
          <ButtonItem layout="below" onClick={() => check(current)} disabled={busy}>
            Rechercher une mise à jour
          </ButtonItem>
        )}
      </PanelSectionRow>
    </PanelSection>
  );
}

// --------------------------------------------------------------------------
// Main panel
// --------------------------------------------------------------------------

const positionOptions = [
  { data: "top-left", label: "En haut à gauche" },
  { data: "top-center", label: "En haut au centre" },
  { data: "top-right", label: "En haut à droite" },
  { data: "bottom-left", label: "En bas à gauche" },
  { data: "bottom-right", label: "En bas à droite" },
];

const showUnderOptions = [
  { data: 0, label: "Toujours" },
  ...[5, 10, 15, 30, 60].map((m) => ({ data: m, label: `Dans les ${m} dernières min` })),
];

let pendingSettings: Partial<Settings> = {};
let settingsTimer: ReturnType<typeof setTimeout> | null = null;
function queueSettingsSave(patch: Partial<Settings>, onSaved: (s: State) => void) {
  pendingSettings = { ...pendingSettings, ...patch };
  if (settingsTimer) clearTimeout(settingsTimer);
  settingsTimer = setTimeout(async () => {
    const toSave = pendingSettings;
    pendingSettings = {};
    settingsTimer = null;
    try {
      onSaved(await saveSettings(toSave));
    } catch (e) {
      console.error("Decky SoT: settings not saved", e);
    }
  }, 300);
}

function CalibrationSection({
  state,
  apply,
  updateSettings,
}: {
  state: State;
  apply: (s: State) => void;
  updateSettings: (patch: Partial<Settings>) => void;
}) {
  const { settings, statuses } = state;
  const [message, setMessage] = useState("");
  const ordered = [...statuses].sort((a, b) => a.index - b.index);

  return (
    <PanelSection title="Calage">
      <PanelSectionRow>
        <div style={{ fontSize: 12, color: "#999", lineHeight: 1.4 }}>
          Quand la pluie commence sur une de tes îles en jeu, appuie sur son bouton : le décalage entre le site et le
          jeu se règle tout seul.
        </div>
      </PanelSectionRow>
      {ordered.map((s) => (
        <PanelSectionRow key={s.id}>
          <ButtonItem
            layout="below"
            onClick={async () => {
              const res = await calibrate(s.id);
              apply(res);
              setMessage(
                res.calibrated === null
                  ? `Aucune pluie prévue sur ${s.name} à moins d'une heure d'ici : décalage inchangé.`
                  : `Décalage réglé à ${formatOffset(res.calibrated)}.`
              );
            }}
          >
            La pluie commence sur {s.name}
          </ButtonItem>
        </PanelSectionRow>
      ))}
      {message && (
        <PanelSectionRow>
          <div style={{ fontSize: 12, color: "#ffc84d" }}>{message}</div>
        </PanelSectionRow>
      )}
      <PanelSectionRow>
        <SliderField
          label={`Décalage : ${formatOffset(settings.offset_seconds)}`}
          description="Positif : la tempête arrive plus tard en jeu que sur le site."
          value={settings.offset_seconds / 60}
          min={-30}
          max={30}
          step={0.5}
          onChange={(v) => updateSettings({ offset_seconds: Math.round(v * 60) })}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <SliderField
          label={`Taille de la pluie : ${settings.rain_scale} %`}
          description="100 % : la tempête dessinée sur la carte de Merfolk's Lullaby (~650 m de rayon)."
          value={settings.rain_scale}
          min={50}
          max={400}
          step={10}
          onChange={(v) => updateSettings({ rain_scale: v })}
        />
      </PanelSectionRow>
    </PanelSection>
  );
}

function Content() {
  const [state, setState] = useState<State | null>(null);
  const [editMode, setEditMode] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  const apply = (s: State) => setState(syncClock(s));
  const refresh = () => getState().then(apply).catch(() => {});

  useEffect(() => {
    refresh();
    // Statuses change when the storm passes an island or a forecast arrives.
    const id = setInterval(refresh, 30_000);
    return () => clearInterval(id);
  }, []);

  if (!state) {
    return (
      <PanelSection>
        <PanelSectionRow>Chargement des prévisions…</PanelSectionRow>
      </PanelSection>
    );
  }

  const { settings, statuses } = state;
  const updateSettings = (patch: Partial<Settings>) => {
    setState((prev) => (prev ? { ...prev, settings: { ...prev.settings, ...patch } } : prev));
    queueSettingsSave(patch, apply);
  };
  const openPicker = () => showModal(<IslandPickerModal chosen={settings.islands} onState={apply} />);
  // Edit mode keeps the chosen order, so ▲ ▼ move what the user sees.
  const rows = editMode ? [...statuses].sort((a, b) => a.index - b.index) : statuses;

  return (
    <>
      <PanelSection title="Tempête">
        {rows.length === 0 && (
          <PanelSectionRow>
            <div style={{ fontSize: 12, color: "#999" }}>
              Choisis une ou plusieurs îles : l'overlay affichera dans combien de temps la tempête y passe.
            </div>
          </PanelSectionRow>
        )}
        {rows.map((s, i) =>
          editMode ? (
            <PanelSectionRow key={s.id}>
              <Focusable style={{ display: "flex", gap: 6, alignItems: "center" }}>
                <div style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {s.name}
                </div>
                <DialogButton style={{ width: 40, minWidth: 40, padding: 0 }} disabled={i === 0}
                  onClick={async () => apply(await moveIsland(s.id, -1))}>▲</DialogButton>
                <DialogButton style={{ width: 40, minWidth: 40, padding: 0 }} disabled={i === rows.length - 1}
                  onClick={async () => apply(await moveIsland(s.id, 1))}>▼</DialogButton>
                <DialogButton style={{ width: 40, minWidth: 40, padding: 0 }}
                  onClick={async () => apply(await removeIsland(s.id))}>✕</DialogButton>
              </Focusable>
            </PanelSectionRow>
          ) : (
            <PanelSectionRow key={s.id}>
              <Field label={s.name} childrenContainerWidth="max" bottomSeparator="none">
                <LiveStatus s={s} />
              </Field>
            </PanelSectionRow>
          )
        )}
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={openPicker}>
            {settings.islands.length ? "Choisir les îles" : "+ Choisir des îles"}
          </ButtonItem>
        </PanelSectionRow>
        {settings.islands.length > 1 && (
          <PanelSectionRow>
            <ToggleField
              label="Modifier la liste"
              description={editMode ? "▲ ▼ pour l'ordre (si le tri par heure est coupé), ✕ pour retirer" : undefined}
              checked={editMode}
              onChange={setEditMode}
            />
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <div style={{ fontSize: 11, color: "#999", lineHeight: 1.4 }}>
            {state.forecast_until > 0 && (
              <div>
                Prévision jusqu'à {formatClock(state.forecast_until)}, reçue à {formatClock(state.forecast_at)}.
              </div>
            )}
            {state.forecast_error && <div style={{ color: "#ff8080" }}>{state.forecast_error}</div>}
            {state.islands_error && <div style={{ color: "#ff8080" }}>{state.islands_error}</div>}
            <div>
              Premières pluies sur l'île, calculées d'après les prévisions de Merfolk's Lullaby
              (merfolkslullaby.com) : environ 2 h, actualisées toutes les 24 min. À caler en jeu ci-dessous.
            </div>
          </div>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={refreshing}
            onClick={async () => {
              setRefreshing(true);
              try {
                apply(await refreshForecast());
              } finally {
                setRefreshing(false);
              }
            }}
          >
            {refreshing ? "Actualisation…" : "Actualiser la prévision"}
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      <CalibrationSection state={state} apply={apply} updateSettings={updateSettings} />

      <PanelSection title="Overlay en jeu">
        <OverlayStatus state={state} onState={apply} />
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={async () => apply(await previewOverlay())}>
            Aperçu de l'overlay (10 s)
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Afficher l'overlay"
            checked={settings.overlay_enabled}
            onChange={(v) => updateSettings({ overlay_enabled: v })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Seulement dans Sea of Thieves"
            description={state.game_running ? "Sea of Thieves est lancé." : "Sea of Thieves n'est pas lancé."}
            checked={settings.only_in_game}
            onChange={(v) => updateSettings({ only_in_game: v })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label="Position" childrenContainerWidth="max">
            <Dropdown
              rgOptions={positionOptions}
              selectedOption={settings.position}
              onChange={(o) => updateSettings({ position: o.data })}
            />
          </Field>
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label="Compte à rebours" childrenContainerWidth="max">
            <Dropdown
              rgOptions={showUnderOptions}
              selectedOption={settings.show_under_minutes}
              onChange={(o) => updateSettings({ show_under_minutes: o.data })}
            />
          </Field>
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Trier par heure d'arrivée"
            checked={settings.sort_by_time}
            onChange={(v) => updateSettings({ sort_by_time: v })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Masquer les îles sans tempête"
            description="Sinon elles affichent « Pas de tempête » (pas de pluie prévue dans les 2 h)."
            checked={settings.hide_without_storm}
            onChange={(v) => updateSettings({ hide_without_storm: v })}
          />
        </PanelSectionRow>
      </PanelSection>

      <UpdateSection />
    </>
  );
}

export default definePlugin(() => {
  // Offer to install Decky Overlay once if it is missing.
  getStartupState()
    .then(async (s) => {
      syncClock(s);
      if (s.overlay_status === "not_installed" && !s.settings.overlay_install_prompted) {
        const error = await installDeckyOverlay();
        syncClock(await overlayInstallPrompted());
        if (error) toaster.toast({ title: "Decky SoT", body: `Decky Overlay non installé : ${error}` });
      }
    })
    .catch(() => {});

  return {
    name: "Decky SoT",
    titleView: <div className={staticClasses.Title}>Decky SoT</div>,
    content: <Content />,
    icon: <FaSkullCrossbones />,
  };
});
