// Landing hero decoration: a slowly turning drum of creator videos, inside the dark "cinema" panel beside the headline.
// A lime listening beam scans each column as it passes; now and then a video says the brand out loud, the drum slows,
// that video pops forward and shows its receipt (quote, timestamp, kind). Illustrative only: the quotes are templates
// around whatever is typed in the Brand field, and the panel says so.
//
// Demoted on purpose (reports/Interactive UI overhaul for Unprompted.md, B8): the page imports this module only after
// window 'load', on wide screens with a fine pointer, never under reduced motion (OS setting or the in-app toggle);
// three.js itself is imported inside start(), so nothing here competes with the headline for first paint. The page
// shows a visible Pause button (WCAG 2.2.2). One draw call for all tiles (instanced, drawn in the shader), no
// post-processing, stopped when off screen, paused or hidden.
const THREE_URL = 'https://cdn.jsdelivr.net/npm/three@0.186.1/+esm';
let THREE;

const COLS = 22, ROWS = 7, R = 4.1, TILE_W = 0.92, TILE_H = 1.62, PAD = 0.03, ROW_GAP = 1.86;
const FOV = 32, DIST = 15, GLOW = 0.32;
const SCAN = -0.36;                          // beam angle on the drum, just left of its front
const SPIN = (-2 * Math.PI) / 70;            // one turn every 70 s; front tiles drift toward the text
const POP_IN = 0.7, POP_HOLD = 2.8, POP_OUT = 0.9;
const PALETTE = ['#1d4c37', '#17474a', '#2c4d2e', '#3b3b2b'];
const LIME = '#d0f854', MINT = '#cfe9da', FOREST = '#042c1b';
const QUOTES = [
  ['honestly I don’t start a shoot without ', ', it’s a whole ritual'],
  ['okay, quick ', ' run before we film this'],
  ['not sponsored, I just really like ', ''],
  ['my go-to is still ', ', every single time'],
  ['so I grabbed ', ' on the way here and'],
  ['people keep asking, yes, it’s ', ''],
];

const TILE_VERT = /* glsl */`
  attribute float aSeed, aAngle, aPop, aHeard;
  uniform float uSpin, uScan;
  varying vec2 vUv;
  varying float vSeed, vPop, vHeard, vListen, vFacing;
  const float PI = 3.14159265, TAU = 6.2831853;
  void main() {
    vUv = uv; vSeed = aSeed; vPop = aPop; vHeard = aHeard;
    float d = mod(aAngle + uSpin - uScan + PI, TAU) - PI;
    vListen = 1.0 - smoothstep(0.0, 0.3, abs(d));
    vec3 p = position;
    p.xy *= 1.0 + 0.1 * aPop;
    p.z += aPop * 0.9;                      // out of the drum, toward the viewer
    vec4 world = modelMatrix * instanceMatrix * vec4(p, 1.0);
    vec3 n = normalize(mat3(modelMatrix * instanceMatrix) * vec3(0.0, 0.0, 1.0));
    vFacing = dot(n, normalize(cameraPosition - world.xyz));
    gl_Position = projectionMatrix * viewMatrix * world;
  }`;

