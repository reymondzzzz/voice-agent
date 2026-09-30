import {
  Room,
  RoomEvent,
  Track,
  createLocalAudioTrack,
} from "https://cdn.jsdelivr.net/npm/livekit-client@2.22.1/dist/livekit-client.esm.mjs"
import { SILENT_VOICE_LEVELS, VoiceLevelMeter } from "/static/audioLevel.js?v=2"
import { VoiceOrb } from "/static/orb.js?v=16"

const KAREN_TOPIC = "karen"
const KAREN_PALETTE = "alice"
const SPEECH_LEVEL = 0.06
const THINKING_TIMEOUT_MS = 20000
const MAX_FRAME_SECONDS = 0.1
const STICK_TO_BOTTOM_PX = 48
const STATE_TEXT = {
  idle: "Not connected",
  listening: "Listening to the room",
  thinking: "Thinking…",
  speaking: "Speaking",
  error: "Something went wrong",
  permissionDenied: "Microphone blocked",
}

const vparams = new URLSearchParams(location.search)
const VOBSERVE = vparams.get("observe") === "1"
const VROOM = vparams.get("room")?.trim() ?? ""

const vui = {
  status: document.getElementById("status"),
  banner: document.getElementById("banner"),
  orb: document.getElementById("orb"),
  state: document.getElementById("state"),
  tasks: document.getElementById("tasks"),
  transcript: document.getElementById("transcript"),
  connect: document.getElementById("connect"),
  mute: document.getElementById("mute"),
  brandNote: document.getElementById("brandNote"),
  hint: document.getElementById("hint"),
}

const vmeter = new VoiceLevelMeter()
const vaudio = new Audio()
vaudio.autoplay = true
// Watching a Meet call, Karen is already heard through Meet; the element stays muted and only feeds the orb.
vaudio.muted = VOBSERVE
const vorb = new VoiceOrb(vui.orb)
vorb.setAgent(KAREN_PALETTE)
const vdecoder = new TextDecoder()
const vtaskItems = new Map()
const vlinesByTs = new Map()

let vroom = null
let vmicrophone = null
let vagentTrack = null
let vlevels = SILENT_VOICE_LEVELS
let vfaultState = ""
let vthinkingSince = 0
let vshownState = ""
let vlastFrameMs = performance.now()
let vframeHandle = 0

function setStatus(vstate, vtext) {
  vui.status.dataset.state = vstate
  vui.status.textContent = vtext
}

function showBanner(vtext) {
  vui.banner.textContent = vtext
  vui.banner.hidden = false
}

function captureUnsupportedReason() {
  if (VOBSERVE) {
    return VROOM ? "" : "Add the bridge's room to the address: /karen?observe=1&room=voice-meet-…"
  }
  if (!window.isSecureContext) {
    return "Microphone capture needs a secure context. Open this page over localhost or https."
  }
  if (!navigator.mediaDevices?.getUserMedia) {
    return "This browser does not expose getUserMedia, so the microphone cannot be opened."
  }
  return ""
}

function clockTime(vseconds) {
  return new Date(vseconds * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })
}

function lineKind(vturn) {
  if (vturn.role === "assistant") {
    return "karen"
  }
  if (vturn.role === "system") {
    return vturn.speaker === "tool" ? "tool" : "note"
  }
  return isYou(vturn.speaker) ? "you" : "other"
}

function isYou(vspeaker) {
  return Boolean(vroom) && vspeaker === vroom.localParticipant.name
}

function addTurn(vturn) {
  const vatBottom = vui.transcript.scrollHeight - vui.transcript.scrollTop - vui.transcript.clientHeight < STICK_TO_BOTTOM_PX
  const vkind = lineKind(vturn)
  const vline = document.createElement("article")
  vline.className = "line"
  vline.dataset.kind = vkind
  const vhead = document.createElement("header")
  const vwho = document.createElement("span")
  vwho.className = "who"
  vwho.textContent = { you: "You", note: "Background result", tool: "Tool" }[vkind] ?? vturn.speaker
  const vtime = document.createElement("time")
  vtime.dateTime = new Date(vturn.ts * 1000).toISOString()
  vtime.textContent = clockTime(vturn.ts)
  vhead.append(vwho, vtime)
  const vlatency = Object.entries(vturn.latency ?? {})
  if (vlatency.length) {
    const vspan = document.createElement("span")
    vspan.className = "latency"
    vspan.textContent = vlatency.map(([vname, vseconds]) => `${vname} ${vseconds.toFixed(2)}s`).join(" · ")
    vhead.append(vspan)
  }
  const vtext = document.createElement("div")
  vtext.textContent = vturn.text
  vline.append(vhead, vtext)
  vui.transcript.append(vline)
  vlinesByTs.set(vturn.ts, vline)
  if (vatBottom) {
    vui.transcript.scrollTop = vui.transcript.scrollHeight
  }
}

