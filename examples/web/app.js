import {
  Room,
  RoomEvent,
  Track,
  createLocalAudioTrack,
} from "https://cdn.jsdelivr.net/npm/livekit-client@2.22.1/dist/livekit-client.esm.mjs"
import { SILENT_VOICE_LEVELS, VoiceLevelMeter } from "/static/audioLevel.js?v=2"
import { VoiceOrb } from "/static/orb.js?v=16"

const AGENT_ROLES = {
  boss: "answers first and routes the call",
  alice: "weather specialist",
  bob: "timekeeper",
}

const vui = {
  status: document.getElementById("status"),
  banner: document.getElementById("banner"),
  orb: document.getElementById("orb"),
  activeName: document.getElementById("activeName"),
  activeRole: document.getElementById("activeRole"),
  roster: document.getElementById("roster"),
  transcript: document.getElementById("transcript"),
  connect: document.getElementById("connect"),
  mute: document.getElementById("mute"),
}

const vmeter = new VoiceLevelMeter()
const vaudio = new Audio()
vaudio.autoplay = true

let vroom = null
let vmicrophone = null
let vagentTrack = null
let vlevels = SILENT_VOICE_LEVELS

function setStatus(vstate, vtext) {
  vui.status.dataset.state = vstate
  vui.status.textContent = vtext
}

function showBanner(vtext) {
  vui.banner.textContent = vtext
  vui.banner.hidden = false
}

function clearBanner() {
  vui.banner.hidden = true
}

function setActiveAgent(vagentId, vagentName) {
  const vid = AGENT_ROLES[vagentId] ? vagentId : "boss"
  vorb.setAgent(vid)
  document.documentElement.style.setProperty("--agent", `var(--${vid})`)
  vui.activeName.textContent = vagentName || vid
  vui.activeRole.textContent = AGENT_ROLES[vid]
  for (const vitem of vui.roster.children) {
    const vactive = vitem.dataset.agent === vid
    vitem.dataset.active = String(vactive)
    vitem.setAttribute("aria-current", vactive ? "true" : "false")
  }
}

function addTranscriptLine(vfrom, vtext) {
  const vline = document.createElement("div")
  vline.className = "line"
  vline.dataset.from = vfrom
  const vlabel = document.createElement("span")
  vlabel.className = "from"
  vlabel.textContent = vfrom
  vline.append(vlabel, document.createTextNode(vtext))
  vui.transcript.append(vline)
  vui.transcript.scrollTop = vui.transcript.scrollHeight
}

function captureUnsupportedReason() {
  if (!window.isSecureContext) {
    return "Microphone capture needs a secure context. Open this page over localhost or https."
  }
  if (!navigator.mediaDevices?.getUserMedia) {
    return "This browser does not expose getUserMedia, so the microphone cannot be opened."
  }
  return ""
}

const SPEECH_LEVEL = 0.06
const THINKING_WINDOW_MS = 6000
const MAX_FRAME_SECONDS = 0.1

const vorb = new VoiceOrb(vui.orb)
let vfaultState = ""
let vlastUserSpeechMs = 0
let vlastFrameMs = performance.now()
let vframeHandle = 0

function orbState() {
  if (vfaultState) {
    return vfaultState
  }
  if (!vroom) {
    return "idle"
  }
  if (vlevels.vagent.vamplitude > SPEECH_LEVEL) {
    return "speaking"
  }
  if (vlevels.vuser.vamplitude > SPEECH_LEVEL) {
    vlastUserSpeechMs = performance.now()
    return "listening"
  }
  return performance.now() - vlastUserSpeechMs < THINKING_WINDOW_MS ? "thinking" : "listening"
}

function orbLevels() {
  const vstate = vorb.vstate
  if (vstate === "speaking") {
    return vlevels.vagent
  }
  return vstate === "listening" ? vlevels.vuser : SILENT_VOICE_LEVELS.vuser
}

function resizeOrb() {
  const vsize = vui.orb.getBoundingClientRect().width
  const vbudget = window.innerWidth < 760 ? 1.5 : 2
  vorb.resize(vsize, Math.min(window.devicePixelRatio || 1, vbudget))
}

function tick(vtime) {
  const vdeltaSeconds = Math.min((vtime - vlastFrameMs) / 1000, MAX_FRAME_SECONDS)
  vlastFrameMs = vtime
  vlevels = vroom ? vmeter.sample(vmicrophone?.mediaStreamTrack ?? null, vagentTrack) : SILENT_VOICE_LEVELS
  vorb.setState(orbState())
  vorb.render(vdeltaSeconds, orbLevels())
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
  if (vtrack.kind !== Track.Kind.Audio) {
    return
  }
  vagentTrack = vtrack.mediaStreamTrack
  vtrack.attach(vaudio)
}

