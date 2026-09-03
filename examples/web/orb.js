import {
  VOICE_ORB_FRAGMENT_SHADER,
  VOICE_ORB_UNIFORM_NAMES,
  VOICE_ORB_VERTEX_SHADER,
} from "/static/orbShader.js?v=14"

// uColorA is the core and uColorB the rim, so each agent is a dark cool interior lit by its own
// signature edge. Two similar warm tones read as a flat marble; the separation is what gives the
// orb depth.
const AGENT_COLORS = {
  boss: [[0.09, 0.05, 0.13], [1.0, 0.68, 0.28]],
  alice: [[0.03, 0.09, 0.14], [0.36, 0.86, 0.98]],
  bob: [[0.07, 0.04, 0.15], [0.72, 0.54, 1.0]],
}
// iterated every frame, so the key list is built once rather than allocating in render()
const ORB_PARAMETERS = ["vintensity", "vnoiseScale", "vdistortion", "vglow", "vtimeScale", "vaudioGain", "vswirl", "vfloor"]

const FAULT_COLORS = {
  error: [[0.12, 0.04, 0.07], [0.95, 0.38, 0.45]],
  permissionDenied: [[0.10, 0.08, 0.05], [0.85, 0.70, 0.42]],
}

// The shader is a rim-lit shell: the interior is deliberately dark and almost all of the light
// comes from fresnel and halo scaled by uIntensity and uGlow, so these run far above 1. vfloor
// keeps a little amplitude alive in silence, otherwise the orb reads as a dead brown ball.
const ORB_STATE_TARGETS = {
  idle: { vintensity: 1.05, vnoiseScale: 4.5, vdistortion: 0.5, vglow: 0.85, vtimeScale: 0.5, vaudioGain: 0.25, vswirl: 0.03, vfloor: 0.16 },
  listening: { vintensity: 1.35, vnoiseScale: 5.5, vdistortion: 0.6, vglow: 1.05, vtimeScale: 1.0, vaudioGain: 1.0, vswirl: 0.1, vfloor: 0.1 },
  thinking: { vintensity: 1.15, vnoiseScale: 2.2, vdistortion: 1.45, vglow: 0.9, vtimeScale: 0.42, vaudioGain: 0.15, vswirl: 0.9, vfloor: 0.2 },
  speaking: { vintensity: 1.6, vnoiseScale: 6.0, vdistortion: 0.7, vglow: 1.3, vtimeScale: 1.15, vaudioGain: 1.15, vswirl: 0.2, vfloor: 0.08 },
  error: { vintensity: 0.8, vnoiseScale: 3.4, vdistortion: 0.28, vglow: 0.5, vtimeScale: 0.35, vaudioGain: 0.0, vswirl: 0.0, vfloor: 0.14 },
  permissionDenied: { vintensity: 0.7, vnoiseScale: 3.0, vdistortion: 0.24, vglow: 0.42, vtimeScale: 0.28, vaudioGain: 0.0, vswirl: 0.0, vfloor: 0.11 },
}

const PARAMETER_HALF_LIFE_S = 0.28
const BAND_HALF_LIFE_S = 0.09
const POINTER_HALF_LIFE_S = 0.35
const REDUCED_MOTION_TIME_SCALE = 0.12

function approach(vcurrent, vtarget, vdeltaSeconds, vhalfLifeSeconds) {
  const vrate = 1 - Math.pow(2, -vdeltaSeconds / vhalfLifeSeconds)
  return vcurrent + (vtarget - vcurrent) * vrate
}

function compile(vgl, vtype, vsource) {
  const vshader = vgl.createShader(vtype)
  vgl.shaderSource(vshader, vsource)
  vgl.compileShader(vshader)
  if (!vgl.getShaderParameter(vshader, vgl.COMPILE_STATUS)) {
    const vlog = vgl.getShaderInfoLog(vshader)
    vgl.deleteShader(vshader)
    throw new Error(`orb shader failed to compile: ${vlog}`)
  }
  return vshader
}

