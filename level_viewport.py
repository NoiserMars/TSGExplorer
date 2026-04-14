"""
level_viewport — OpenGL 3D level renderer for The Simpsons Game (Wii)

Shader-based pipeline (VBO + shader programs) for rendering:
  - Textured environment meshes (StrippedEnv v0 and v1)
  - Entity placement markers with type-colored labels
  - Collision mesh overlay (NEHP)
  - Navigation mesh overlay (1VAN)
  - Spline paths, cliché markers, cutscene entities
  - Skybox (6-face cubemap) and fog from GOF chunk

Vertex colors are pre-baked at load time, fragment shader does:
  textured:   fragment = texture_sample * vertex_color
  untextured: fragment = vertex_color
  + linear fog blending

Controls: WASD + mouse fly-through camera
"""
import math, ctypes, traceback
from collections import defaultdict
import numpy as np
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox,
    QPushButton, QSizePolicy, QFileDialog, QLineEdit)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QSurfaceFormat

try:
    from PySide6.QtOpenGLWidgets import QOpenGLWidget
    from OpenGL.GL import *
    from OpenGL.GLU import gluPerspective, gluProject
    HAS_GL = True
except ImportError:
    HAS_GL = False

# ─── Matrix helpers (replace gluPerspective / glRotate / glTranslate) ───

def _perspective(fov_deg, aspect, near, far):
    f = 1.0 / math.tan(math.radians(fov_deg) / 2.0)
    nf = near - far
    return np.array([
        [f / aspect, 0, 0, 0],
        [0, f, 0, 0],
        [0, 0, (far + near) / nf, 2 * far * near / nf],
        [0, 0, -1, 0]], dtype=np.float32)

def _view_matrix(cam):
    """Build view matrix from FlyCamera yaw/pitch/position."""
    ry = math.radians(-cam.yaw); rp = math.radians(-cam.pitch)
    cy, sy = math.cos(ry), math.sin(ry)
    cp, sp = math.cos(rp), math.sin(rp)
    R = np.array([
        [cy,    sp * sy,  -cp * sy, 0],
        [0,     cp,        sp,      0],
        [sy,   -sp * cy,   cp * cy, 0],
        [0,     0,         0,       1]], dtype=np.float32)
    T = np.eye(4, dtype=np.float32)
    T[0, 3] = -cam.x; T[1, 3] = -cam.y; T[2, 3] = -cam.z
    return R @ T

# ─── GLSL shaders ───

_ENV_VERT = """
#version 120
attribute vec3 a_pos;
attribute vec2 a_uv;
attribute vec4 a_color;
uniform mat4 u_mvp;
varying vec2 v_uv;
varying vec4 v_color;
varying float v_dist;
void main() {
    vec4 p = u_mvp * vec4(a_pos, 1.0);
    gl_Position = p;
    v_uv = a_uv;
    v_color = a_color;
    v_dist = p.w;
}
"""

_ENV_FRAG = """
#version 120
uniform sampler2D u_tex;
uniform int u_use_tex;
uniform vec3 u_fog_color;
uniform float u_fog_start;
uniform float u_fog_end;
uniform int u_fog_on;
varying vec2 v_uv;
varying vec4 v_color;
varying float v_dist;
void main() {
    vec4 c;
    if (u_use_tex == 1) {
        c = texture2D(u_tex, v_uv) * v_color;
    } else {
        c = v_color;
    }
    if (c.a < 0.1) discard;
    if (u_fog_on == 1) {
        float fog = clamp((u_fog_end - v_dist) / (u_fog_end - u_fog_start), 0.0, 1.0);
        c = mix(vec4(u_fog_color, 1.0), c, fog);
    }
    gl_FragColor = c;
}
"""

_OVL_VERT = """
#version 120
attribute vec3 a_pos;
attribute vec4 a_color;
uniform mat4 u_mvp;
varying vec4 v_color;
void main() {
    gl_Position = u_mvp * vec4(a_pos, 1.0);
    v_color = a_color;
}
"""

_OVL_FRAG = """
#version 120
varying vec4 v_color;
void main() { gl_FragColor = v_color; }
"""

def _compile_shader(src, stype):
    s = glCreateShader(stype)
    glShaderSource(s, src); glCompileShader(s)
    if not glGetShaderiv(s, GL_COMPILE_STATUS):
        raise RuntimeError(glGetShaderInfoLog(s).decode())
    return s

def _link_program(vsrc, fsrc):
    vs = _compile_shader(vsrc, GL_VERTEX_SHADER)
    fs = _compile_shader(fsrc, GL_FRAGMENT_SHADER)
    p = glCreateProgram(); glAttachShader(p, vs); glAttachShader(p, fs)
    glLinkProgram(p)
    if not glGetProgramiv(p, GL_LINK_STATUS):
        raise RuntimeError(glGetProgramInfoLog(p).decode())
    glDeleteShader(vs); glDeleteShader(fs)
    return p

# ─── VBO helpers ───

def _make_vbo(arr):
    buf = glGenBuffers(1)
    glBindBuffer(GL_ARRAY_BUFFER, buf)
    glBufferData(GL_ARRAY_BUFFER, arr.nbytes, arr, GL_STATIC_DRAW)
    glBindBuffer(GL_ARRAY_BUFFER, 0)
    return buf

def _del_vbo(buf):
    if buf: glDeleteBuffers(1, [buf])

# ─── Camera ───

class FlyCamera:
    """WASD fly camera. Left-drag to look around, scroll to change speed."""
    def __init__(self):
        self.x, self.y, self.z = 0, 10, 0
        self.yaw, self.pitch = 0, -20
        self.speed = 20.0
        self.sensitivity = 0.2
        self.keys = set()
    def update(self, dt):
        ry = math.radians(self.yaw)
        fx, fz = -math.sin(ry), -math.cos(ry)
        rx, rz = math.cos(ry), -math.sin(ry)
        m = self.speed * dt
        if Qt.Key_W in self.keys: self.x += fx*m; self.z += fz*m
        if Qt.Key_S in self.keys: self.x -= fx*m; self.z -= fz*m
        if Qt.Key_A in self.keys: self.x -= rx*m; self.z -= rz*m
        if Qt.Key_D in self.keys: self.x += rx*m; self.z += rz*m
        if Qt.Key_Space in self.keys: self.y += m
        if Qt.Key_Shift in self.keys: self.y -= m

