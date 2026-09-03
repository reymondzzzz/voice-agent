const BASS_HZ = 250
const MID_HZ = 2000
const TREBLE_HZ = 8000

export const SILENT_VOICE_BANDS = { vamplitude: 0, vbass: 0, vmid: 0, vtreble: 0 }
export const SILENT_VOICE_LEVELS = { vuser: SILENT_VOICE_BANDS, vagent: SILENT_VOICE_BANDS }

export function audioLevelFromBins(vbins) {
  if (vbins.length === 0) {
    return 0
  }
  let vsum = 0
  for (const vbin of vbins) {
    const vvalue = vbin / 255
    vsum += vvalue * vvalue
  }
  return Math.min(1, Math.sqrt(vsum / vbins.length) * 3)
}

export function bandsFromBins(vbins, vsampleRate, vfftSize) {
  if (vbins.length === 0 || vsampleRate <= 0 || vfftSize <= 0) {
    return SILENT_VOICE_BANDS
  }
  const vbinHz = vsampleRate / vfftSize
  return {
    vamplitude: audioLevelFromBins(vbins),
    vbass: bandEnergy(vbins, vbinHz, 0, BASS_HZ),
    vmid: bandEnergy(vbins, vbinHz, BASS_HZ, MID_HZ),
    vtreble: bandEnergy(vbins, vbinHz, MID_HZ, TREBLE_HZ),
  }
}

function bandEnergy(vbins, vbinHz, vfromHz, vtoHz) {
  const vfirst = Math.max(1, Math.floor(vfromHz / vbinHz))
  const vlast = Math.min(vbins.length - 1, Math.ceil(vtoHz / vbinHz))
  if (vlast < vfirst) {
    return 0
  }
  let vsum = 0
  for (let vindex = vfirst; vindex <= vlast; vindex++) {
    vsum += vbins[vindex] / 255
  }
  return Math.min(1, (vsum / (vlast - vfirst + 1)) * 2.2)
}

function readProbe(vprobe, vsampleRate) {
  if (!vprobe) {
    return SILENT_VOICE_BANDS
  }
  vprobe.vanalyser.getByteFrequencyData(vprobe.vbins)
  return bandsFromBins(vprobe.vbins, vsampleRate, vprobe.vanalyser.fftSize)
}

export class VoiceLevelMeter {
  constructor() {
    this.vcontext = null
    this.vuserProbe = null
    this.vagentProbe = null
  }

  sample(vuserTrack, vagentTrack) {
    if (typeof AudioContext === "undefined") {
      return SILENT_VOICE_LEVELS
    }
    this.vuserProbe = this.retune(this.vuserProbe, vuserTrack)
    this.vagentProbe = this.retune(this.vagentProbe, vagentTrack)
    if (this.vcontext?.state === "suspended") {
      void this.vcontext.resume()
    }
    const vsampleRate = this.vcontext?.sampleRate ?? 0
    return {
      vuser: readProbe(this.vuserProbe, vsampleRate),
      vagent: readProbe(this.vagentProbe, vsampleRate),
    }
  }

  close() {
    this.vuserProbe = this.retune(this.vuserProbe, null)
    this.vagentProbe = this.retune(this.vagentProbe, null)
    const vcontext = this.vcontext
    this.vcontext = null
    if (vcontext) {
      void vcontext.close()
    }
  }

  retune(vprobe, vtrack) {
    if (vprobe?.vtrack === vtrack) {
      return vprobe
    }
    if (vprobe) {
      vprobe.vsource.disconnect()
      vprobe.vanalyser.disconnect()
    }
    if (!vtrack) {
      return null
    }
    const vcontext = this.ensureContext()
    const vanalyser = vcontext.createAnalyser()
    vanalyser.fftSize = 512
    vanalyser.smoothingTimeConstant = 0.55
    const vsource = vcontext.createMediaStreamSource(new MediaStream([vtrack]))
    vsource.connect(vanalyser)
    return { vtrack, vsource, vanalyser, vbins: new Uint8Array(vanalyser.frequencyBinCount) }
  }

  ensureContext() {
    if (this.vcontext) {
      return this.vcontext
    }
    this.vcontext = new AudioContext()
    return this.vcontext
  }
}