export class VoiceOrb {
  constructor(vcanvas) {
    this.vcanvas = vcanvas
    this.vstate = "idle"
    this.vagent = "boss"
    this.vparams = { ...ORB_STATE_TARGETS.idle }
    this.vbands = { vamplitude: 0, vbass: 0, vmid: 0, vtreble: 0 }
    this.vcolorA = [...AGENT_COLORS.boss[0]]
    this.vcolorB = [...AGENT_COLORS.boss[1]]
    this.vpointer = { vx: 0, vy: 0, vtargetX: 0, vtargetY: 0 }
    this.vshaderTime = 0
    this.vswirlPhase = 0
    this.vlost = false
    this.vreducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false

    this.vonContextLost = (vevent) => {
      vevent.preventDefault()
      this.vlost = true
    }
    this.vonContextRestored = () => {
      this.vlost = false
      this.buildProgram()
    }
    vcanvas.addEventListener("webglcontextlost", this.vonContextLost)
    vcanvas.addEventListener("webglcontextrestored", this.vonContextRestored)

    this.vgl = vcanvas.getContext("webgl", { alpha: true, antialias: false, premultipliedAlpha: false, powerPreference: "low-power" })
    if (this.vgl) {
      this.buildProgram()
    }
  }

  get vsupported() {
    return Boolean(this.vgl)
  }

  buildProgram() {
    const vgl = this.vgl
    const vvertex = compile(vgl, vgl.VERTEX_SHADER, VOICE_ORB_VERTEX_SHADER)
    const vfragment = compile(vgl, vgl.FRAGMENT_SHADER, VOICE_ORB_FRAGMENT_SHADER)
    const vprogram = vgl.createProgram()
    vgl.attachShader(vprogram, vvertex)
    vgl.attachShader(vprogram, vfragment)
    vgl.linkProgram(vprogram)
    vgl.deleteShader(vvertex)
    vgl.deleteShader(vfragment)
    if (!vgl.getProgramParameter(vprogram, vgl.LINK_STATUS)) {
      throw new Error(`orb program failed to link: ${vgl.getProgramInfoLog(vprogram)}`)
    }

    this.vprogram = vprogram
    this.vbuffer = vgl.createBuffer()
    vgl.bindBuffer(vgl.ARRAY_BUFFER, this.vbuffer)
    vgl.bufferData(vgl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), vgl.STATIC_DRAW)
    const vposition = vgl.getAttribLocation(vprogram, "position")
    vgl.enableVertexAttribArray(vposition)
    vgl.vertexAttribPointer(vposition, 2, vgl.FLOAT, false, 0, 0)