ETYPE_COLORS = {
    0x0007:(1.0,1.0,0.5),   # PhysicsObject: light yellow
    0x0014:(1.0,0.3,0.0),   # AdvVolumeTrigger: orange
    0x0021:(0.4,0.4,0.8),   # PFX_Effect: blue-grey
    0x0029:(0.0,1.0,1.0),   # StartPoint: cyan
    0x002f:(0.0,1.0,1.0),   # DebugMsgTrigger: cyan
    0x0037:(0.6,0.6,0.4),   # GamesceneNode: khaki
    0x003C:(0.3,0.8,0.3),   # GamesceneSpline: green
    0x8003:(0.25,1.0,0.25), # NPC: bright green
    0x8005:(1.0,0.25,0.25), # Pickup: red
    0x8006:(1.0,0.5,0.0),   # DestructibleObj: orange
    0x8007:(0.0,1.0,1.0),   # StartPoint_Game: cyan (player spawn)
    0x8011:(1.0,0.4,0.4),   # Bunny: pink-red
    0x8012:(1.0,1.0,0.25),  # Trampoline: yellow
    0x8013:(0.5,1.0,0.8),   # Interactive: teal
    0x8015:(0.2,0.8,1.0),   # Respawn: light blue
    0x8016:(1.0,0.0,0.0),   # DeathVolume: red
    0x8017:(1.0,0.5,1.0),   # NPCSpawner: pink
    0x8021:(0.5,0.25,1.0),  # LardLad: purple
}

# ─── GL Canvas ───