const TILE_FRAG = /* glsl */`
  uniform float uTime;
  uniform vec3 uLime, uMint, uForest, uPal[4];
  varying vec2 vUv;
  varying float vSeed, vPop, vHeard, vListen, vFacing;
  const vec2 SIZE = vec2(${TILE_W.toFixed(3)}, ${TILE_H.toFixed(3)});
  const float PAD = ${PAD.toFixed(3)};
  float hash(float n) { return fract(sin(n) * 43758.5453); }
  float box(vec2 p, vec2 b, float r) { vec2 q = abs(p) - b + r; return length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - r; }
  float aa(float d) { return 1.0 - smoothstep(-fwidth(d), fwidth(d), d); }
  // A disc in tile units (so it stays round on a 9:16 tile), from tile coordinates t in 0..1.
  float disc(vec2 t, vec2 c, float r) { return aa(length((t - c) * SIZE) - r); }
  void main() {
    vec2 p = (vUv - 0.5) * (SIZE + 2.0 * PAD);
    float d = box(p, SIZE * 0.5, 0.09);
    if (d > fwidth(d)) discard;
    float vis = max(smoothstep(0.2, 0.9, vFacing), vPop);
    vec2 t = p / SIZE + 0.5;
    vec3 tint = uPal[int(floor(vSeed * 3.999))];
    // The "video": a ring-lit creator silhouette on a top-lit backdrop.
    vec3 col = mix(tint * 0.5, tint * 0.95, t.y);
    vec2 head = vec2(0.5 + (vSeed - 0.5) * 0.14, 0.57);
    col += tint * 0.55 * exp(-pow(length((t - head) * SIZE) / 0.34, 2.0));
    float body = max(max(disc(t, head, 0.155), aa(length((t - vec2(head.x, 0.26)) * SIZE * vec2(1.0, 1.55)) - 0.4)),
                     aa(box((t - vec2(head.x, 0.45)) * SIZE, vec2(0.06, 0.08), 0.02)));
    col = mix(col, tint * 0.32, body);
    col = mix(col, tint * 0.35, smoothstep(0.3, 0.06, t.y) * 0.75);
    col += (hash(dot(floor(t * SIZE * 140.0), vec2(1.0, 57.0)) + vSeed) - 0.5) * 0.035;
    // Handle placeholder, top left.
    col = mix(col, uMint, 0.16 * max(disc(t, vec2(0.17, 0.925), 0.05), aa(box((t - vec2(0.4, 0.925)) * SIZE, vec2(0.13, 0.012), 0.012))));
    // Waveform: still and dim until the beam listens; the middle bars are the brand, lime once heard.
    float live = max(vListen, vPop);
    float u = (t.x - 0.12) / 0.76;
    float i = floor(u * 20.0);
    float amp = 0.25 + 0.75 * hash(i * 7.31 + vSeed * 91.0);
    amp *= mix(0.45, 0.55 + 0.45 * sin(uTime * 9.0 + i * 1.7 + vSeed * 20.0), live);
    float brand = step(8.0, i) * step(i, 11.0) * max(vPop, vHeard * 0.6);
    float bar = step(0.0, u) * step(u, 1.0) * aa(abs(fract(u * 20.0) - 0.5) * SIZE.x * 0.038 - 0.009)
              * aa(abs(t.y - 0.13) * SIZE.y - amp * 0.11);
    col = mix(col, mix(uMint * mix(0.32, 0.95, live), uLime, brand), bar);
    // Progress line.
    float prog = aa(abs(t.y - 0.045) * SIZE.y - 0.006) * step(0.1, t.x) * step(t.x, 0.9);
    col = mix(col, mix(uMint * 0.25, uLime, step(t.x, 0.1 + 0.8 * fract(uTime * 0.04 + vSeed)) * live), prog);
    // Heard: a lime dot that stays on for the rest of the turn.
    col = mix(col, uLime, disc(t, vec2(0.84, 0.925), 0.04) * vHeard);
    col *= 0.72 + 0.28 * live;
    col += vec3(0.04, 0.07, 0.03) * vListen;
    col = mix(col, uLime, aa(-d - 0.012) * (1.0 - aa(-d)) * vPop);   // lime rim
    col = mix(col, col + 0.08, aa(-d - 0.008) * (1.0 - aa(-d)) * (1.0 - vPop));
    gl_FragColor = vec4(mix(uForest, col, vis), 1.0);
  }`;

// Lime halo around the popped tile; its own mesh so it draws over the neighbours instead of being cut by them.
const GLOW_FRAG = /* glsl */`
  uniform float uGlow;
  uniform vec3 uLime;
  varying vec2 vUv;
  const vec2 SIZE = vec2(${TILE_W.toFixed(3)}, ${TILE_H.toFixed(3)});
  const float GLOW = ${GLOW.toFixed(3)};
  float box(vec2 p, vec2 b, float r) { vec2 q = abs(p) - b + r; return length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - r; }
  void main() {
    float d = box((vUv - 0.5) * (SIZE + 2.0 * GLOW), SIZE * 0.5, 0.09);
    gl_FragColor = vec4(uLime, d > 0.0 ? exp(-d * 16.0) * 0.75 * uGlow : 0.0);
  }`;