// The agent logs a line the moment it is heard and decides a moment later whether it was said to Karen.
function markAddressed(vts) {
  const vhead = vlinesByTs.get(vts)?.querySelector("header")
  if (vhead && !vhead.querySelector(".to")) {
    const vto = document.createElement("span")
    vto.className = "to"
    vto.textContent = "→ Мэгги"
    vhead.append(vto)
  }
}

// A pause mid-sentence splits a request into lines; the agent answers them as one, and so does the page.
function mergeTurn(vevent) {
  const vkept = vlinesByTs.get(vevent.into)
  const vpart = vlinesByTs.get(vevent.ts)
  if (!vkept || !vpart) {
    return
  }
  vkept.lastElementChild.textContent = vevent.text
  vpart.remove()
  vlinesByTs.set(vevent.ts, vkept)
}

function upsertTask(vtask) {
  let vitem = vtaskItems.get(vtask.id)
  if (!vitem) {
    vitem = document.createElement("li")
    vitem.className = "task"
    const vdot = document.createElement("i")
    vdot.className = "dot"
    vdot.setAttribute("aria-hidden", "true")
    const vgoal = document.createElement("span")
    vgoal.className = "goal"
    const vmeta = document.createElement("span")
    vmeta.className = "meta"
    vitem.append(vdot, vgoal, vmeta)
    vtaskItems.set(vtask.id, vitem)
    vui.tasks.prepend(vitem)
  }
  vitem.dataset.status = vtask.status
  vitem.children[1].textContent = vtask.goal
  vitem.children[1].title = vtask.goal
  const vlabel = { running: "working", completed: "done", failed: "failed", cancelled: "cancelled", superseded: "replaced" }[vtask.status] ?? vtask.status
  vitem.children[2].textContent = `${vlabel} · asked by ${isYou(vtask.requester) ? "you" : vtask.requester}`
}

function onData(vpayload, _vparticipant, _vkind, vtopic) {
  if (vtopic !== KAREN_TOPIC) {
    return
  }
  const vevent = JSON.parse(vdecoder.decode(vpayload))
  if (vevent.type === "turn") {
    addTurn(vevent)
  } else if (vevent.type === "merged") {
    mergeTurn(vevent)
  } else if (vevent.type === "addressed") {
    markAddressed(vevent.ts)
  } else if (vevent.type === "task") {
    upsertTask(vevent)
  } else if (vevent.type === "state") {
    vthinkingSince = vevent.state === "thinking" ? performance.now() : 0
  }
}

function orbState() {
  if (vfaultState) {
    return vfaultState
  }
  if (!vroom) {
    return "idle"
  }
  if (vlevels.vagent.vamplitude > SPEECH_LEVEL) {
    vthinkingSince = 0
    return "speaking"
  }
  if (vthinkingSince && performance.now() - vthinkingSince < THINKING_TIMEOUT_MS) {
    return "thinking"
  }
  return "listening"
}

function orbLevels(vstate) {
  if (vstate === "speaking") {
    return vlevels.vagent
  }
  return vstate === "listening" ? vlevels.vuser : SILENT_VOICE_LEVELS.vuser
}

function showState(vstate) {
  if (vstate !== vshownState) {
    vshownState = vstate
    vui.state.textContent = STATE_TEXT[vstate]
  }
}

function resizeOrb() {
  const vsize = vui.orb.getBoundingClientRect().width
  vorb.resize(vsize, Math.min(window.devicePixelRatio || 1, window.innerWidth < 760 ? 1.5 : 2))
}

function tick(vtime) {
  const vdeltaSeconds = Math.min((vtime - vlastFrameMs) / 1000, MAX_FRAME_SECONDS)
  vlastFrameMs = vtime
  vlevels = vroom ? vmeter.sample(vmicrophone?.mediaStreamTrack ?? null, vagentTrack) : SILENT_VOICE_LEVELS
  const vstate = orbState()
  vorb.setState(vstate)
  vorb.render(vdeltaSeconds, orbLevels(vstate))
  showState(vstate)
  vframeHandle = requestAnimationFrame(tick)
}

function startRendering() {
  if (!vframeHandle) {
    vlastFrameMs = performance.now()
    vframeHandle = requestAnimationFrame(tick)
  }
}

function stopRendering() {
  if (vframeHandle) {
    cancelAnimationFrame(vframeHandle)
    vframeHandle = 0
  }
}