if HAS_GL:
 class GLCanvas(QOpenGLWidget):
    def __init__(self, parent=None):
        fmt = QSurfaceFormat(); fmt.setDepthBufferSize(24); fmt.setSamples(4)
        fmt.setVersion(2, 1); fmt.setProfile(QSurfaceFormat.CompatibilityProfile)
        QSurfaceFormat.setDefaultFormat(fmt)
        super().__init__(parent)
        self.setFocusPolicy(Qt.StrongFocus); self.setMouseTracking(True)
        self.cam = FlyCamera()
        self._drag = False; self._last_pos = None
        self._env_prog = 0; self._ovl_prog = 0
        self._env_batches = []   # (vbo, n_verts, sec_idx, sec_name, tex_id, transparent)
        self._prop_batches = []  # (vbo, n_verts, tex_id)
        self._textures = {}
        # Overlay VBOs: (buf_id, n_verts)
        self._ent_vbo = 0; self._ent_n = 0; self._ent_raw = []; self._prop_raw = []
        self._vl_vbo = 0; self._vl_n = 0  # vol lines
        self._vf_vbo = 0; self._vf_n = 0  # vol fills
        self._nav_vbo = 0; self._nav_n = 0
        self._col_vbo = 0; self._col_n = 0; self._col_faces_vbo = 0; self._col_faces_n = 0
        self._pcol_vbo = 0; self._pcol_n = 0
        self._spl_vbo = 0; self._spl_n = 0
        self._cli_vbo = 0; self._cli_n = 0; self._cli_raw = []
        self._cli_lines_vbo = 0; self._cli_lines_n = 0
        self._cli_faces_vbo = 0; self._cli_faces_n = 0
        self._cut_vbo = 0; self._cut_n = 0; self._cut_raw = []
        self._bg = (0.08, 0.08, 0.12)
        self._fog_far = 500.0; self._fog_start = 250.0
        self._ready = False; self._level = False
        self._show_ents = True; self._show_fog = True; self._show_labels = False
        self._show_props = True; self._show_nav = False; self._show_volumes = False
        self._show_collision = False; self._show_splines = False
        self._show_cliches = False; self._show_cutscenes = True
        self._show_skybox = True
        self._sky_batches = []  # (vbo, n_verts, tex_id)
        self._section_filter = ''
        self._timer = QTimer(); self._timer.timeout.connect(self._tick); self._timer.start(16)

    def initializeGL(self):
        try:
            glClearColor(*self._bg, 1.0); glEnable(GL_DEPTH_TEST)
            glEnable(GL_BLEND); glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
            self._env_prog = _link_program(_ENV_VERT, _ENV_FRAG)
            self._ovl_prog = _link_program(_OVL_VERT, _OVL_FRAG)
            self._ready = True
        except Exception as e:
            print(f"GL init error: {e}"); traceback.print_exc()

    # ── cleanup ──
    def _cleanup(self):
        for vbo, *_ in self._env_batches: _del_vbo(vbo)
        for vbo, *_ in self._prop_batches: _del_vbo(vbo)
        for vbo, *_ in self._sky_batches: _del_vbo(vbo)
        self._env_batches = []; self._prop_batches = []; self._sky_batches = []
        for tid in self._textures.values():
            if tid: glDeleteTextures(1, [tid])
        self._textures = {}
        for a in ('_ent_vbo','_vl_vbo','_vf_vbo','_nav_vbo','_col_vbo','_col_faces_vbo','_pcol_vbo','_spl_vbo','_cli_vbo','_cli_lines_vbo','_cli_faces_vbo','_cut_vbo'):
            v = getattr(self, a, 0)
            if v: _del_vbo(v)
            setattr(self, a, 0)
        self._ent_n=self._vl_n=self._vf_n=self._nav_n=self._col_n=self._col_faces_n=self._pcol_n=self._spl_n=self._cli_n=self._cli_lines_n=self._cli_faces_n=self._cut_n=0
        self._ent_raw=[]; self._cli_raw=[]; self._cut_raw=[]; self._prop_raw=[]

    def _upload_tex(self, img):
        if img.mode != 'RGBA': img = img.convert('RGBA')
        w, h = img.size; raw = img.tobytes('raw', 'RGBA')
        tid = glGenTextures(1); glBindTexture(GL_TEXTURE_2D, tid)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_REPEAT)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_REPEAT)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR_MIPMAP_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, raw)
        glGenerateMipmap(GL_TEXTURE_2D); glBindTexture(GL_TEXTURE_2D, 0)
        return tid

    # ── Main load ──
    def load_level(self, env, materials, tex_pil_func, entities, fog=None,
                   prop_meshes=None, palette_lut=None, mat_details=None,
                   navmesh=None, dome_sections=None, start_pos=None,
                   collision=None, splines=None, cliches=None, cutscenes=None,
                   skybox_faces=None, prop_bbs=None):
        self.makeCurrent()
        if not self._ready: self.doneCurrent(); return
        self._cleanup()
        positions = env['positions']; texcoords = env['texcoords']; colors = env['colors']
        if not positions: self._level = False; self.doneCurrent(); return

        transparent_mats = set()
        if mat_details:
            for i, m in enumerate(mat_details):
                if m.get('transparent', False): transparent_mats.add(i)
        palette_mat_ids = set()
        for i, m in enumerate(materials):
            if m and 'palette' in m.lower(): palette_mat_ids.add(i)

        # Upload textures used by strips
        used = set(s['mat'] for s in env['strips'])
        for mid in used:
            if mid >= len(materials) or not materials[mid]: continue
            try:
                from PIL import ImageOps
                img = tex_pil_func(materials[mid])
                if img:
                    img = ImageOps.flip(img)  # env mesh UVs have 1.0-v, flip to compensate
                    self._textures[('env', mid)] = self._upload_tex(img)
            except: pass

        # ── Build env mesh VBOs (one per section×material) ──
        batch_data = defaultdict(list)  # (mesh_idx, mat_id) → vertex list
        for s in env['strips']:
            mi = s.get('mesh', 0); mid = s['mat']
            tid = self._textures.get(('env', mid), 0)
            is_tr = mid in transparent_mats
            for tri in s['tris']:
                for pi, ui, ci in tri:
                    px, py, pz = positions[pi] if pi < len(positions) else (0, 0, 0)
                    u, v = texcoords[ui] if ui < len(texcoords) else (0, 0)
                    vr, vg, vb = colors[ci] if ci < len(colors) else (0, 128, 20)
                    # Vertex colors encode baked lighting:
                    #   R = always 0 (unused)
                    #   G = lighting/palette value (0-255)
                    #   B = shadow/brightness modifier (typically 4-116)
                    # Combine G and B for smoother lighting with gamma correction.
                    g_norm = vg / 255.0
                    b_norm = min(1.0, vb / 32.0)  # B encodes shadow: higher = brighter
                    lum = min(1.0, 0.30 + g_norm * 0.55 + b_norm * 0.15)
                    # Apply soft gamma to reduce harsh dark/bright contrast
                    lum = lum ** 0.85
                    if tid:
                        # Textured: texture has full color, vertex = brightness only
                        cr, cg, cb = lum, lum, lum
                        # Transparent materials: slight vertex alpha for non-alpha textures,
                        # texture RGBA alpha handles GC_Alpha_Textures compositing
                        ca = 0.85 if is_tr else 1.0
                    else:
                        # Untextured: vertex G is palette index, B is brightness
                        if palette_lut is not None and 0 <= vg < 256:
                            pr = palette_lut[vg][0] / 255.0
                            pg = palette_lut[vg][1] / 255.0
                            pb = palette_lut[vg][2] / 255.0
                            bri = max(0.15, vb / 40.0)
                            cr, cg, cb = min(1.0, pr*bri), min(1.0, pg*bri), min(1.0, pb*bri)
                        else:
                            cr = cg = cb = max(0.08, vg / 200.0)
                        ca = 1.0
                    batch_data[(mi, mid)].append((px, py, pz, u, v, cr, cg, cb, ca))

        for (mi, mid), vlist in batch_data.items():
            arr = np.array(vlist, dtype=np.float32)
            vbo = _make_vbo(arr)
            sn = dome_sections[mi].get('name', '') if dome_sections and mi < len(dome_sections) else ''
            tid = self._textures.get(('env', mid), 0)
            self._env_batches.append((vbo, len(vlist), mi, sn, tid, mid in transparent_mats))

        # ── Props ──
        n_prop_textured = 0; n_prop_untextured = 0
        if prop_meshes:
            for pm in prop_meshes:
                verts = pm['verts']; tris = pm['tris']
                if not verts or not tris: continue
                uvs = pm.get('uvs', []); px, py, pz = pm['pos']
                quat = pm.get('quat'); tex_path = pm.get('tex_path')
                ptid = 0
                if tex_path and tex_pil_func:
                    tk = tex_path.lower()
                    if tk not in self._textures:
                        try:
                            img = tex_pil_func(tex_path, use_palette=True)
                            if img:
                                self._textures[tk] = self._upload_tex(img)
                            else:
                                print(f"[PROP TEX] '{pm.get('name','')}' path='{tex_path}' → img=None")
                        except Exception as _e:
                            print(f"[PROP TEX] '{pm.get('name','')}' path='{tex_path}' → ERROR: {_e}")
                    ptid = self._textures.get(tk, 0)
                elif not tex_path:
                    pname = pm.get('name', '?')
                    if pname not in ('homer', 'bart', 'lisa', 'marge'):  # skip known characters
                        print(f"[PROP TEX] '{pname}' → no tex_path")
                if ptid: n_prop_textured += 1
                else: n_prop_untextured += 1
                has_uv = len(uvs) == len(verts)
                r0, g0, b0 = pm.get('color', (0.8, 0.6, 0.3))
                # Build rotation matrix from quaternion (if valid)
                rot = None
                quat = pm.get('quat')
                if quat:
                    qx, qy, qz, qw = quat
                    # Convert from Asura Y-down to display Y-up: negate qy and qz
                    qy, qz = -qy, -qz
                    qmag = math.sqrt(qx*qx+qy*qy+qz*qz+qw*qw)
                    if 0.9 < qmag < 1.1:
                        qx/=qmag; qy/=qmag; qz/=qmag; qw/=qmag
                        rot = np.array([
                            [1-2*(qy*qy+qz*qz), 2*(qx*qy-qz*qw),   2*(qx*qz+qy*qw)],
                            [2*(qx*qy+qz*qw),   1-2*(qx*qx+qz*qz), 2*(qy*qz-qx*qw)],
                            [2*(qx*qz-qy*qw),   2*(qy*qz+qx*qw),   1-2*(qx*qx+qy*qy)]])
                pv = []
                for i0, i1, i2 in tris:
                    if max(i0, i1, i2) >= len(verts): continue
                    for vi in (i0, i1, i2):
                        vx, vy, vz = verts[vi]
                        if rot is not None:
                            rv = rot @ np.array([vx, vy, vz])
                            wx, wy, wz = rv[0]+px, rv[1]+py, rv[2]+pz
                        else:
                            wx, wy, wz = vx+px, vy+py, vz+pz
                        uu, vv = uvs[vi] if has_uv else (0, 0)
                        if ptid: pv.append((wx,wy,wz,uu,vv,1,1,1,1))
                        else: pv.append((wx,wy,wz,0,0,r0,g0,b0,1))
                if pv:
                    self._prop_batches.append((_make_vbo(np.array(pv, dtype=np.float32)), len(pv), ptid))
                    self._prop_raw.append((px, py, pz, pm.get('name', '')))
            print(f"[PROPS] {n_prop_textured} textured, {n_prop_untextured} untextured out of {len(prop_meshes)}")

        # ── Overlays ──
        self._build_overlays(entities, navmesh, collision, splines, cliches, cutscenes,
                             prop_meshes=prop_meshes, prop_bbs=prop_bbs)

        # ── Camera ──
        xs=[p[0] for p in positions]; ys=[p[1] for p in positions]; zs=[p[2] for p in positions]
        cx = (min(xs)+max(xs))/2; cy = (min(ys)+max(ys))/2; cz = (min(zs)+max(zs))/2
        span = max(max(xs)-min(xs), max(zs)-min(zs)) or 100
        if start_pos:
            self.cam.x, self.cam.y, self.cam.z = start_pos[0], start_pos[1]+3, start_pos[2]
            self.cam.pitch = -15; self.cam.yaw = 0
        else:
            self.cam.x = cx; self.cam.z = cz
            self.cam.y = max(ys)+15; self.cam.pitch = -30; self.cam.yaw = 0
        self.cam.speed = max(5, span/10)
        if fog and len(fog) >= 7:
            fr, fgc, fb = fog[0], fog[1], fog[2]; f_range = fog[5]
            self._fog_far = max(100, f_range if f_range > 50 else span*2)
            self._bg = (min(1.0, fr*0.55), min(1.0, fgc*0.58), min(1.0, fb*0.65))
        else: self._fog_far = max(300, span*2)
        self._fog_start = self._fog_far * 0.5

        # ── Skybox ──
        if skybox_faces:
            S = self._fog_far * 0.9  # skybox size = just inside far plane
            # Face quads: (name_index, 4 corners with UVs)
            # Order: top(0), front(1), right(2), back(3), left(4), bottom(5)
            face_quads = {
                0: [(-S, S,-S, 0,0), ( S, S,-S, 1,0), ( S, S, S, 1,1), (-S, S, S, 0,1)],  # top (Y+)
                1: [(-S,-S,-S, 0,1), ( S,-S,-S, 1,1), ( S, S,-S, 1,0), (-S, S,-S, 0,0)],  # front (Z-)
                2: [( S,-S,-S, 0,1), ( S,-S, S, 1,1), ( S, S, S, 1,0), ( S, S,-S, 0,0)],  # right (X+)
                3: [( S,-S, S, 0,1), (-S,-S, S, 1,1), (-S, S, S, 1,0), ( S, S, S, 0,0)],  # back (Z+)
                4: [(-S,-S, S, 0,1), (-S,-S,-S, 1,1), (-S, S,-S, 1,0), (-S, S, S, 0,0)],  # left (X-)
                5: [(-S,-S, S, 0,0), ( S,-S, S, 1,0), ( S,-S,-S, 1,1), (-S,-S,-S, 0,1)],  # bottom (Y-)
            }
            for fi, face_path in enumerate(skybox_faces):
                if not face_path or fi not in face_quads: continue
                # Upload skybox texture (tex_pil_func already handles flip)
                sky_tid = 0
                try:
                    img = tex_pil_func(face_path)
                    if img:
                        sky_tid = self._upload_tex(img)
                except: pass
                if not sky_tid: continue
                q = face_quads[fi]
                # Two triangles per face: 0-1-2, 0-2-3
                verts = []
                for idx in (0,1,2, 0,2,3):
                    x, y, z, u, v = q[idx]
                    verts.append((x, y, z, u, v, 1, 1, 1, 1))
                arr = np.array(verts, dtype=np.float32)
                self._sky_batches.append((_make_vbo(arr), 6, sky_tid))

        self._level = True; self.doneCurrent(); self.update()

    def _build_overlays(self, entities, navmesh, collision, splines, cliches, cutscenes,
                         prop_meshes=None, prop_bbs=None):
        VOL_COLORS = {0x0014:(1,0.3,0,0.35), 0x0033:(0.2,0.6,1,0.3), 0x8016:(1,0,0,0.4)}
        ev = []; vl = []; vf = []
        for e in entities:
            if 'pos' not in e: continue
            ex, ey, ez = e['pos']
            r, g, b = ETYPE_COLORS.get(e.get('type', 0), (0.5, 0.5, 0.5))
            self._ent_raw.append((ex, ey, ez, r, g, b, e.get('type_name', f"0x{e.get('type',0):04X}")))
            ev.append((ex, ey, ez, r, g, b, 1.0))
            if 'bb_min' in e and 'bb_max' in e:
                vc = VOL_COLORS.get(e['type'], (0.8, 0.8, 0, 0.25))
                vr, vg, vb, va = vc; mn, mx = e['bb_min'], e['bb_max']
                x0,y0,z0 = mn; x1,y1,z1 = mx
                C = [(x0,y0,z0),(x1,y0,z0),(x1,y1,z0),(x0,y1,z0),
                     (x0,y0,z1),(x1,y0,z1),(x1,y1,z1),(x0,y1,z1)]
                for a2,b2 in [(0,1),(1,2),(2,3),(3,0),(4,5),(5,6),(6,7),(7,4),(0,4),(1,5),(2,6),(3,7)]:
                    vl.append((*C[a2], vr, vg, vb, min(1.0, va+0.4)))
                    vl.append((*C[b2], vr, vg, vb, min(1.0, va+0.4)))
                for f in [(0,1,2,3),(4,5,6,7),(0,1,5,4),(2,3,7,6),(0,3,7,4),(1,2,6,5)]:
                    for idx in (f[0],f[1],f[2],f[0],f[2],f[3]):
                        vf.append((*C[idx], vr, vg, vb, va*0.4))
        if ev: self._ent_vbo = _make_vbo(np.array(ev, dtype=np.float32)); self._ent_n = len(ev)
        if vl: self._vl_vbo = _make_vbo(np.array(vl, dtype=np.float32)); self._vl_n = len(vl)
        if vf: self._vf_vbo = _make_vbo(np.array(vf, dtype=np.float32)); self._vf_n = len(vf)
        # NavMesh
        if navmesh and navmesh.get('vertices'):
            nv = navmesh['vertices']; conns = navmesh.get('connections', []); nv2 = []; ci = 0
            for vi, v in enumerate(nv):
                for j in range(v['n_connections']):
                    if ci >= len(conns): break
                    t = conns[ci]['target']
                    if t < len(nv):
                        nv2.append((*v['pos'], 0, 0.6, 0.8, 0.35))
                        nv2.append((*nv[t]['pos'], 0, 0.6, 0.8, 0.35))
                    ci += 1
            if nv2: self._nav_vbo = _make_vbo(np.array(nv2, dtype=np.float32)); self._nav_n = len(nv2)
        # Collision — wireframe edges + semi-transparent filled faces
        if collision and collision.get('vertices') and collision.get('faces'):
            cv = collision['vertices']; cf = collision['faces']
            cl = []; cf_filled = []
            # Color by material/surface type for better readability
            MAT_COLORS = [
                (0.1, 0.8, 0.1),  # 0: default (green)
                (0.1, 0.6, 0.9),  # 1: blue
                (0.9, 0.6, 0.1),  # 2: orange
                (0.9, 0.1, 0.4),  # 3: red
                (0.6, 0.1, 0.9),  # 4: purple
                (0.1, 0.9, 0.7),  # 5: cyan
                (0.9, 0.9, 0.1),  # 6: yellow
                (0.5, 0.8, 0.3),  # 7: lime
            ]
            for f in cf:
                i0, i1, i2 = f[0], f[1], f[2]
                if max(i0, i1, i2) >= len(cv): continue
                mat = f[3] if len(f) > 3 else 0
                mc = MAT_COLORS[mat % len(MAT_COLORS)]
                # Wireframe edges
                for a2, b2 in ((i0,i1),(i1,i2),(i2,i0)):
                    x1,y1,z1 = cv[a2]; x2,y2,z2 = cv[b2]
                    cl.append((x1, y1+0.05, z1, mc[0], mc[1], mc[2], 0.4))
                    cl.append((x2, y2+0.05, z2, mc[0], mc[1], mc[2], 0.4))
                # Filled face (very transparent)
                for idx in (i0, i1, i2):
                    x,y,z = cv[idx]
                    cf_filled.append((x, y+0.03, z, mc[0], mc[1], mc[2], 0.08))
            if cl: self._col_vbo = _make_vbo(np.array(cl, dtype=np.float32)); self._col_n = len(cl)
            if cf_filled:
                self._col_faces_vbo = _make_vbo(np.array(cf_filled, dtype=np.float32))
                self._col_faces_n = len(cf_filled)
        # Prop collision bounding boxes
        if prop_bbs and prop_meshes:
            pcl = []
            BOX_EDGES = [(0,1),(1,2),(2,3),(3,0),(4,5),(5,6),(6,7),(7,4),(0,4),(1,5),(2,6),(3,7)]
            n_matched = 0
            for pm in prop_meshes:
                name = pm.get('name', '')
                if name not in prop_bbs: continue
                n_matched += 1
                px, py, pz = pm['pos']
                rot = None
                quat = pm.get('quat')
                if quat:
                    qx, qy, qz, qw = quat
                    qy, qz = -qy, -qz
                    qm = math.sqrt(qx*qx+qy*qy+qz*qz+qw*qw)
                    if 0.9 < qm < 1.1:
                        qx/=qm; qy/=qm; qz/=qm; qw/=qm
                        rot = np.array([
                            [1-2*(qy*qy+qz*qz), 2*(qx*qy-qz*qw), 2*(qx*qz+qy*qw)],
                            [2*(qx*qy+qz*qw), 1-2*(qx*qx+qz*qz), 2*(qy*qz-qx*qw)],
                            [2*(qx*qz-qy*qw), 2*(qy*qz+qx*qw), 1-2*(qx*qx+qy*qy)]])
                for bb in prop_bbs[name]:
                    x0, x1 = bb[0], bb[3]
                    y0, y1 = -bb[4], -bb[1]
                    z0, z1 = -bb[5], -bb[2]
                    corners = [(x0,y0,z0),(x1,y0,z0),(x1,y1,z0),(x0,y1,z0),
                               (x0,y0,z1),(x1,y0,z1),(x1,y1,z1),(x0,y1,z1)]
                    tc = []
                    for cx,cy,cz in corners:
                        if rot is not None:
                            rv = rot @ np.array([cx,cy,cz])
                            tc.append((rv[0]+px, rv[1]+py, rv[2]+pz))
                        else:
                            tc.append((cx+px, cy+py, cz+pz))
                    for a2, b2 in BOX_EDGES:
                        pcl.append((*tc[a2], 0.3, 0.9, 0.3, 0.4))
                        pcl.append((*tc[b2], 0.3, 0.9, 0.3, 0.4))
            if pcl: self._pcol_vbo = _make_vbo(np.array(pcl, dtype=np.float32)); self._pcol_n = len(pcl)
            print(f"[PROP COL] {n_matched} props matched BBs, {len(pcl)//2} edges, prop_bbs={len(prop_bbs)} keys, prop_meshes={len(prop_meshes)} meshes")
        # Splines
        if splines:
            sl = []
            for sp in splines:
                pts = sp.get('points', [])
                if len(pts) < 2: continue
                r, g, b = (1, 0.8, 0) if sp['type'] == 0x003C else (0, 1, 0.8)
                for i in range(len(pts)-1):
                    sl.append((*pts[i], r, g, b, 1)); sl.append((*pts[i+1], r, g, b, 1))
            if sl: self._spl_vbo = _make_vbo(np.array(sl, dtype=np.float32)); self._spl_n = len(sl)
        # Cliches
        if cliches:
            cl2 = []; cli_lines = []; cli_faces = []
            BOX_EDGES2 = [(0,1),(1,2),(2,3),(3,0),(4,5),(5,6),(6,7),(7,4),(0,4),(1,5),(2,6),(3,7)]
            BOX_FACES = [(0,1,2,3),(4,5,6,7),(0,1,5,4),(2,3,7,6),(0,3,7,4),(1,2,6,5)]
            for c in cliches:
                p = c.get('pos')
                if not p: continue
                cl2.append((*p, 1, 0.85, 0, 1))
                lb = c.get('label', 'Cliché')
                self._cli_raw.append((*p, lb))
                # Diamond marker: 6 vertices forming an octahedron
                px, py, pz = p; ds = 0.5  # diamond half-size
                diamond = [(px,py+ds,pz),(px,py-ds,pz),(px+ds,py,pz),
                           (px-ds,py,pz),(px,py,pz+ds),(px,py,pz-ds)]
                dtris = [(0,2,4),(0,4,3),(0,3,5),(0,5,2),(1,4,2),(1,3,4),(1,5,3),(1,2,5)]
                for a,b,c2 in dtris:
                    for idx in (a,b,c2):
                        cli_faces.append((*diamond[idx], 1, 0.75, 0, 0.6))
                # If has bounding box, draw wireframe + filled faces
                if c.get('bb_min') and c.get('bb_max'):
                    mn, mx = c['bb_min'], c['bb_max']
                    x0,y0,z0 = mn; x1,y1,z1 = mx
                    corners = [(x0,y0,z0),(x1,y0,z0),(x1,y1,z0),(x0,y1,z0),
                               (x0,y0,z1),(x1,y0,z1),(x1,y1,z1),(x0,y1,z1)]
                    for a2,b2 in BOX_EDGES2:
                        cli_lines.append((*corners[a2], 1, 0.8, 0, 0.6))
                        cli_lines.append((*corners[b2], 1, 0.8, 0, 0.6))
                    for face in BOX_FACES:
                        for idx in (face[0],face[1],face[2],face[0],face[2],face[3]):
                            cli_faces.append((*corners[idx], 1, 0.8, 0, 0.08))
            if cl2: self._cli_vbo = _make_vbo(np.array(cl2, dtype=np.float32)); self._cli_n = len(cl2)
            if cli_lines:
                self._cli_lines_vbo = _make_vbo(np.array(cli_lines, dtype=np.float32))
                self._cli_lines_n = len(cli_lines)
            if cli_faces:
                self._cli_faces_vbo = _make_vbo(np.array(cli_faces, dtype=np.float32))
                self._cli_faces_n = len(cli_faces)
        # Cutscenes
        if cutscenes:
            ct = []
            for cs in cutscenes:
                px, py, pz = cs['position']; py2, pz2 = -py, -pz
                ct.append((px, py2, pz2, 0.3, 0.8, 1, 1))
                self._cut_raw.append((px, py2, pz2, cs['name'], cs['duration'],
                                      len(cs.get('actors',[])), cs.get('actors',[])))
            if ct: self._cut_vbo = _make_vbo(np.array(ct, dtype=np.float32)); self._cut_n = len(ct)

    # ── Draw helpers ──
    def _draw_env(self, vbo, nv, tid, mvp):
        p = self._env_prog; glUseProgram(p)
        glUniformMatrix4fv(glGetUniformLocation(p, 'u_mvp'), 1, GL_TRUE, mvp)
        glUniform3f(glGetUniformLocation(p, 'u_fog_color'), *self._bg)
        glUniform1f(glGetUniformLocation(p, 'u_fog_start'), self._fog_start)
        glUniform1f(glGetUniformLocation(p, 'u_fog_end'), self._fog_far)
        glUniform1i(glGetUniformLocation(p, 'u_fog_on'), 1 if self._show_fog else 0)
        if tid:
            glActiveTexture(GL_TEXTURE0); glBindTexture(GL_TEXTURE_2D, tid)
            glUniform1i(glGetUniformLocation(p, 'u_tex'), 0)
            glUniform1i(glGetUniformLocation(p, 'u_use_tex'), 1)
        else:
            glUniform1i(glGetUniformLocation(p, 'u_use_tex'), 0)
        glBindBuffer(GL_ARRAY_BUFFER, vbo)
        ST = 36  # 9 floats × 4 bytes
        a0 = glGetAttribLocation(p, 'a_pos'); a1 = glGetAttribLocation(p, 'a_uv'); a2 = glGetAttribLocation(p, 'a_color')
        glEnableVertexAttribArray(a0); glVertexAttribPointer(a0, 3, GL_FLOAT, GL_FALSE, ST, ctypes.c_void_p(0))
        if a1 >= 0: glEnableVertexAttribArray(a1); glVertexAttribPointer(a1, 2, GL_FLOAT, GL_FALSE, ST, ctypes.c_void_p(12))
        if a2 >= 0: glEnableVertexAttribArray(a2); glVertexAttribPointer(a2, 4, GL_FLOAT, GL_FALSE, ST, ctypes.c_void_p(20))
        glDrawArrays(GL_TRIANGLES, 0, nv)
        glDisableVertexAttribArray(a0)
        if a1 >= 0: glDisableVertexAttribArray(a1)
        if a2 >= 0: glDisableVertexAttribArray(a2)
        glBindBuffer(GL_ARRAY_BUFFER, 0)
        if tid: glBindTexture(GL_TEXTURE_2D, 0)

    def _draw_ovl(self, vbo, nv, mvp, mode=GL_POINTS, psz=6.0, lw=1.0):
        p = self._ovl_prog; glUseProgram(p)
        glUniformMatrix4fv(glGetUniformLocation(p, 'u_mvp'), 1, GL_TRUE, mvp)
        glBindBuffer(GL_ARRAY_BUFFER, vbo)
        ST = 28  # 7 floats × 4
        a0 = glGetAttribLocation(p, 'a_pos'); a1 = glGetAttribLocation(p, 'a_color')
        glEnableVertexAttribArray(a0); glVertexAttribPointer(a0, 3, GL_FLOAT, GL_FALSE, ST, ctypes.c_void_p(0))
        if a1 >= 0: glEnableVertexAttribArray(a1); glVertexAttribPointer(a1, 4, GL_FLOAT, GL_FALSE, ST, ctypes.c_void_p(12))
        if mode == GL_POINTS: glPointSize(psz)
        if mode == GL_LINES: glLineWidth(lw)
        glDrawArrays(mode, 0, nv)
        glDisableVertexAttribArray(a0)
        if a1 >= 0: glDisableVertexAttribArray(a1)
        glBindBuffer(GL_ARRAY_BUFFER, 0)

    # ── Paint ──
    def paintGL(self):
        glClearColor(*self._bg, 1.0); glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        if not self._level: return
        w, h = max(self.width(), 1), max(self.height(), 1)
        proj = _perspective(60, w/h, 0.5, self._fog_far*2)
        view = _view_matrix(self.cam)
        mvp = (proj @ view).astype(np.float32)
        filt = self._section_filter

        # ── Skybox (at camera position, no depth write, no fog) ──
        if self._show_skybox and self._sky_batches:
            glDisable(GL_DEPTH_TEST); glDepthMask(GL_FALSE); glDisable(GL_CULL_FACE)
            sky_view = view.copy()
            sky_view[0,3] = 0; sky_view[1,3] = 0; sky_view[2,3] = 0
            sky_mvp = (proj @ sky_view).astype(np.float32)
            for vbo, nv, tid in self._sky_batches:
                p = self._env_prog; glUseProgram(p)
                glUniformMatrix4fv(glGetUniformLocation(p, 'u_mvp'), 1, GL_TRUE, sky_mvp)
                glUniform1i(glGetUniformLocation(p, 'u_fog_on'), 0)
                glUniform1i(glGetUniformLocation(p, 'u_use_tex'), 1)
                glActiveTexture(GL_TEXTURE0); glBindTexture(GL_TEXTURE_2D, tid)
                glUniform1i(glGetUniformLocation(p, 'u_tex'), 0)
                glBindBuffer(GL_ARRAY_BUFFER, vbo)
                ST = 36
                a0 = glGetAttribLocation(p, 'a_pos'); a1 = glGetAttribLocation(p, 'a_uv'); a2 = glGetAttribLocation(p, 'a_color')
                glEnableVertexAttribArray(a0); glVertexAttribPointer(a0, 3, GL_FLOAT, GL_FALSE, ST, ctypes.c_void_p(0))
                if a1 >= 0: glEnableVertexAttribArray(a1); glVertexAttribPointer(a1, 2, GL_FLOAT, GL_FALSE, ST, ctypes.c_void_p(12))
                if a2 >= 0: glEnableVertexAttribArray(a2); glVertexAttribPointer(a2, 4, GL_FLOAT, GL_FALSE, ST, ctypes.c_void_p(20))
                glDrawArrays(GL_TRIANGLES, 0, nv)
                glDisableVertexAttribArray(a0)
                if a1 >= 0: glDisableVertexAttribArray(a1)
                if a2 >= 0: glDisableVertexAttribArray(a2)
                glBindBuffer(GL_ARRAY_BUFFER, 0); glBindTexture(GL_TEXTURE_2D, 0)
            glEnable(GL_DEPTH_TEST); glDepthMask(GL_TRUE)

        # Env opaque
        glEnable(GL_DEPTH_TEST); glDepthMask(GL_TRUE); glDisable(GL_CULL_FACE)
        for vbo, nv, si, sn, tid, tr in self._env_batches:
            if filt and filt not in sn.lower(): continue
            if not tr: self._draw_env(vbo, nv, tid, mvp)
        # Env transparent
        glDepthMask(GL_FALSE)
        for vbo, nv, si, sn, tid, tr in self._env_batches:
            if filt and filt not in sn.lower(): continue
            if tr: self._draw_env(vbo, nv, tid, mvp)
        glDepthMask(GL_TRUE)
        # Props
        if self._show_props:
            for vbo, nv, tid in self._prop_batches: self._draw_env(vbo, nv, tid, mvp)
        # Overlays
        o = self._ovl_prog
        if self._show_ents and self._ent_n:
            glDisable(GL_DEPTH_TEST)
            self._draw_ovl(self._ent_vbo, self._ent_n, mvp, GL_POINTS, 6.0)
            glEnable(GL_DEPTH_TEST)
        if self._show_volumes and self._vf_n:
            glDepthMask(GL_FALSE); self._draw_ovl(self._vf_vbo, self._vf_n, mvp, GL_TRIANGLES); glDepthMask(GL_TRUE)
        if self._show_volumes and self._vl_n:
            self._draw_ovl(self._vl_vbo, self._vl_n, mvp, GL_LINES, lw=1.5)
        if self._show_nav and self._nav_n:
            self._draw_ovl(self._nav_vbo, self._nav_n, mvp, GL_LINES, lw=1.0)
        if self._show_collision and self._col_n:
            glDepthMask(GL_FALSE); glDisable(GL_CULL_FACE)
            if self._col_faces_n:
                self._draw_ovl(self._col_faces_vbo, self._col_faces_n, mvp, GL_TRIANGLES)
            self._draw_ovl(self._col_vbo, self._col_n, mvp, GL_LINES, lw=1.0)
            glDepthMask(GL_TRUE)
        if self._show_collision and self._pcol_n:
            glDepthMask(GL_FALSE)
            self._draw_ovl(self._pcol_vbo, self._pcol_n, mvp, GL_LINES, lw=1.5)
            glDepthMask(GL_TRUE)
        if self._show_splines and self._spl_n:
            self._draw_ovl(self._spl_vbo, self._spl_n, mvp, GL_LINES, lw=2.5)
        if self._show_cliches and self._cli_n:
            glDepthMask(GL_FALSE)
            if self._cli_faces_n:
                self._draw_ovl(self._cli_faces_vbo, self._cli_faces_n, mvp, GL_TRIANGLES)
            self._draw_ovl(self._cli_vbo, self._cli_n, mvp, GL_POINTS, 14.0)
            if self._cli_lines_n:
                self._draw_ovl(self._cli_lines_vbo, self._cli_lines_n, mvp, GL_LINES, lw=2.5)
            glDepthMask(GL_TRUE)
        if self._show_cutscenes and self._cut_n:
            self._draw_ovl(self._cut_vbo, self._cut_n, mvp, GL_POINTS, 8.0)
        glUseProgram(0)

        # Labels (legacy GL for gluProject only)
        if self._show_labels: self._draw_labels()

    def _draw_labels(self):
        try:
            from PySide6.QtGui import QPainter as _QP, QColor as _QC, QFont as _QF
            w, h = self.width(), self.height()
            proj = _perspective(60, w/max(h,1), 0.5, self._fog_far*2)
            view = _view_matrix(self.cam)
            mvp = proj @ view
            def _proj(x, y, z):
                p = mvp @ np.array([x, y, z, 1.0], dtype=np.float32)
                if p[3] <= 0: return -1, -1, 2
                ndc = p[:3] / p[3]
                sx = (ndc[0] * 0.5 + 0.5) * w
                sy = (1.0 - (ndc[1] * 0.5 + 0.5)) * h
                return sx, sy, ndc[2] * 0.5 + 0.5
            _p = _QP(self); _p.setRenderHint(_QP.TextAntialiasing)
            cp = (self.cam.x, self.cam.y, self.cam.z)
            max_d2 = 10000
            if self._ent_raw:
                _p.setFont(_QF("Segoe UI", 8))
                for x,y,z,r,g,b,tn in self._ent_raw:
                    dx,dy,dz = x-cp[0],y-cp[1],z-cp[2]
                    if dx*dx+dy*dy+dz*dz > max_d2: continue
                    sx,sy,sz = _proj(x,y,z)
                    if 0<sx<w and 0<sy<h and 0<sz<1:
                        _p.setPen(_QC(220,220,220,200)); _p.drawText(int(sx+5),int(sy-2),tn)
            if self._show_props and self._prop_raw:
                _p.setFont(_QF("Segoe UI", 7)); _p.setPen(_QC(255,200,100,180))
                for x,y,z,nm in self._prop_raw:
                    dx,dy,dz = x-cp[0],y-cp[1],z-cp[2]
                    if dx*dx+dy*dy+dz*dz > max_d2: continue
                    sx,sy,sz = _proj(x,y,z)
                    if 0<sx<w and 0<sy<h and 0<sz<1: _p.drawText(int(sx+5),int(sy-2),nm)
            if self._show_cliches and self._cli_raw:
                _p.setPen(_QC(255,200,50,240)); _p.setFont(_QF("Segoe UI", 9, _QF.Bold))
                for x,y,z,lb in self._cli_raw:
                    sx,sy,sz = _proj(x,y,z)
                    if 0<sx<w and 0<sy<h and 0<sz<1: _p.drawText(int(sx+8),int(sy-4),lb)
            if self._show_cutscenes and self._cut_raw:
                _p.setPen(_QC(100,220,255,220)); _p.setFont(_QF("Segoe UI", 8))
                for x,y,z,nm,dur,na,act in self._cut_raw:
                    sx,sy,sz = _proj(x,y,z)
                    if 0<sx<w and 0<sy<h and 0<sz<1: _p.drawText(int(sx+8),int(sy-4),f"{nm} ({dur:.1f}s)")
            _p.end()
        except Exception as ex:
            import traceback; traceback.print_exc()

    def resizeGL(self, w, h): glViewport(0, 0, w, h)
    def _tick(self):
        if self.cam.keys and self._level: self.cam.update(0.016); self.update()
    def keyPressEvent(self, e): self.cam.keys.add(e.key())
    def keyReleaseEvent(self, e): self.cam.keys.discard(e.key())
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton: self._drag = True; self._last_pos = e.position()
    def mouseReleaseEvent(self, e): self._drag = False
    def mouseMoveEvent(self, e):
        if self._drag and self._last_pos:
            dx = e.position().x() - self._last_pos.x()
            dy = e.position().y() - self._last_pos.y()
            self._last_pos = e.position()
            self.cam.yaw += dx * self.cam.sensitivity
            self.cam.pitch = max(-89, min(89, self.cam.pitch - dy * self.cam.sensitivity))
            self.update()
    def wheelEvent(self, e):
        self.cam.speed *= 1.3 if e.angleDelta().y() > 0 else 1/1.3
        self.cam.speed = max(1, self.cam.speed)
    # Toggles
    def set_fog_enabled(self, on): self._show_fog = on; self.update()
    def set_entities_visible(self, on): self._show_ents = on; self.update()
    def set_props_visible(self, on): self._show_props = on; self.update()
    def set_nav_visible(self, on): self._show_nav = on; self.update()
    def set_volumes_visible(self, on): self._show_volumes = on; self.update()
    def set_collision_visible(self, on): self._show_collision = on; self.update()
    def set_splines_visible(self, on): self._show_splines = on; self.update()
    def set_cliches_visible(self, on): self._show_cliches = on; self.update()
    def set_labels_visible(self, on): self._show_labels = on; self.update()
    def set_cutscenes_visible(self, on): self._show_cutscenes = on; self.update()
    def set_skybox_visible(self, on): self._show_skybox = on; self.update()
    def set_section_filter(self, text): self._section_filter = text.lower().strip(); self.update()

