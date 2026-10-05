// Receipts hero: an iridescent glass lens (a twisted ribbon loop) that creator quotes drift through. While a quote is
// inside the lens its brand lights up lime: the product in one picture (we listen to videos; the brand said gets marked).
// Decoration only: loaded late, paused off screen / hidden / on request.
const THREE_URL = '/ui/vendor/three/three.module.min.js';
const BG = '#060708';                          // must match .glass-stage's background in the page CSS
const LIME = '#d0f854', INK = '#0b1a10';
const FALLBACK_QUOTES = [
  { who: '@maya.eats', a: 'iced capp, extra large, nobody talk to me.', brand: 'Tim Hortons', b: 'hits different', ts: '0:05', views: '412K views' },
  { who: '@sami.lifts', a: 'post-leg-day reward, honestly my', brand: 'Starbucks', b: 'order is a personality', ts: '0:31', views: '88K views' },
  { who: '@noor.cooks', a: 'this shawarma is unreal, and a cold', brand: 'Vimto', b: 'because I\u2019m a child', ts: '0:09', views: '1.3M views' },
  { who: '@dxb.dad', a: 'school run fuel:', brand: 'Almarai', b: 'laban, every single morning', ts: '0:14', views: '240K views' },
  { who: '@late.night.lina', a: 'it\u2019s 2am and I\u2019m ordering', brand: 'Talabat', b: 'again, don\u2019t judge me', ts: '0:22', views: '67K views' },
  { who: '@fitwithomar', a: 'the only shorts I train in are', brand: 'Gymshark', b: 'and that\u2019s not an ad', ts: '0:47', views: '510K views' },
  { who: '@hana.haul', a: 'the whole fit is', brand: 'Shein', b: 'and it cost less than lunch', ts: '0:03', views: '930K views' },
];

let THREE;