const BEAM_FRAG = /* glsl */`
  uniform float uTime;
  uniform vec3 uLime;
  varying vec2 vUv;
  void main() {
    float x = (vUv.x - 0.5) * 2.0;
    float fade = smoothstep(0.0, 0.22, vUv.y) * smoothstep(1.0, 0.78, vUv.y);
    float glint = smoothstep(0.92, 1.0, sin(vUv.y * 34.0 - uTime * 5.0)) * 0.6;
    float a = (exp(-x * x * 900.0) * (0.9 + glint) + exp(-x * x * 14.0) * 0.16) * fade;
    gl_FragColor = vec4(uLime, a);
  }`;

const PLAIN_VERT = 'varying vec2 vUv; void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }';

const DUST_VERT = /* glsl */`
  attribute float aSeed;
  uniform float uTime, uDpr;
  varying float vSeed;
  void main() {
    vSeed = aSeed;
    vec3 p = position;
    p.y = mod(p.y + uTime * (0.12 + 0.22 * aSeed) + 7.0, 14.0) - 7.0;
    p.x += sin(uTime * 0.3 + aSeed * 10.0) * 0.15;
    vec4 mv = modelViewMatrix * vec4(p, 1.0);
    gl_PointSize = (1.5 + 2.5 * aSeed) * uDpr * (10.0 / -mv.z);
    gl_Position = projectionMatrix * mv;
  }`;

const DUST_FRAG = /* glsl */`
  uniform float uTime;
  uniform vec3 uLime, uMint;
  varying float vSeed;
  void main() {
    float a = smoothstep(0.5, 0.0, length(gl_PointCoord - 0.5)) * (0.18 + 0.2 * sin(uTime * 1.3 + vSeed * 40.0));
    gl_FragColor = vec4(vSeed > 0.9 ? uLime : uMint, max(a, 0.0));
  }`;

const easeOutBack = (k) => 1 + 2.2 * (k - 1) ** 3 + 1.2 * (k - 1) ** 2;
const easeInOut = (k) => (k < 0.5 ? 2 * k * k : 1 - (-2 * k + 2) ** 2 / 2);
const wrap = (a) => ((((a + Math.PI) % (2 * Math.PI)) + 2 * Math.PI) % (2 * Math.PI)) - Math.PI;

function popAt(age) {
  if (age < POP_IN) return easeOutBack(age / POP_IN);
  if (age < POP_IN + POP_HOLD) return 1;
  const k = (age - POP_IN - POP_HOLD) / POP_OUT;
  return k >= 1 ? 0 : 1 - easeInOut(k);
}

/**
 * Start the drum on `canvas` (inside the hero's cinema panel). `brandInput` is the Brand field the quotes use,
 * `receipt` is the DOM card that follows a popped tile, `reduced()` says whether motion should stay off (checked on
 * every refresh), `paused` is the starting pause state. Resolves to a controller, or null when motion is off, three.js
 * can't load or WebGL isn't available (the panel's static poster stays).
 */