    this.vuniforms = {}
    for (const vname of VOICE_ORB_UNIFORM_NAMES) {
      this.vuniforms[vname] = vgl.getUniformLocation(vprogram, vname)
    }
    vgl.useProgram(vprogram)
    vgl.enable(vgl.BLEND)
    vgl.blendFunc(vgl.SRC_ALPHA, vgl.ONE_MINUS_SRC_ALPHA)
  }

  setState(vstate) {
    if (ORB_STATE_TARGETS[vstate]) {
      this.vstate = vstate
    }
  }

  setAgent(vagent) {
    if (AGENT_COLORS[vagent]) {
      this.vagent = vagent
    }
  }

  setPointer(vx, vy) {
    this.vpointer.vtargetX = vx
    this.vpointer.vtargetY = vy
  }

  resize(vcssSize, vdevicePixelRatio) {
    const vpixels = Math.round(vcssSize * vdevicePixelRatio)
    if (this.vcanvas.width !== vpixels) {
      this.vcanvas.width = vpixels
      this.vcanvas.height = vpixels
    }
  }

  render(vdeltaSeconds, vlevels) {
    if (!this.vgl || this.vlost || !this.vprogram) {
      return
    }
    const vgl = this.vgl
    const vtargets = ORB_STATE_TARGETS[this.vstate]
    for (let vi = 0; vi < ORB_PARAMETERS.length; vi++) {
      const vkey = ORB_PARAMETERS[vi]
      this.vparams[vkey] = approach(this.vparams[vkey], vtargets[vkey], vdeltaSeconds, PARAMETER_HALF_LIFE_S)
    }

    const [vtargetA, vtargetB] = FAULT_COLORS[this.vstate] ?? AGENT_COLORS[this.vagent]
    for (let vi = 0; vi < 3; vi++) {
      this.vcolorA[vi] = approach(this.vcolorA[vi], vtargetA[vi], vdeltaSeconds, PARAMETER_HALF_LIFE_S)
      this.vcolorB[vi] = approach(this.vcolorB[vi], vtargetB[vi], vdeltaSeconds, PARAMETER_HALF_LIFE_S)
    }

    const vgain = this.vparams.vaudioGain
    const vfloor = this.vparams.vfloor * (0.75 + 0.25 * Math.sin(this.vshaderTime * 0.9))
    this.vbands.vamplitude = approach(this.vbands.vamplitude, Math.max(vlevels.vamplitude * vgain, vfloor), vdeltaSeconds, BAND_HALF_LIFE_S)
    this.vbands.vbass = approach(this.vbands.vbass, vlevels.vbass * vgain, vdeltaSeconds, BAND_HALF_LIFE_S)
    this.vbands.vmid = approach(this.vbands.vmid, vlevels.vmid * vgain, vdeltaSeconds, BAND_HALF_LIFE_S)
    this.vbands.vtreble = approach(this.vbands.vtreble, vlevels.vtreble * vgain, vdeltaSeconds, BAND_HALF_LIFE_S)

    this.vpointer.vx = approach(this.vpointer.vx, this.vpointer.vtargetX, vdeltaSeconds, POINTER_HALF_LIFE_S)
    this.vpointer.vy = approach(this.vpointer.vy, this.vpointer.vtargetY, vdeltaSeconds, POINTER_HALF_LIFE_S)

    const vbreath = 1 + 0.12 * Math.sin(this.vshaderTime * 0.7)
    const vscale = this.vreducedMotion ? REDUCED_MOTION_TIME_SCALE : this.vparams.vtimeScale
    this.vshaderTime += vdeltaSeconds * vscale * vbreath
    this.vswirlPhase += vdeltaSeconds * this.vparams.vswirl * 0.6

    const vsize = this.vcanvas.width
    vgl.viewport(0, 0, vsize, vsize)
    vgl.clearColor(0, 0, 0, 0)
    vgl.clear(vgl.COLOR_BUFFER_BIT)
    vgl.useProgram(this.vprogram)

    const vu = this.vuniforms
    vgl.uniform1f(vu.uTime, this.vshaderTime)
    vgl.uniform3f(vu.uResolution, vsize, vsize, 1)
    vgl.uniform1f(vu.uAmplitude, this.vbands.vamplitude)
    vgl.uniform1f(vu.uBass, this.vbands.vbass)
    vgl.uniform1f(vu.uMid, this.vbands.vmid)
    vgl.uniform1f(vu.uTreble, this.vbands.vtreble)
    vgl.uniform1f(vu.uIntensity, this.vparams.vintensity)
    vgl.uniform1f(vu.uNoiseScale, this.vparams.vnoiseScale)
    vgl.uniform1f(vu.uDistortion, this.vparams.vdistortion)
    vgl.uniform1f(vu.uGlow, this.vparams.vglow)
    vgl.uniform3f(vu.uColorA, this.vcolorA[0], this.vcolorA[1], this.vcolorA[2])
    vgl.uniform3f(vu.uColorB, this.vcolorB[0], this.vcolorB[1], this.vcolorB[2])
    vgl.uniform2f(
      vu.uPointer,
      this.vpointer.vx + Math.cos(this.vswirlPhase) * this.vparams.vswirl * 0.35,
      this.vpointer.vy + Math.sin(this.vswirlPhase) * this.vparams.vswirl * 0.35,
    )
    vgl.drawArrays(vgl.TRIANGLES, 0, 3)
  }

  dispose() {
    this.vcanvas.removeEventListener("webglcontextlost", this.vonContextLost)
    this.vcanvas.removeEventListener("webglcontextrestored", this.vonContextRestored)
    const vgl = this.vgl
    if (!vgl) {
      return
    }
    if (this.vbuffer) {
      vgl.deleteBuffer(this.vbuffer)
      this.vbuffer = null
    }
    if (this.vprogram) {
      vgl.deleteProgram(this.vprogram)
      this.vprogram = null
    }
    vgl.getExtension("WEBGL_lose_context")?.loseContext()
    this.vgl = null
  }
}

export { AGENT_COLORS, ORB_STATE_TARGETS }