function onTrackUnsubscribed(vtrack) {
  if (vtrack.mediaStreamTrack === vagentTrack) {
    vagentTrack = null
  }
  vtrack.detach(vaudio)
}

function onAttributesChanged(vattributes) {
  if (vattributes.active_agent_id) {
    setActiveAgent(vattributes.active_agent_id, vattributes.active_agent_name)
  }
}

function onTranscription(vsegments, vparticipant) {
  const vfrom = vparticipant && !vparticipant.isLocal ? vui.activeName.textContent : "you"
  for (const vsegment of vsegments) {
    if (vsegment.final && vsegment.text.trim()) {
      addTranscriptLine(vfrom, vsegment.text.trim())
    }
  }
}

async function connect() {
  const vreason = captureUnsupportedReason()
  if (vreason) {
    setStatus("error", "unsupported")
    showBanner(vreason)
    return
  }

  clearBanner()
  vfaultState = ""
  vui.transcript.replaceChildren()
  vui.connect.disabled = true
  setStatus("connecting", "connecting")

  try {
    const vresponse = await fetch("/token")
    if (!vresponse.ok) {
      throw new Error(`token endpoint returned ${vresponse.status}`)
    }
    const vgrant = await vresponse.json()

    vroom = new Room()
    vroom
      .on(RoomEvent.TrackSubscribed, onTrackSubscribed)
      .on(RoomEvent.TrackUnsubscribed, onTrackUnsubscribed)
      .on(RoomEvent.ParticipantAttributesChanged, onAttributesChanged)
      .on(RoomEvent.TranscriptionReceived, onTranscription)
      .on(RoomEvent.MediaDevicesError, (verror) => showBanner(`Microphone error: ${verror.message}`))
      .on(RoomEvent.Disconnected, () => disconnect())

    await vroom.connect(vgrant.vlk_url, vgrant.vlk_token, { autoSubscribe: true })
    await vroom.startAudio()

    vmicrophone = await createLocalAudioTrack({ echoCancellation: true, noiseSuppression: true })
    await vroom.localParticipant.publishTrack(vmicrophone, { source: Track.Source.Microphone })

    setStatus("live", `live · ${vgrant.vroom}`)
    vui.connect.textContent = "Hang up"
    vui.connect.dataset.live = "true"
    vui.connect.disabled = false
    vui.mute.disabled = false
  } catch (verror) {
    const vdenied = verror.name === "NotAllowedError" || verror.name === "SecurityError"
    vfaultState = vdenied ? "permissionDenied" : "error"
    setStatus("error", vdenied ? "microphone blocked" : "failed")
    showBanner(
      vdenied
        ? "The microphone was blocked. Allow it for this site and connect again."
        : `Could not start the call: ${verror.message}`,
    )
    await disconnect()
  }
}

async function disconnect() {
  vui.mute.disabled = true
  vui.mute.textContent = "Mute"
  vui.connect.textContent = "Connect"
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

vui.connect.addEventListener("click", () => {
  if (vroom) {
    void disconnect()
    return
  }
  void connect()
})

vui.mute.addEventListener("click", async () => {
  if (!vmicrophone) {
    return
  }
  const vmuted = !vmicrophone.isMuted
  await (vmuted ? vmicrophone.mute() : vmicrophone.unmute())
  vui.mute.textContent = vmuted ? "Unmute" : "Mute"
})

const vunsupported = captureUnsupportedReason()
if (vunsupported) {
  showBanner(vunsupported)
}
setActiveAgent("boss", "Boss")

window.addEventListener("pointermove", (vevent) => {
  const vbounds = vui.orb.getBoundingClientRect()
  const vx = (vevent.clientX - (vbounds.left + vbounds.width / 2)) / Math.max(vbounds.width, 1)
  const vy = (vevent.clientY - (vbounds.top + vbounds.height / 2)) / Math.max(vbounds.height, 1)
  vorb.setPointer(Math.max(-1.5, Math.min(1.5, vx)), Math.max(-1.5, Math.min(1.5, -vy)))
})

window.addEventListener("resize", resizeOrb)
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    stopRendering()
  } else {
    startRendering()
  }
})
window.addEventListener("pagehide", () => {
  stopRendering()
  vmeter.close()
  vorb.dispose()
})

if (!vorb.vsupported) {
  showBanner("WebGL is unavailable, so the orb cannot render. The call itself still works.")
}
resizeOrb()
startRendering()

window.voiceOrb = vorb