export async function start(canvas, { brandInput, receipt, reduced = () => false, paused: startPaused = false }) {
  if (reduced()) return null;
  try { THREE = THREE || await import(THREE_URL); } catch { return null; }
  if (reduced() || !canvas.isConnected) return null;
  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true, powerPreference: 'low-power' });
  } catch {
    return null;
  }
  const stage = canvas.parentElement;
  renderer.setClearColor(0x000000, 0);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(FOV, 1, 0.1, 60);
  camera.position.set(0, 0.4, DIST);

  const drum = new THREE.Group();            // placed and scaled per layout
  const spin = new THREE.Group();
  drum.add(spin);
  scene.add(drum);

  const color = (hex) => new THREE.Color().setStyle(hex, THREE.LinearSRGBColorSpace);   // raw sRGB: shaders write it as-is
  const uniforms = {
    uTime: { value: 0 }, uSpin: { value: 0 }, uScan: { value: SCAN }, uDpr: { value: 1 },
    uLime: { value: color(LIME) }, uMint: { value: color(MINT) }, uForest: { value: color(FOREST) },
    uPal: { value: PALETTE.map(color) },
  };

  // Tiles: one instanced plane per video, facing outward around the drum, odd columns staggered half a row.
  const n = COLS * ROWS;
  const geo = new THREE.PlaneGeometry(TILE_W + 2 * PAD, TILE_H + 2 * PAD);
  const seeds = new Float32Array(n), angles = new Float32Array(n), pops = new Float32Array(n), heard = new Float32Array(n);
  const rowY = (c, r) => (r - (ROWS - 1) / 2) * ROW_GAP + (c % 2) * ROW_GAP * 0.5;
  const tiles = new THREE.InstancedMesh(geo, new THREE.ShaderMaterial({ uniforms, vertexShader: TILE_VERT, fragmentShader: TILE_FRAG }), n);
  const m = new THREE.Object3D();
  for (let c = 0; c < COLS; c++) {
    const a = (c / COLS) * Math.PI * 2;
    for (let r = 0; r < ROWS; r++) {
      const i = c * ROWS + r;
      m.position.set(R * Math.sin(a), rowY(c, r), R * Math.cos(a));
      m.rotation.set(0, a, 0);
      m.updateMatrix();
      tiles.setMatrixAt(i, m.matrix);
      seeds[i] = (Math.sin(i * 12.9898) * 43758.5453) % 1 * 0.5 + 0.5;
      angles[i] = a;
    }
  }
  const popAttr = new THREE.InstancedBufferAttribute(pops, 1).setUsage(THREE.DynamicDrawUsage);
  const heardAttr = new THREE.InstancedBufferAttribute(heard, 1).setUsage(THREE.DynamicDrawUsage);
  geo.setAttribute('aSeed', new THREE.InstancedBufferAttribute(seeds, 1));
  geo.setAttribute('aAngle', new THREE.InstancedBufferAttribute(angles, 1));
  geo.setAttribute('aPop', popAttr);
  geo.setAttribute('aHeard', heardAttr);
  tiles.frustumCulled = false;
  spin.add(tiles);

  // The listening beam: fixed on the drum's surface while the tiles turn under it.
  const beam = new THREE.Mesh(new THREE.PlaneGeometry(1.1, ROWS * ROW_GAP + 3), new THREE.ShaderMaterial({
    uniforms, vertexShader: PLAIN_VERT, fragmentShader: BEAM_FRAG, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
  }));
  beam.position.set((R + 0.03) * Math.sin(SCAN), 0, (R + 0.03) * Math.cos(SCAN));
  beam.rotation.y = SCAN;
  beam.renderOrder = 2;
  drum.add(beam);

  const glowUniforms = { uGlow: { value: 0 }, uLime: uniforms.uLime };
  const glow = new THREE.Mesh(new THREE.PlaneGeometry(TILE_W + 2 * GLOW, TILE_H + 2 * GLOW), new THREE.ShaderMaterial({
    uniforms: glowUniforms, vertexShader: PLAIN_VERT, fragmentShader: GLOW_FRAG, transparent: true, depthWrite: false,
    depthTest: false, blending: THREE.AdditiveBlending,
  }));
  glow.matrixAutoUpdate = false;
  glow.renderOrder = 1;
  glow.visible = false;
  scene.add(glow);

  // Dust drifting up through the projector light, mostly in front of the drum.
  const dustN = 160, dust = new Float32Array(dustN * 3), dustSeed = new Float32Array(dustN);
  for (let i = 0; i < dustN; i++) {
    const a = -1.7 + Math.random() * 3, rr = R + 0.4 + Math.random() * 3.2;
    dust.set([rr * Math.sin(a), -7 + Math.random() * 14, rr * Math.cos(a)], i * 3);
    dustSeed[i] = Math.random();
  }
  const dustGeo = new THREE.BufferGeometry();
  dustGeo.setAttribute('position', new THREE.BufferAttribute(dust, 3));
  dustGeo.setAttribute('aSeed', new THREE.BufferAttribute(dustSeed, 1));
  const dustPts = new THREE.Points(dustGeo, new THREE.ShaderMaterial({
    uniforms, vertexShader: DUST_VERT, fragmentShader: DUST_FRAG, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
  }));
  dustPts.frustumCulled = false;
  dustPts.renderOrder = 3;
  drum.add(dustPts);

  // ---- layout: the drum fills its panel, centred. A panel too small to read stays on the static poster.
  let w = 0, h = 0, narrow = true;
  const tilt = { x: 0.06, z: -0.1 }, pointer = { x: 0, y: 0 }, eased = { x: 0, y: 0 };
  function layout() {
    w = canvas.clientWidth; h = canvas.clientHeight;
    if (!w || !h) return;
    renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 1.75));
    renderer.setSize(w, h, false);
    uniforms.uDpr.value = renderer.getPixelRatio();
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    narrow = w < 240 || h < 240;
    stage.classList.toggle('has-3d', !narrow && !reduced());
    drum.scale.setScalar(Math.min(1, Math.max(0.6, camera.aspect * 0.92)));
    drum.position.set(0, 0.1, 0);
    if (narrow) receipt.classList.remove('on');
  }

  // ---- hits: when a column crosses the beam, one of its videos may "say" the brand.
  const v = new THREE.Vector3(), tileWorld = new THREE.Matrix4();
  const tileMatrix = (i, out) => out.fromArray(tiles.instanceMatrix.array, i * 16).premultiply(tiles.matrixWorld);
  function project(c, r, x, y, z) {             // a point on tile (c, r), in tile units, to canvas pixels
    v.set(x, y, z).applyMatrix4(tileMatrix(c * ROWS + r, tileWorld)).project(camera);
    return { x: (v.x + 1) / 2 * w, y: (1 - v.y) / 2 * h };
  }
  function eligibleRows(c) {
    const rows = [];
    for (let r = 0; r < ROWS; r++) {
      const y = project(c, r, 0, 0, 0).y / h;
      if (y > 0.22 && y < 0.6) rows.push(r);   // the middle of the panel, clear of the caption and the Pause button
    }
    return rows;
  }

  let hit = null;                             // { i, c, r, t0 }
  const mark = receipt.querySelector('mark'), quoteA = receipt.querySelector('.q-a'), quoteB = receipt.querySelector('.q-b');
  const meta = receipt.querySelector('.hero-receipt-meta');
  const brandText = () => {
    const b = brandInput.value.trim();
    return !b ? 'your brand' : b.length > 28 ? b.slice(0, 27) + '…' : b;
  };
  function trigger(c, now) {
    const rows = eligibleRows(c);
    if (!rows.length) return;
    const r = rows[Math.floor(Math.random() * rows.length)];
    hit = { i: c * ROWS + r, c, r, t0: now };
    const [a, b] = QUOTES[Math.floor(Math.random() * QUOTES.length)];
    quoteA.textContent = `“…${a}`;
    quoteB.textContent = `${b}…”`;
    mark.textContent = brandText();
    meta.textContent = `${Math.floor(Math.random() * 2)}:${String(5 + Math.floor(Math.random() * 54)).padStart(2, '0')} · On camera only`;
  }
  const lift = new THREE.Matrix4();
  function placeGlow(i) {
    const k = pops[i];
    glow.visible = k > 0.01;
    glowUniforms.uGlow.value = Math.min(k, 1);
    glow.matrix.copy(tileMatrix(i, tileWorld)).multiply(lift.makeTranslation(0, 0, k * 0.9 - 0.01))
      .multiply(lift.makeScale(1 + 0.1 * k, 1 + 0.1 * k, 1));
  }
  function placeReceipt() {
    const right = project(hit.c, hit.r, TILE_W / 2 + 0.05, TILE_H / 2 - 0.1, pops[hit.i] * 0.9);
    const left = project(hit.c, hit.r, -TILE_W / 2 - 0.05, TILE_H / 2 - 0.1, pops[hit.i] * 0.9);
    const rw = receipt.offsetWidth, rh = receipt.offsetHeight;
    // Right of the video; left of it when the right runs out of panel; else hug the nearer edge.
    let x = right.x + 12;
    if (x + rw > w - 12) x = left.x - 12 - rw >= 12 ? left.x - 12 - rw : Math.max(12, w - 12 - rw);
    const y = Math.min(Math.max(right.y, 12), h - rh - 56);
    receipt.style.transform = `translate3d(${Math.round(x)}px, ${Math.round(y)}px, 0)`;
  }

  // ---- loop
  let angle = SCAN + 0.09, speed = SPIN, last = 0, raf = 0, visible = true, running = false, paused = !!startPaused;
  const prevD = Array.from({ length: COLS }, (_, c) => wrap((c / COLS) * Math.PI * 2 + angle - SCAN));
  let first = true;
  function frame(nowMs) {
    const now = nowMs / 1000, dt = Math.min(0.05, last ? now - last : 0.016);
    last = now;
    uniforms.uTime.value = now;
    const age = hit ? now - hit.t0 : Infinity;
    speed += ((age < POP_IN + POP_HOLD ? SPIN * 0.18 : SPIN) - speed) * (1 - Math.exp(-dt * 2.5));
    angle += speed * dt;
    spin.rotation.y = angle;
    uniforms.uSpin.value = angle;

    eased.x += (pointer.x - eased.x) * (1 - Math.exp(-dt * 3));
    eased.y += (pointer.y - eased.y) * (1 - Math.exp(-dt * 3));
    drum.rotation.set(tilt.x + eased.y * 0.05, eased.x * 0.1, tilt.z);
    camera.position.x = eased.x * 0.35;
    camera.lookAt(0, 0.2, 0);
    scene.updateMatrixWorld();

    for (let c = 0; c < COLS; c++) {
      const d = wrap((c / COLS) * Math.PI * 2 + angle - SCAN);
      if (prevD[c] > 0 && d <= 0 && prevD[c] < 1 && !(age < POP_IN + POP_HOLD + POP_OUT) && (first || Math.random() < 0.85)) {
        first = false;
        trigger(c, now);
      }
      prevD[c] = d;
    }
    if (hit) {
      const a = now - hit.t0;
      pops[hit.i] = popAt(a);
      heard[hit.i] = 1;
      popAttr.needsUpdate = heardAttr.needsUpdate = true;
      placeGlow(hit.i);
      const show = a > 0.25 && a < POP_IN + POP_HOLD - 0.1;
      receipt.classList.toggle('on', show);
      if (show) {
        if (mark.textContent !== brandText()) mark.textContent = brandText();
        placeReceipt();
      }
      if (a > POP_IN + POP_HOLD + POP_OUT) { pops[hit.i] = 0; glow.visible = false; hit = null; }
    }
    for (let i = 0; i < n; i++) if (heard[i] && (!hit || hit.i !== i)) { heard[i] = Math.max(0, heard[i] - dt / 55); heardAttr.needsUpdate = true; }

    renderer.render(scene, camera);
    raf = running ? requestAnimationFrame(frame) : 0;
  }

  // One still frame: shown while paused, so the panel never goes blank.
  function still() {
    spin.rotation.y = uniforms.uSpin.value = angle;
    if (!running && !last) drum.rotation.set(tilt.x, 0, tilt.z);
    camera.lookAt(0, 0.2, 0);
    scene.updateMatrixWorld();
    renderer.render(scene, camera);
  }

  function run() {
    const off = reduced();
    if (off) {                                // motion turned off while running: stop, drop the drum, keep the poster
      hit = null; pops.fill(0); popAttr.needsUpdate = true; glow.visible = false; receipt.classList.remove('on');
      stage.classList.remove('has-3d');
    } else if (w > 0 && !narrow) stage.classList.add('has-3d');
    const want = visible && !document.hidden && !off && !paused && w > 0 && !narrow;
    if (want && !running) { running = true; last = 0; raf = requestAnimationFrame(frame); }
    if (!want && running) { running = false; cancelAnimationFrame(raf); }
    if (!want && !off && w > 0 && !narrow) still();
  }

  new ResizeObserver(() => { layout(); run(); }).observe(canvas);
  new IntersectionObserver(([e]) => { visible = e.isIntersecting; run(); }).observe(canvas);
  document.addEventListener('visibilitychange', run);
  addEventListener('pointermove', (e) => {
    if (!running) return;
    pointer.x = e.clientX / innerWidth - 0.5;
    pointer.y = e.clientY / innerHeight - 0.5;
  }, { passive: true });
  brandInput.addEventListener('input', () => { if (hit && !running) mark.textContent = brandText(); });
  canvas.addEventListener('webglcontextlost', () => { running = false; cancelAnimationFrame(raf); stage.classList.remove('has-3d'); receipt.classList.remove('on'); });

  layout();
  run();
  return {
    setPaused(p) { paused = !!p; run(); },
    paused: () => paused,
    refresh: run,
  };
}