function onTrackSubscribed(vtrack) {
  if (vtrack.kind === Track.Kind.Audio) {
    vagentTrack = vtrack.mediaStreamTrack
    vtrack.attach(vaudio)
  }
}

function onTrackUnsubscribed(vtrack) {
  if (vtrack.mediaStreamTrack === vagentTrack) {
    vagentTrack = null
  }
  vtrack.detach(vaudio)
}

async function connect() {
  const vreason = captureUnsupportedReason()
  if (vreason) {
    setStatus("error", "unsupported")
    showBanner(vreason)
    return
  }
  vui.banner.hidden = true
  vfaultState = ""
  vthinkingSince = 0
  vui.transcript.replaceChildren()
  vui.tasks.replaceChildren()
  vtaskItems.clear()
  vlinesByTs.clear()
  vui.connect.disabled = true
  setStatus("connecting", "connecting")

  try {
    const vquery = new URLSearchParams({ room: VROOM, ...(VOBSERVE ? { observe: "1" } : {}) })
    const vresponse = await fetch(`/token?${vquery}`)
    if (!vresponse.ok) {
      throw new Error(`token endpoint returned ${vresponse.status}`)
    }
    const vgrant = await vresponse.json()
    vroom = new Room()
    vroom
      .on(RoomEvent.TrackSubscribed, onTrackSubscribed)
      .on(RoomEvent.TrackUnsubscribed, onTrackUnsubscribed)
      .on(RoomEvent.DataReceived, onData)
      .on(RoomEvent.MediaDevicesError, (verror) => showBanner(`Microphone error: ${verror.message}`))
      .on(RoomEvent.Disconnected, () => disconnect())
    await vroom.connect(vgrant.vlk_url, vgrant.vlk_token, { autoSubscribe: true })
    await vroom.startAudio()
    if (!VOBSERVE) {
      vmicrophone = await createLocalAudioTrack({ echoCancellation: true, noiseSuppression: true })
      await vroom.localParticipant.publishTrack(vmicrophone, { source: Track.Source.Microphone })
    }

    setStatus("live", `${VOBSERVE ? "watching" : "live"} · ${vgrant.vroom}`)
    vui.connect.textContent = VOBSERVE ? "Stop watching" : "Hang up"
    vui.connect.dataset.live = "true"
    vui.connect.disabled = false
    vui.mute.disabled = VOBSERVE
  } catch (verror) {
    const vdenied = verror.name === "NotAllowedError" || verror.name === "SecurityError"
    vfaultState = vdenied ? "permissionDenied" : "error"
    setStatus("error", vdenied ? "microphone blocked" : "failed")
    showBanner(vdenied ? "The microphone was blocked. Allow it for this site and connect again." : `Could not start the call: ${verror.message}`)
    await disconnect()
  }
}

async function disconnect() {
  vui.mute.disabled = true
  vui.mute.textContent = "Mute"
  vui.connect.textContent = VOBSERVE ? "Watch" : "Connect"
  delete vui.connect.dataset.live
  vui.connect.disabled = false
  if (vmicrophone) {
    vmicrophone.stop()
    vmicrophone = null
  }
  vagentTrack = null
  vlevels = SILENT_VOICE_LEVELS
  const vprevious = vroom
  vroom = null
  if (vprevious) {
    await vprevious.disconnect()
  }
  if (vui.status.dataset.state !== "error") {
    setStatus("idle", "idle")
  }
}

vui.connect.addEventListener("click", () => void (vroom ? disconnect() : connect()))

vui.mute.addEventListener("click", async () => {
  if (!vmicrophone) {
    return
  }
  const vmuted = !vmicrophone.isMuted
  await (vmuted ? vmicrophone.mute() : vmicrophone.unmute())
  vui.mute.textContent = vmuted ? "Unmute" : "Mute"
})

window.addEventListener("resize", resizeOrb)
document.addEventListener("visibilitychange", () => (document.hidden ? stopRendering() : startRendering()))
window.addEventListener("pagehide", () => {
  stopRendering()
  vmeter.close()
  vorb.dispose()
})

if (VOBSERVE) {
  vui.connect.textContent = "Watch"
  vui.mute.hidden = true
  vui.brandNote.textContent = `— watching the Meet call${VROOM ? ` · ${VROOM}` : ""}`
  vui.hint.textContent = "You talk to Мэгги in Meet. This page only watches: what she hears, the tools she calls and her background work."
}
const vunsupported = captureUnsupportedReason()
if (vunsupported) {
  showBanner(vunsupported)
}
if (!vorb.vsupported) {
  showBanner("WebGL is unavailable, so the orb cannot render. The call itself still works.")
}
resizeOrb()
startRendering()