function ribbon(U = 420, V = 36) {
  // Closed tube with a flat elliptical section along an upright oval, given a half twist: the section maps onto itself
  // after a half turn, so the seam joins vertex j to j + V/2 and normals stay smooth.
  const pos = new Float32Array(U * V * 3), idx = [];
  const P = (u) => [1.28 * Math.sin(u) * (1 - 0.18 * Math.max(0, Math.cos(u)) ** 3), 1.78 * Math.cos(u), 0.38 * Math.sin(2 * u)];
  const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
  const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
  const norm = (a) => { const l = Math.hypot(...a) || 1; return [a[0] / l, a[1] / l, a[2] / l]; };
  for (let i = 0; i < U; i++) {
    const u = (i / U) * Math.PI * 2;
    const c = P(u), T = norm(sub(P(u + 1e-3), P(u - 1e-3)));
    const N = norm(cross([0, 0, 1], T)), B = cross(T, N);
    const th = u / 2 + 0.6, ct = Math.cos(th), st = Math.sin(th);
    const e1 = [N[0] * ct + B[0] * st, N[1] * ct + B[1] * st, N[2] * ct + B[2] * st];
    const e2 = [-N[0] * st + B[0] * ct, -N[1] * st + B[1] * ct, -N[2] * st + B[2] * ct];
    const a = 0.1 + 0.34 * Math.abs(Math.sin(u)) ** 0.9, b = 0.055;
    for (let j = 0; j < V; j++) {
      const f = (j / V) * Math.PI * 2, x = a * Math.cos(f), y = b * Math.sin(f), k = (i * V + j) * 3;
      pos[k] = c[0] + e1[0] * x + e2[0] * y; pos[k + 1] = c[1] + e1[1] * x + e2[1] * y; pos[k + 2] = c[2] + e1[2] * x + e2[2] * y;
    }
  }
  for (let i = 0; i < U; i++) for (let j = 0; j < V; j++) {
    const n = i + 1 < U ? i + 1 : 0, shift = i + 1 < U ? 0 : V / 2;
    const A = i * V + j, Bi = n * V + ((j + shift) % V), C = n * V + ((j + 1 + shift) % V), D = i * V + ((j + 1) % V);
    idx.push(A, Bi, D, Bi, C, D);
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  g.setIndex(idx);
  g.computeVertexNormals();
  return g;
}

function envMap(renderer) {
  // A dark studio with a few soft boxes (white key, cyan and violet strips, warm kicker): what the glass reflects.
  const env = new THREE.Scene();
  const room = new THREE.Mesh(new THREE.BoxGeometry(20, 20, 20), new THREE.MeshBasicMaterial({ color: 0x0b0c0e, side: THREE.BackSide }));
  env.add(room);
  const box = (w, h, x, y, z, rgb, look = [0, 0, 0]) => {
    const m = new THREE.Mesh(new THREE.PlaneGeometry(w, h), new THREE.MeshBasicMaterial({ color: new THREE.Color(...rgb), side: THREE.DoubleSide }));
    m.position.set(x, y, z); m.lookAt(...look); env.add(m);
  };
  box(9, 3, 0, 8.5, 2, [5, 5, 5]);
  box(1.2, 12, -8.5, 0, 1, [0.4, 2.2, 3.2]);
  box(1.2, 12, 8.5, 1, -1, [2.4, 0.8, 3.6]);
  box(6, 1.4, 3, -6, 6, [3, 1.8, 1.1]);
  box(3, 3, -3, 2, -8.5, [1.4, 1.8, 0.5]);
  const pm = new THREE.PMREMGenerator(renderer);
  const tex = pm.fromScene(env, 0.035).texture;
  pm.dispose();
  return tex;
}

function wrapWords(ctx, words, max) {
  const lines = [[]]; let w = 0;
  for (const word of words) {
    const ww = ctx.measureText(word.t + ' ').width;
    if (w + ww > max && lines.at(-1).length) { lines.push([]); w = 0; }
    lines.at(-1).push({ ...word, x: w, w: ctx.measureText(word.t).width }); w += ww;
  }
  return lines;
}

function cardTexture(q, lit) {
  const W = 768, H = 440, cv = document.createElement('canvas');
  cv.width = W; cv.height = H;
  const x = cv.getContext('2d');
  const r = 34;
  x.beginPath(); x.roundRect(4, 4, W - 8, H - 8, r);
  const g = x.createLinearGradient(0, 0, 0, H);
  g.addColorStop(0, lit ? '#1c211b' : '#16191a'); g.addColorStop(1, '#0d0f10');
  x.fillStyle = g; x.fill();
  x.lineWidth = 3; x.strokeStyle = lit ? 'rgba(208,248,84,0.55)' : 'rgba(255,255,255,0.14)'; x.stroke();
  // frame thumbnail
  x.save(); x.beginPath(); x.roundRect(36, 36, 96, 170, 16); x.clip();
  const tg = x.createLinearGradient(36, 36, 132, 206); tg.addColorStop(0, '#24443a'); tg.addColorStop(1, '#3c2f5c');
  x.fillStyle = tg; x.fillRect(36, 36, 96, 170); x.restore();
  x.fillStyle = 'rgba(255,255,255,0.85)'; x.beginPath(); x.moveTo(76, 108); x.lineTo(76, 134); x.lineTo(98, 121); x.closePath(); x.fill();
  x.font = '500 26px "IBM Plex Mono", monospace'; x.fillStyle = 'rgba(255,255,255,0.62)'; x.fillText(q.who, 160, 66);
  const kind = lit ? 'SAID ON CAMERA' : 'LISTENING\u2026';
  x.font = '600 20px "Host Grotesk", sans-serif';
  const kw = x.measureText(kind).width + 34;
  x.beginPath(); x.roundRect(W - 40 - kw, 40, kw, 36, 18); x.fillStyle = lit ? LIME : 'rgba(255,255,255,0.08)'; x.fill();
  x.fillStyle = lit ? INK : 'rgba(255,255,255,0.55)'; x.fillText(kind, W - 40 - kw + 17, 65);
  // the quote, brand marked only when lit
  x.font = 'italic 400 40px "Instrument Serif", "Newsreader", Georgia, serif';
  const words = [{ t: '\u201C\u2026' + q.a.split(' ')[0] }, ...q.a.split(' ').slice(1).map((t) => ({ t })),
    ...q.brand.split(' ').map((t) => ({ t, brand: true })), ...(q.b + '\u2026\u201D').split(' ').map((t) => ({ t }))];
  const lines = wrapWords(x, words, W - 160 - 40).slice(0, 4);
  lines.forEach((line, li) => {
    const y = 128 + li * 50;
    for (const w of line) {
      if (w.brand) {
        x.font = '700 34px "Host Grotesk", sans-serif';
        if (lit) { x.fillStyle = LIME; x.beginPath(); x.roundRect(160 + w.x - 6, y - 34, x.measureText(w.t).width + 12, 46, 6); x.fill(); x.fillStyle = INK; }
        else x.fillStyle = 'rgba(255,255,255,0.5)';
        x.fillText(w.t, 160 + w.x, y);
        x.font = 'italic 400 40px "Instrument Serif", "Newsreader", Georgia, serif';
      } else {
        x.fillStyle = lit ? '#f4f5ef' : 'rgba(244,245,239,0.62)';
        x.fillText(w.t, 160 + w.x, y);
      }
    }
  });
  x.font = '500 24px "IBM Plex Mono", monospace';
  x.beginPath(); x.roundRect(36, H - 84, 112, 44, 10); x.fillStyle = lit ? '#103a25' : 'rgba(255,255,255,0.06)'; x.fill();
  x.fillStyle = lit ? LIME : 'rgba(255,255,255,0.5)'; x.fillText('\u25B6 ' + q.ts, 52, H - 54);
  x.fillStyle = 'rgba(255,255,255,0.45)'; x.fillText(q.views, 170, H - 54);
  const t = new THREE.CanvasTexture(cv);
  t.colorSpace = THREE.SRGBColorSpace; t.anisotropy = 4;
  return t;
}

const CARD_VS = `varying vec2 vUv; void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`;
const CARD_FS = `uniform sampler2D uA; uniform sampler2D uB; uniform float uLit; uniform float uFade; uniform vec3 uBg; varying vec2 vUv;
void main() { vec4 a = texture2D(uA, vUv), b = texture2D(uB, vUv); vec4 c = mix(a, b, uLit); if (c.a < 0.5) discard;
  gl_FragColor = vec4(mix(uBg, c.rgb, uFade), 1.0);
  #include <colorspace_fragment>
}`;
const GLOW_FS = `uniform float uTime; uniform vec3 uBg; uniform vec3 uA; uniform vec3 uB; uniform vec3 uC; varying vec2 vUv;
void main() { vec2 p = vUv - 0.5; float t = uTime * 0.05;
  float a = smoothstep(0.42, 0.0, length(p - vec2(-0.12 + 0.05 * sin(t * 3.0), 0.1 + 0.04 * cos(t * 2.0))));
  float b = smoothstep(0.38, 0.0, length(p - vec2(0.16 + 0.05 * cos(t * 2.5), -0.12 + 0.05 * sin(t * 1.7))));
  float c = smoothstep(0.16, 0.0, length(p - vec2(0.02, -0.02)));
  vec3 col = uBg + uA * a * 0.55 + uB * b * 0.5 + uC * c * 0.18;
  gl_FragColor = vec4(col, 1.0);
  #include <colorspace_fragment>
}`;

/**
 * Start the lens on `canvas` (inside its stage element). `reduced()` is re-checked on refresh; `paused` is the
 * starting state; `quotes` are illustrative {who, a, brand, b, ts, views}. Resolves to a controller, or null when
 * three.js can't load or WebGL is missing (the stage's CSS poster stays).
 */
export async function start(canvas, { reduced = () => false, paused: startPaused = false, quotes = FALLBACK_QUOTES } = {}) {
  try { THREE = THREE || await import(THREE_URL); } catch { return null; }
  if (!canvas.isConnected) return null;
  try { await document.fonts?.ready; } catch {}
  let renderer;
  try { renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: false, powerPreference: 'high-performance' }); } catch { return null; }
  const stage = canvas.parentElement;
  const bg = new THREE.Color(BG);
  renderer.setClearColor(bg, 1);
  renderer.toneMapping = THREE.NeutralToneMapping;
  renderer.toneMappingExposure = 1.05;
  const scene = new THREE.Scene();
  scene.environment = envMap(renderer);
  const camera = new THREE.PerspectiveCamera(30, 1, 0.1, 80);
  camera.position.set(0, 0, 10);

  const glow = new THREE.Mesh(new THREE.PlaneGeometry(30, 18), new THREE.ShaderMaterial({
    vertexShader: CARD_VS, fragmentShader: GLOW_FS, toneMapped: false,
    uniforms: { uTime: { value: 0 }, uBg: { value: bg.clone() }, uA: { value: new THREE.Color('#5a3df0') }, uB: { value: new THREE.Color('#0aa3a8') }, uC: { value: new THREE.Color('#d0f854') } },
  }));
  glow.position.z = -7;
  scene.add(glow);

  const lens = new THREE.Group();
  const ring = new THREE.Mesh(ribbon(), new THREE.MeshPhysicalMaterial({
    color: 0xffffff, metalness: 0, roughness: 0.035, transmission: 1, thickness: 0.85, ior: 1.5, dispersion: 2.5,
    iridescence: 1, iridescenceIOR: 1.35, iridescenceThicknessRange: [140, 640], clearcoat: 1, clearcoatRoughness: 0.04,
    specularIntensity: 1, envMapIntensity: 1.7, attenuationColor: new THREE.Color('#e9f2ff'), attenuationDistance: 3,
  }));
  lens.add(ring);
  scene.add(lens);

  // Quote cards on belts behind (refracted) and a couple in front, wrapping left to right.
  const cards = quotes.map((q, i) => {
    const mat = new THREE.ShaderMaterial({ vertexShader: CARD_VS, fragmentShader: CARD_FS, toneMapped: false,
      uniforms: { uA: { value: cardTexture(q, false) }, uB: { value: cardTexture(q, true) }, uLit: { value: 0 }, uFade: { value: 0 }, uBg: { value: bg.clone() } } });
    const m = new THREE.Mesh(new THREE.PlaneGeometry(2.6, 2.6 * 440 / 768), mat);
    const front = i % 3 === 2;
    m.userData = { phase: i / quotes.length, y: [-1.55, 0.35, 1.6, -0.5, 1.05, -1.75, 0.0][i % 7] * 1.05, z: front ? 1.3 : -1.1 - (i % 2) * 1.1,
      speed: front ? 0.42 : 0.3 + (i % 3) * 0.04, s: front ? 0.62 : 1 - (i % 2) * 0.12, lit: 0 };
    m.scale.setScalar(m.userData.s);
    scene.add(m);
    return m;
  });

  let w = 0, h = 0, span = 8, narrow = false;
  function layout() {
    const r = canvas.getBoundingClientRect();
    w = Math.round(r.width); h = Math.round(r.height);
    if (!w || !h) return;
    narrow = w < 640;
    renderer.setPixelRatio(Math.min(devicePixelRatio || 1, narrow ? 1.5 : 1.75));
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.fov = narrow ? 38 : 30;
    camera.updateProjectionMatrix();
    const halfH = Math.tan((camera.fov * Math.PI) / 360) * camera.position.z;
    span = halfH * camera.aspect * 2 + 4;
    lens.scale.setScalar(Math.min(1.25, (halfH * 2) / 4.6) * (narrow ? 0.9 : 1));
  }

  const pointer = { x: 0, y: 0 }, eased = { x: 0, y: 0 };
  let scrollK = 0, easedScroll = 0, t = 0, last = 0, raf = 0, running = false, visible = true, paused = !!startPaused;
  const smooth = (a, b, k) => Math.min(1, Math.max(0, (k - a) / (b - a)));
  function place(dt) {
    t += dt;
    eased.x += (pointer.x - eased.x) * (1 - Math.exp(-dt * 2.5));
    eased.y += (pointer.y - eased.y) * (1 - Math.exp(-dt * 2.5));
    easedScroll += (scrollK - easedScroll) * (1 - Math.exp(-dt * 4));
    lens.rotation.set(0.25 + eased.y * 0.35 + easedScroll * 1.1, t * 0.22 + eased.x * 0.6, Math.sin(t * 0.3) * 0.08 + easedScroll * 0.3);
    lens.position.y = Math.sin(t * 0.6) * 0.06 + easedScroll * 1.2;
    camera.position.x = eased.x * 0.4; camera.position.y = -eased.y * 0.25;
    camera.lookAt(0, 0, 0);
    glow.material.uniforms.uTime.value = t;
    const lensR = 1.25 * lens.scale.x;
    for (const m of cards) {
      const d = m.userData;
      const x = (((d.phase + (t * d.speed) / span) % 1) + 1) % 1 * span - span / 2;
      m.position.set(x, d.y + Math.sin(t * 0.5 + d.phase * 6) * 0.08, d.z);
      m.rotation.set(Math.sin(t * 0.4 + d.phase * 5) * 0.05, -x * 0.04, Math.sin(t * 0.3 + d.phase * 4) * 0.03);
      const inside = 1 - smooth(lensR * 0.55, lensR * 1.05, Math.abs(x));
      d.lit += ((inside > 0.5 ? 1 : 0) - d.lit) * (1 - Math.exp(-dt * (inside > 0.5 ? 9 : 1.4)));
      m.material.uniforms.uLit.value = d.lit;
      const edge = 1 - smooth(span / 2 - 2.6, span / 2 - 0.4, Math.abs(x));
      m.material.uniforms.uFade.value = edge * (d.z > 0 ? 0.92 : 0.82 - (d.z < -1.6 ? 0.18 : 0));
    }
  }
  function frame(ms) {
    const now = ms / 1000, dt = Math.min(0.05, last ? now - last : 0.016);
    last = now;
    place(dt);
    renderer.render(scene, camera);
    raf = running ? requestAnimationFrame(frame) : 0;
  }
  function still() { place(0); renderer.render(scene, camera); }
  function run() {
    const off = reduced();
    if (w > 0) stage.classList.add('has-3d');
    const want = visible && !document.hidden && !off && !paused && w > 0;
    if (want && !running) { running = true; last = 0; raf = requestAnimationFrame(frame); }
    if (!want && running) { running = false; cancelAnimationFrame(raf); }
    if (!want && w > 0) still();
  }
  new ResizeObserver(() => { layout(); run(); }).observe(canvas);
  new IntersectionObserver(([e]) => { visible = e.isIntersecting; run(); }).observe(canvas);
  document.addEventListener('visibilitychange', run);
  addEventListener('pointermove', (e) => {
    if (reduced()) return;
    pointer.x = e.clientX / innerWidth - 0.5;
    pointer.y = e.clientY / innerHeight - 0.5;
  }, { passive: true });
  addEventListener('scroll', () => {
    if (reduced()) return;
    const r = stage.getBoundingClientRect();
    scrollK = Math.min(1, Math.max(0, -r.top / Math.max(1, r.height)));
  }, { passive: true });
  canvas.addEventListener('webglcontextlost', () => { running = false; cancelAnimationFrame(raf); stage.classList.remove('has-3d'); });

  t = 3.2;                                    // start with a card already inside the lens
  layout();
  run();
  return { setPaused(p) { paused = !!p; run(); }, paused: () => paused, refresh: run };
}