# ─── Wrapper widget ───

class LevelViewport3D(QWidget):
    def __init__(self):
        super().__init__()
        lo = QVBoxLayout(self); lo.setContentsMargins(0,0,0,0); lo.setSpacing(0)
        bar = QHBoxLayout(); bar.setContentsMargins(8,4,8,4); bar.setSpacing(8)
        self.info = QLabel("No level"); self.info.setObjectName("ptitle")
        bar.addWidget(self.info); bar.addStretch()
        self.ent_cb = QCheckBox("Entities"); self.ent_cb.setChecked(True); bar.addWidget(self.ent_cb)
        self.prop_cb = QCheckBox("Props"); self.prop_cb.setChecked(True); bar.addWidget(self.prop_cb)
        self.nav_cb = QCheckBox("NavMesh"); self.nav_cb.setChecked(False); bar.addWidget(self.nav_cb)
        self.vol_cb = QCheckBox("Volumes"); self.vol_cb.setChecked(False); bar.addWidget(self.vol_cb)
        self.col_cb = QCheckBox("Collision"); self.col_cb.setChecked(False); bar.addWidget(self.col_cb)
        self.spl_cb = QCheckBox("Splines"); self.spl_cb.setChecked(False); bar.addWidget(self.spl_cb)
        self.cli_cb = QCheckBox("Clichés"); self.cli_cb.setChecked(False); bar.addWidget(self.cli_cb)
        self.cut_cb = QCheckBox("Cutscenes"); self.cut_cb.setChecked(True); bar.addWidget(self.cut_cb)
        self.fog_cb = QCheckBox("Fog"); self.fog_cb.setChecked(True); bar.addWidget(self.fog_cb)
        self.sky_cb = QCheckBox("Skybox"); self.sky_cb.setChecked(True); bar.addWidget(self.sky_cb)
        self.lbl_cb = QCheckBox("Labels"); self.lbl_cb.setChecked(False); bar.addWidget(self.lbl_cb)
        self.sec_filter = QLineEdit(); self.sec_filter.setPlaceholderText("Filter sections...")
        self.sec_filter.setFixedWidth(120); bar.addWidget(self.sec_filter)
        self.ss_btn = QPushButton("Screenshot"); self.ss_btn.setFixedWidth(70)
        self.ss_btn.clicked.connect(self._ss); bar.addWidget(self.ss_btn)
        bar.addWidget(QLabel("<span style='color:#666;font-size:10px;'>WASD+mouse · scroll=speed</span>"))
        lo.addLayout(bar)
        if HAS_GL:
            self.canvas = GLCanvas(); self.canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            lo.addWidget(self.canvas)
            self.ent_cb.toggled.connect(self.canvas.set_entities_visible)
            self.prop_cb.toggled.connect(self.canvas.set_props_visible)
            self.nav_cb.toggled.connect(self.canvas.set_nav_visible)
            self.vol_cb.toggled.connect(self.canvas.set_volumes_visible)
            self.col_cb.toggled.connect(self.canvas.set_collision_visible)
            self.spl_cb.toggled.connect(self.canvas.set_splines_visible)
            self.cli_cb.toggled.connect(self.canvas.set_cliches_visible)
            self.cut_cb.toggled.connect(self.canvas.set_cutscenes_visible)
            self.fog_cb.toggled.connect(self.canvas.set_fog_enabled)
            self.sky_cb.toggled.connect(self.canvas.set_skybox_visible)
            self.lbl_cb.toggled.connect(self.canvas.set_labels_visible)
            self.sec_filter.textChanged.connect(self.canvas.set_section_filter)
        else:
            self.canvas = None
            lo.addWidget(QLabel("<div style='text-align:center;padding:40px;color:#888;'><h2>Install PyOpenGL</h2></div>"))

    def load_level(self, env, materials, tex_pil_func, entities, fog=None,
                   prop_meshes=None, palette_lut=None, mat_details=None,
                   navmesh=None, dome_sections=None, start_pos=None,
                   collision=None, splines=None, cliches=None, cutscenes=None,
                   skybox_faces=None, prop_bbs=None):
        if not self.canvas: return
        tt = sum(len(s['tris']) for s in env['strips'])
        nt = len(set(s['mat'] for s in env['strips'] if s['mat']<len(materials) and materials[s['mat']]))
        ne = sum(1 for e in entities if 'pos' in e)
        np2 = len(prop_meshes) if prop_meshes else 0
        n_sec = len(dome_sections) if dome_sections else env.get('nMeshes', 0)
        nav_n = len(navmesh['vertices']) if navmesh and navmesh.get('vertices') else 0
        n_col = len(collision['faces']) if collision and collision.get('faces') else 0
        n_spl = len(splines) if splines else 0
        n_cli = len(cliches) if cliches else 0
        n_cut = len(cutscenes) if cutscenes else 0
        parts = [f"3D · {len(env['positions']):,}v · {tt:,}t · {nt}tex · {n_sec}sec · {ne}ent · {np2}props"]
        if n_col: parts.append(f"col:{n_col}f")
        if n_spl: parts.append(f"{n_spl}spl")
        if n_cli: parts.append(f"{n_cli}cli")
        if n_cut: parts.append(f"{n_cut}cuts")
        if nav_n: parts.append(f"nav:{nav_n}")
        self.info.setText(' · '.join(parts))
        self.canvas.load_level(env, materials, tex_pil_func, entities, fog,
                               prop_meshes, palette_lut, mat_details, navmesh,
                               dome_sections, start_pos, collision, splines, cliches, cutscenes,
                               skybox_faces)

    def _ss(self):
        if not self.canvas: return
        p, _ = QFileDialog.getSaveFileName(self, "Screenshot", "level_3d.png", "PNG (*.png)")
        if p: self.canvas.grabFramebuffer().save(p)
