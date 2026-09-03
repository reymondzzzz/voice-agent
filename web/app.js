import {
  Room,
  RoomEvent,
  Track,
  createLocalAudioTrack,
} from "https://cdn.jsdelivr.net/npm/livekit-client@2.22.1/dist/livekit-client.esm.mjs"
import { SILENT_VOICE_LEVELS, VoiceLevelMeter } from "/static/audioLevel.js"

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
  document.documentElement.style.setProperty("--agent", `var(--${vid})`)
  vui.activeName.textContent = vagentName || vid
  vui.activeRole.textContent = AGENT_ROLES[vid]
  for (const vitem of vui.roster.children) {
    vitem.dataset.active = String(vitem.dataset.agent === vid)
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

function orbColor() {
  const vname = getComputedStyle(document.documentElement).getPropertyValue("--agent").trim()
  const vresolved = vname.startsWith("var(")
    ? getComputedStyle(document.documentElement).getPropertyValue(vname.slice(4, -1)).trim()
    : vname
  return vresolved || "#f0a742"
}

function drawOrb(vtime) {
  const vcanvas = vui.orb
  const vctx = vcanvas.getContext("2d")
  const vsize = vcanvas.width
  const vcentre = vsize / 2
  vctx.clearRect(0, 0, vsize, vsize)

  const vbands = vlevels.vagent.vamplitude > vlevels.vuser.vamplitude ? vlevels.vagent : vlevels.vuser
  const vcolor = orbColor()
  const vcore = vsize * 0.17 * (1 + vbands.vamplitude * 0.28)

  const vglow = vctx.createRadialGradient(vcentre, vcentre, vcore * 0.2, vcentre, vcentre, vcore * 2.6)
  vglow.addColorStop(0, `${vcolor}cc`)
  vglow.addColorStop(0.45, `${vcolor}33`)
  vglow.addColorStop(1, `${vcolor}00`)
  vctx.fillStyle = vglow
  vctx.beginPath()
  vctx.arc(vcentre, vcentre, vcore * 2.6, 0, Math.PI * 2)
  vctx.fill()

  vctx.fillStyle = vcolor
  vctx.beginPath()
  vctx.arc(vcentre, vcentre, vcore, 0, Math.PI * 2)
  vctx.fill()

  const vrings = [
    [vbands.vbass, 1.55, 5],
    [vbands.vmid, 1.95, 3],
    [vbands.vtreble, 2.35, 1.5],
  ]
  vrings.forEach(([venergy, vscale, vwidth], vindex) => {
    const vradius = vcore * vscale + venergy * vsize * 0.045
    const vspin = vtime / (2600 + vindex * 900)
    vctx.strokeStyle = vcolor
    vctx.globalAlpha = 0.12 + venergy * 0.62
    vctx.lineWidth = vwidth
    vctx.beginPath()
    vctx.arc(vcentre, vcentre, vradius, vspin, vspin + Math.PI * 1.35)
    vctx.stroke()
  })
  vctx.globalAlpha = 1
}

function tick(vtime) {
  vlevels = vroom ? vmeter.sample(vmicrophone?.mediaStreamTrack ?? null, vagentTrack) : SILENT_VOICE_LEVELS
  drawOrb(vtime)
  requestAnimationFrame(tick)
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
    setStatus("error", "failed")
    showBanner(`Could not start the call: ${verror.message}`)
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
requestAnimationFrame(tick)
