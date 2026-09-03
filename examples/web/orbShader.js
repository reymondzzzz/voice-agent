export const VOICE_ORB_VERTEX_SHADER = `
precision highp float;
attribute vec2 position;
varying vec2 vUv;
void main(){ vUv = position * 0.5 + 0.5; gl_Position = vec4(position, 0.0, 1.0); }
`

export const VOICE_ORB_FRAGMENT_SHADER = `
precision highp float;

uniform float uTime;
uniform vec3  uResolution;
uniform float uAmplitude;
uniform float uBass;
uniform float uMid;
uniform float uTreble;
uniform float uIntensity;
uniform float uNoiseScale;
uniform float uDistortion;
uniform float uGlow;
uniform vec3  uColorA;
uniform vec3  uColorB;
uniform vec2  uPointer;
varying vec2 vUv;

vec3 hash33(vec3 p){
  p = fract(p * vec3(0.1031, 0.11369, 0.13787));
  p += dot(p, p.yxz + 19.19);
  return -1.0 + 2.0 * fract(vec3(p.x + p.y, p.x + p.z, p.y + p.z) * p.zyx);
}

float snoise(vec3 p){
  const float K1 = 0.333333333;
  const float K2 = 0.166666667;
  vec3 i = floor(p + (p.x + p.y + p.z) * K1);
  vec3 d0 = p - (i - (i.x + i.y + i.z) * K2);
  vec3 e = step(vec3(0.0), d0 - d0.yzx);
  vec3 i1 = e * (1.0 - e.zxy);
  vec3 i2 = 1.0 - e.zxy * (1.0 - e);
  vec3 d1 = d0 - (i1 - K2);
  vec3 d2 = d0 - (i2 - K1);
  vec3 d3 = d0 - 0.5;
  vec4 h = max(0.6 - vec4(dot(d0, d0), dot(d1, d1), dot(d2, d2), dot(d3, d3)), 0.0);
  vec4 n = h * h * h * h * vec4(
    dot(d0, hash33(i)),
    dot(d1, hash33(i + i1)),
    dot(d2, hash33(i + i2)),
    dot(d3, hash33(i + 1.0))
  );
  return dot(vec4(31.316), n);
}

float fbm(vec3 p){
  float s = 0.0;
  float a = 0.5;
  for (int i = 0; i < 3; i++) { s += a * snoise(p); p *= 2.03; a *= 0.5; }
  return s;
}

float field(vec2 p){
  vec3 b = vec3(p * uNoiseScale, uTime * 0.16);
  vec3 w = b + uDistortion * vec3(fbm(b), fbm(b + 5.2), 0.0);
  float big = fbm(w * 0.75 + vec3(0.0, 0.0, uTime * 0.05)) * (0.55 + uBass * 1.30);
  float surf = fbm(w * 1.90 + vec3(0.0, 0.0, uTime * 0.30)) * (0.28 + uMid * 0.85);
  return big + surf;
}

void main(){
  vec2 frag = vUv * uResolution.xy;
  vec2 p = (frag - uResolution.xy * 0.5) / min(uResolution.x, uResolution.y) * 2.0;
  p += uPointer * 0.04;

  float r = length(p);
  float h = field(p);
  float e = 0.006;
  float hx = field(p + vec2(e, 0.0)) - h;
  float hy = field(p + vec2(0.0, e)) - h;
  vec3 N = normalize(vec3(-hx * 26.0, -hy * 26.0, 1.0));

  // the silhouette follows a low-frequency field so loud audio swells the orb fluidly instead
  // of growing high-frequency fur along the rim
  float hLow = field(p * 0.30);
  float deform = 0.038 * hLow * (0.35 + 0.65 * uDistortion) + 0.030 * uBass + 0.018 * uAmplitude;
  float radius = 0.62 + deform;
  float edge = 0.030 + 0.030 * uAmplitude;
  float body = smoothstep(radius + edge, radius - edge, r);

  vec3 V = vec3(0.0, 0.0, 1.0);
  float ndv = clamp(dot(N, V), 0.0, 1.0);
  float shell = smoothstep(radius * 0.62, radius * 1.01, r);
  float fres = pow(1.0 - ndv, 3.0) * 0.16 + pow(shell, 2.0) * 1.10;

  vec3 L = normalize(vec3(0.42, 0.62, 0.66));
  float diff = clamp(dot(N, L) * 0.5 + 0.5, 0.0, 1.0);
  float spec = pow(clamp(dot(reflect(-L, N), V), 0.0, 1.0), 42.0);

  float depth = clamp(1.0 - r / max(radius, 0.001), 0.0, 1.0);
  float flow = clamp(0.5 + 0.5 * h, 0.0, 1.0);

  vec3 core = mix(uColorB, uColorA, clamp(depth * 1.15, 0.0, 1.0));
  vec3 interior = core * (0.03 + 0.22 * depth) * (0.45 + 0.75 * diff);
  float currents = smoothstep(0.48, 0.92, flow);
  float band = pow(depth * (1.0 - depth) * 4.0, 0.65);
  interior += uColorB * currents * 1.15 * band;
  interior += uColorB * uMid * 0.45 * band;
  interior += uColorB * pow(depth, 3.0) * 0.10 * (0.4 + 0.6 * uBass);

  float irid = 0.5 + 0.5 * sin(5.5 * fres + 2.2 * flow + uTime * 0.35);
  vec3 rimCol = mix(uColorB, uColorB.zxy, irid * 0.22);

  vec3 col = interior + rimCol * fres * (0.85 + 0.90 * uAmplitude) * uIntensity;
  col += spec * 0.30 * (0.35 + uMid);
  col += rimCol * fbm(vec3(p * 7.5, uTime * 0.9)) * uTreble * 0.09 * body * band;

  float halo = exp(-3.6 * max(r - radius, 0.0) / max(edge * 3.4, 0.001));
  vec3 haloCol = rimCol * halo * uGlow * (0.30 + 0.75 * uAmplitude) * (1.0 - body);

  float alpha = clamp(body * (0.55 + 0.45 * fres) + halo * uGlow * 0.45 * (1.0 - body), 0.0, 1.0);
  vec3 outc = col * body + haloCol;
  outc = outc / (1.0 + outc * 0.38);
  gl_FragColor = vec4(outc, alpha);
}
`

export const VOICE_ORB_UNIFORM_NAMES = [
  "uTime",
  "uResolution",
  "uAmplitude",
  "uBass",
  "uMid",
  "uTreble",
  "uIntensity",
  "uNoiseScale",
  "uDistortion",
  "uGlow",
  "uColorA",
  "uColorB",
  "uPointer",
]

