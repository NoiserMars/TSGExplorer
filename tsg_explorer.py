#!/usr/bin/env python3
"""
TSGExplorer — The Simpsons Game graphical asset viewer

PySide6 application for browsing Asura engine containers: textures, 3D models,
audio, dialogue, scripts, animations, and full level visualization.

Requires: PySide6, Pillow, numpy, PyOpenGL (optional, for 3D views)
"""
VERSION = "2026.04.18"
APP_NAME = "TSGExplorer"
import sys, os, struct, io, wave, array, tempfile, math
from collections import defaultdict
from PySide6.QtWidgets import *
from PySide6.QtCore import Qt, QTimer, QUrl, QSize, QPointF, QEvent, Signal
from PySide6.QtGui import *
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tsg_oldgen as asura
try:
    import tsg_newgen as ng
    import tsg_game_data as gd
    HAS_NEWGEN = True
except ImportError:
    HAS_NEWGEN = False
try:
    from level_viewport import LevelViewport3D, HAS_GL
except ImportError:
    HAS_GL = False
    LevelViewport3D = None

try:
    from PySide6.QtOpenGLWidgets import QOpenGLWidget as _QOpenGLWidget
    from OpenGL.GL import *
    from OpenGL.GLU import gluPerspective, gluLookAt
    _HAS_MODEL_GL = True
except ImportError:
    _HAS_MODEL_GL = False

DARK_STYLE = """
QMainWindow, QWidget { background: #1a1a1e; color: #ccc; }
QMenuBar { background: #222228; color: #ccc; border-bottom: 1px solid #333; }
QMenuBar::item:selected { background: #e0a030; color: #111; }
QMenu { background: #222228; color: #ccc; border: 1px solid #444; }
QMenu::item:selected { background: #e0a030; color: #111; }
QStatusBar { background: #1a1a1e; color: #888; border-top: 1px solid #333; font-size: 11px; }
QSplitter::handle { background: #444; width: 4px; }
QSplitter::handle:hover { background: #e0a030; }
QTreeWidget { background: #16161a; color: #ccc; border: none; font-size: 12px; }
QTreeWidget::item:hover { background: #252530; }
QTreeWidget::item:selected { background: #e0a030; color: #111; }
QHeaderView::section { background: #222228; color: #aaa; border: 1px solid #333; padding: 3px; font-size: 11px; }
QTableWidget { background: #16161a; color: #ccc; gridline-color: #333; }
QTableWidget::item:selected { background: #e0a030; color: #111; }
QScrollBar:vertical { background: #1a1a1e; width: 10px; border: none; }
QScrollBar::handle:vertical { background: #444; border-radius: 4px; min-height: 20px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: #1a1a1e; height: 10px; border: none; }
QScrollBar::handle:horizontal { background: #444; border-radius: 4px; min-width: 20px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QLineEdit { background: #222228; color: #ccc; border: 1px solid #444; border-radius: 3px; padding: 4px 8px; }
QLineEdit:focus { border-color: #e0a030; }
QPushButton { background: #2a2a32; color: #ccc; border: 1px solid #444; border-radius: 3px; padding: 4px 12px; font-size: 11px; }
QPushButton:hover { background: #333340; border-color: #e0a030; }
QPushButton:pressed { background: #e0a030; color: #111; }
QCheckBox { color: #ccc; }
QCheckBox::indicator:checked { background: #e0a030; border: 1px solid #e0a030; border-radius: 2px; }
QCheckBox::indicator:unchecked { background: #333; border: 1px solid #555; border-radius: 2px; }
QLabel#ptitle { color: #e0a030; font-size: 13px; font-weight: bold; }
"""

class TexturePanel(QWidget):
    """Texture viewer with info bar, palette toggle, alpha toggle."""
    def __init__(self):
        super().__init__()
        lo = QVBoxLayout(self); lo.setContentsMargins(0,0,0,0); lo.setSpacing(0)
        # Info/controls bar
        bar = QHBoxLayout(); bar.setContentsMargins(8,4,8,4); bar.setSpacing(8)
        self.info = QLabel("No texture"); self.info.setObjectName("ptitle"); bar.addWidget(self.info)
        bar.addStretch()
        self.pal_cb = QCheckBox("Palette"); self.pal_cb.setChecked(True); self.pal_cb.toggled.connect(self._refresh); bar.addWidget(self.pal_cb)
        self.alpha_cb = QCheckBox("Alpha"); self.alpha_cb.setChecked(True); self.alpha_cb.toggled.connect(self._refresh); bar.addWidget(self.alpha_cb)
        self.export_btn = QPushButton("Export PNG"); self.export_btn.setFixedWidth(90); bar.addWidget(self.export_btn)
        lo.addLayout(bar)
        # Viewer
        self.view = TextureView(); lo.addWidget(self.view)
        self._file = None; self._owner = None
    def set_owner(self, owner): self._owner = owner
    def load_texture(self, f, owner):
        self._file = f; self._owner = owner
        fmt_names = {0:'I4',1:'I8',2:'IA4',3:'IA8',4:'RGB565',5:'RGB5A3',6:'RGBA8',14:'CMPR'}
        import tsg_oldgen as _a
        imgs = _a.parse_tpl(f['data'])
        if imgs:
            i0 = imgs[0]; fn = fmt_names.get(i0['fmt'], f"?({i0['fmt']})")
            extra = f"+{fmt_names.get(imgs[1]['fmt'],'?')}" if len(imgs)>=2 else ""
            self.info.setText(f"{i0['w']}×{i0['h']}  ·  {fn}{extra}  ·  {len(f['data']):,} bytes")
            self.pal_cb.setEnabled(i0['fmt'] == 1)
        self._refresh()
    def _refresh(self):
        if not self._file or not self._owner: return
        img = self._owner._full_tex_to_pil(self._file, use_palette=self.pal_cb.isChecked(), use_alpha=self.alpha_cb.isChecked())
        if not img: return
        if img.mode != 'RGBA': img = img.convert('RGBA')
        raw = img.tobytes('raw','RGBA')
        from PySide6.QtGui import QImage
        qi = QImage(raw, img.width, img.height, img.width*4, QImage.Format_RGBA8888).copy()
        self.view.set_image(qi)

class TextureView(QGraphicsView):
    def __init__(self):
        super().__init__()
        self._scene = QGraphicsScene(); self.setScene(self._scene)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setRenderHint(QPainter.SmoothPixmapTransform, False)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        tile = QPixmap(16, 16); p = QPainter(tile)
        p.fillRect(0,0,16,16,QColor(0x28,0x28,0x2c))
        p.fillRect(0,0,8,8,QColor(0x1e,0x1e,0x22)); p.fillRect(8,8,8,8,QColor(0x1e,0x1e,0x22)); p.end()
        self.setBackgroundBrush(QBrush(tile))
        self._qimg = None
    def set_image(self, qimg):
        self._scene.clear(); self._qimg = qimg
        self._scene.addPixmap(QPixmap.fromImage(qimg))
        self._scene.setSceneRect(QPixmap.fromImage(qimg).rect().toRectF())
        self.resetTransform(); self.fitInView(self._scene.sceneRect(), Qt.KeepAspectRatio)
    def wheelEvent(self, e):
        f = 1.15 if e.angleDelta().y() > 0 else 1/1.15; self.scale(f, f)

class HexView(QTextEdit):
    def __init__(self):
        super().__init__(); self.setReadOnly(True); self.setFont(QFont("Consolas",10))
        self.setStyleSheet("background:#111114;color:#88cc88;border:none;"); self.setLineWrapMode(QTextEdit.NoWrap)
    def set_data(self, data, mx=8192):
        lines = []
        for i in range(0, min(len(data),mx), 16):
            row = data[i:i+16]; hx = ' '.join(f'{b:02x}' for b in row).ljust(47)
            asc = ''.join(chr(b) if 32<=b<127 else '.' for b in row)
            lines.append(f'{i:08x}  {hx}  {asc}')
        if len(data)>mx: lines.append(f'\n... ({len(data):,} bytes total)')
        self.setPlainText('\n'.join(lines))

class WaveformWidget(QWidget):
    """Custom waveform drawing widget that respects layout constraints."""
    def __init__(self):
        super().__init__()
        self._pcm = None
        self.setMinimumHeight(60)
        self.setMaximumHeight(140)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setStyleSheet("background:#111114; border:1px solid #333; border-radius:3px;")
    def set_pcm(self, pcm): self._pcm = pcm; self.update()
    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._pcm: return
        w, h = self.width(), self.height()
        if w < 10 or h < 10: return
        pa = QPainter(self); pa.setPen(QPen(QColor(0xe0,0xa0,0x30), 1))
        n = len(self._pcm); mid = h // 2; step = max(1, n // w)
        for x in range(min(w, n // step)):
            c = self._pcm[x*step:(x+1)*step]
            if c: pa.drawLine(x, mid-int(max(c)/32768*mid), x, mid-int(min(c)/32768*mid))
        pa.setPen(QPen(QColor(0x33,0x33,0x33), 1)); pa.drawLine(0, mid, w, mid); pa.end()

class AudioPlayer(QWidget):
    def __init__(self):
        super().__init__()
        lo = QVBoxLayout(self); lo.setContentsMargins(8,8,8,8); lo.setSpacing(6)
        self.info = QLabel("No audio"); self.info.setObjectName("ptitle"); lo.addWidget(self.info)
        self.wf = WaveformWidget(); lo.addWidget(self.wf)
        ct = QHBoxLayout(); ct.setSpacing(6)
        self.play_btn = QPushButton("Play"); self.play_btn.setFixedWidth(70); self.play_btn.clicked.connect(self._toggle); ct.addWidget(self.play_btn)
        self.stop_btn = QPushButton("Stop"); self.stop_btn.setFixedWidth(60); self.stop_btn.clicked.connect(self._stop); ct.addWidget(self.stop_btn)
        ct.addWidget(QLabel("Vol:")); self.vol_sl = QSlider(Qt.Horizontal); self.vol_sl.setRange(0,100); self.vol_sl.setValue(80)
        self.vol_sl.setFixedWidth(80); self.vol_sl.valueChanged.connect(self._set_vol); ct.addWidget(self.vol_sl)
        self.exp_btn = QPushButton("Export WAV"); self.exp_btn.setFixedWidth(90); self.exp_btn.clicked.connect(self._export); ct.addWidget(self.exp_btn)
        ct.addStretch(); lo.addLayout(ct)
        # Seek bar
        self.seek_sl = QSlider(Qt.Horizontal); self.seek_sl.setRange(0,1000); self.seek_sl.setEnabled(False)
        self.seek_sl.sliderMoved.connect(self._seek); lo.addWidget(self.seek_sl)
        lo.addStretch()
        self._wav=None; self._sr=0; self._pcm=None; self._player=None; self._ao=None; self._tf=None
    def load_dsp(self, data, name=""):
        # Stop any current playback and clear temp file
        if self._player: self._player.stop()
        if self._tf:
            try: os.unlink(self._tf.name)
            except: pass
            self._tf = None
        self.play_btn.setText("Play"); self.seek_sl.setValue(0); self.seek_sl.setEnabled(False)
        r = asura._decode_dsp_adpcm(data)
        if not r: self.info.setText(f"Decode failed: {name}"); self._wav=None; return
        self._wav, self._sr = r
        wf = wave.open(io.BytesIO(self._wav),'r'); frames = wf.readframes(wf.getnframes())
        self._pcm = array.array('h'); self._pcm.frombytes(frames); wf.close()
        self.info.setText(f"{name}  ·  {self._sr} Hz  ·  {len(self._pcm)/self._sr:.2f}s  ·  {len(self._pcm):,} samples")
        self.wf.set_pcm(self._pcm)
    def load_wav_bytes(self, wav_data, name=""):
        """Load audio from raw WAV bytes (e.g. from BIK decode)."""
        if self._player: self._player.stop()
        if self._tf:
            try: os.unlink(self._tf.name)
            except: pass
            self._tf = None
        self.play_btn.setText("Play"); self.seek_sl.setValue(0); self.seek_sl.setEnabled(False)
        if not wav_data: self.info.setText(f"Decode failed: {name}"); self._wav=None; return
        self._wav = wav_data
        try:
            wf = wave.open(io.BytesIO(self._wav),'r')
            self._sr = wf.getframerate()
            nch = wf.getnchannels()
            frames = wf.readframes(wf.getnframes())
            wf.close()
            self._pcm = array.array('h'); self._pcm.frombytes(frames)
            # For stereo, show left channel samples only for waveform
            dur = len(self._pcm) / (self._sr * nch) if self._sr else 0
            ch_label = "stereo" if nch > 1 else "mono"
            self.info.setText(f"{name}  ·  {self._sr} Hz {ch_label}  ·  {dur:.2f}s")
            self.wf.set_pcm(self._pcm)
        except Exception as e:
            self.info.setText(f"WAV parse error: {e}"); self._wav=None
    def _toggle(self):
        if not self._wav: return
        if self._player and self._player.playbackState()==QMediaPlayer.PlayingState: self._player.pause(); self.play_btn.setText("Play"); return
        if not self._tf: self._tf=tempfile.NamedTemporaryFile(suffix='.wav',delete=False); self._tf.write(self._wav); self._tf.flush()
        if not self._player:
            self._ao=QAudioOutput(); self._ao.setVolume(self.vol_sl.value()/100.0)
            self._player=QMediaPlayer(); self._player.setAudioOutput(self._ao)
            self._player.positionChanged.connect(self._pos_changed)
            self._player.durationChanged.connect(lambda d: self.seek_sl.setEnabled(d > 0))
        self._player.setSource(QUrl.fromLocalFile(self._tf.name)); self._player.play(); self.play_btn.setText("Pause"); self.seek_sl.setEnabled(True)
    def _stop(self):
        if self._player: self._player.stop(); self.play_btn.setText("Play"); self.seek_sl.setValue(0)
    def _set_vol(self, v):
        if self._ao: self._ao.setVolume(v / 100.0)
    def _seek(self, pos):
        if self._player and self._player.duration() > 0:
            self._player.setPosition(int(pos * self._player.duration() / 1000))
    def _pos_changed(self, pos):
        if self._player and self._player.duration() > 0 and not self.seek_sl.isSliderDown():
            self.seek_sl.setValue(int(pos * 1000 / self._player.duration()))
    def _export(self):
        if not self._wav: return
        p,_=QFileDialog.getSaveFileName(self,"Export WAV","","WAV (*.wav)")
        if p: open(p,'wb').write(self._wav)

class ModelViewer(QWidget):
    def __init__(self):
        super().__init__(); lo=QVBoxLayout(self); lo.setContentsMargins(0,0,0,0); lo.setSpacing(0)
        bar=QHBoxLayout(); bar.setContentsMargins(8,4,8,4)
        self.info=QLabel("No model"); self.info.setObjectName("ptitle"); bar.addWidget(self.info); bar.addStretch()
        self.tex_cb=QCheckBox("Textured"); self.tex_cb.setChecked(True); bar.addWidget(self.tex_cb)
        self.wire_cb=QCheckBox("Wire"); bar.addWidget(self.wire_cb)
        self.smooth_cb=QCheckBox("Smooth"); self.smooth_cb.setChecked(True); bar.addWidget(self.smooth_cb)
        self.ss_btn=QPushButton("Screenshot"); self.ss_btn.setFixedWidth(90); self.ss_btn.clicked.connect(self._screenshot); bar.addWidget(self.ss_btn)
        self.exp_btn=QPushButton("Export OBJ"); self.exp_btn.setFixedWidth(90); self.exp_btn.clicked.connect(self._export); bar.addWidget(self.exp_btn)
        lo.addLayout(bar)
        self._v=[]; self._uv=[]; self._t=[]; self._nrm=[]; self._mn=""; self._md=None; self._cv=2
        self._pil_tex=None; self._gl_tid=0
        self._rx=25.; self._ry=-35.; self._zm=1.
        self._cx=[0,0,0]; self._rad=1.; self._drag=False; self._rdrag=False; self._lp=None
        self._gl_ready=False; self._dl=0
        if _HAS_MODEL_GL:
            self._use_gl = True
            self.glw = _QOpenGLWidget()
            self.glw.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            self.glw.initializeGL = self._initGL
            self.glw.paintGL = self._paintGL
            self.glw.resizeGL = self._resizeGL
            lo.addWidget(self.glw)
            self.glw.installEventFilter(self)
            self.tex_cb.toggled.connect(lambda: self._rebuild_and_paint())
            self.wire_cb.toggled.connect(lambda: self.glw.update())
            self.smooth_cb.toggled.connect(lambda: self._rebuild_and_paint())
        else:
            self._use_gl = False
            self.glw = QLabel("OpenGL not available\npip install PyOpenGL"); self.glw.setStyleSheet("background:#111114;color:#888;")
            self.glw.setAlignment(Qt.AlignCenter)
            lo.addWidget(self.glw)
    def _initGL(self):
        glClearColor(0.067, 0.067, 0.08, 1.0)
        glEnable(GL_DEPTH_TEST); glEnable(GL_LIGHTING); glEnable(GL_LIGHT0)
        glLightfv(GL_LIGHT0, GL_POSITION, [0.3, 1.0, 0.8, 0.0])
        glLightfv(GL_LIGHT0, GL_DIFFUSE, [0.9, 0.9, 0.9, 1.0])
        glLightfv(GL_LIGHT0, GL_AMBIENT, [0.35, 0.35, 0.35, 1.0])
        glEnable(GL_COLOR_MATERIAL); glColorMaterial(GL_FRONT_AND_BACK, GL_AMBIENT_AND_DIFFUSE)
        glShadeModel(GL_SMOOTH)
        self._gl_ready = True
    def _resizeGL(self, w, h):
        glViewport(0, 0, max(w,1), max(h,1))
        glMatrixMode(GL_PROJECTION); glLoadIdentity()
        gluPerspective(45, max(w,1)/max(h,1), 0.01, 1000.0)
    def _upload_gl_tex(self):
        if self._gl_tid: glDeleteTextures([self._gl_tid]); self._gl_tid = 0
        if not self._pil_tex: return
        img = self._pil_tex.convert('RGBA'); w, h = img.size; raw = img.tobytes('raw', 'RGBA')
        self._gl_tid = glGenTextures(1); glBindTexture(GL_TEXTURE_2D, self._gl_tid)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_REPEAT)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_REPEAT)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, raw)
        glBindTexture(GL_TEXTURE_2D, 0)
    def _build_dl(self):
        if self._dl: glDeleteLists(self._dl, 1); self._dl = 0
        if not self._v or not self._t: return
        has_tex = self._gl_tid and self._uv and self.tex_cb.isChecked()
        use_smooth = self.smooth_cb.isChecked() and len(self._nrm) == len(self._v)
        self._dl = glGenLists(1); glNewList(self._dl, GL_COMPILE)
        if has_tex: glEnable(GL_TEXTURE_2D); glBindTexture(GL_TEXTURE_2D, self._gl_tid)
        glColor3f(1.0, 1.0, 1.0)
        glBegin(GL_TRIANGLES)
        for i0, i1, i2 in self._t:
            if max(i0,i1,i2) >= len(self._v): continue
            v0,v1,v2 = self._v[i0],self._v[i1],self._v[i2]
            if not use_smooth:
                # Flat shading: one face normal per triangle
                ex1,ey1,ez1 = v1[0]-v0[0],v1[1]-v0[1],v1[2]-v0[2]
                ex2,ey2,ez2 = v2[0]-v0[0],v2[1]-v0[1],v2[2]-v0[2]
                nx=ey1*ez2-ez1*ey2; ny=ez1*ex2-ex1*ez2; nz=ex1*ey2-ey1*ex2
                nl=(nx*nx+ny*ny+nz*nz)**0.5
                if nl>1e-8: nx/=nl;ny/=nl;nz/=nl
                else: nx,ny,nz=0,1,0
                glNormal3f(nx,ny,nz)
                if has_tex and i0<len(self._uv): glTexCoord2f(self._uv[i0][0],self._uv[i0][1])
                glVertex3f(*v0)
                if has_tex and i1<len(self._uv): glTexCoord2f(self._uv[i1][0],self._uv[i1][1])
                glVertex3f(*v1)
                if has_tex and i2<len(self._uv): glTexCoord2f(self._uv[i2][0],self._uv[i2][1])
                glVertex3f(*v2)
            else:
                # Smooth shading: per-vertex normals
                if has_tex and i0<len(self._uv): glTexCoord2f(self._uv[i0][0],self._uv[i0][1])
                glNormal3f(*self._nrm[i0]); glVertex3f(*v0)
                if has_tex and i1<len(self._uv): glTexCoord2f(self._uv[i1][0],self._uv[i1][1])
                glNormal3f(*self._nrm[i1]); glVertex3f(*v1)
                if has_tex and i2<len(self._uv): glTexCoord2f(self._uv[i2][0],self._uv[i2][1])
                glNormal3f(*self._nrm[i2]); glVertex3f(*v2)
        glEnd()
        if has_tex: glDisable(GL_TEXTURE_2D)
        glEndList()
    def _rebuild_and_paint(self):
        if self._use_gl and self._gl_ready:
            self.glw.makeCurrent(); self._build_dl(); self.glw.doneCurrent()
        if self._use_gl: self.glw.update()
    def _paintGL(self):
        glClear(GL_COLOR_BUFFER_BIT|GL_DEPTH_BUFFER_BIT)
        glMatrixMode(GL_MODELVIEW); glLoadIdentity()
        dist=self._rad*3.0/max(0.1,self._zm); cx,cy,cz=self._cx
        rx=math.radians(self._rx); ry=math.radians(self._ry)
        ex2=cx+dist*math.sin(ry)*math.cos(rx); ey2=cy+dist*math.sin(rx); ez2=cz+dist*math.cos(ry)*math.cos(rx)
        gluLookAt(ex2,ey2,ez2,cx,cy,cz,0,1,0)
        wire=self.wire_cb.isChecked()
        if wire: glDisable(GL_LIGHTING); glPolygonMode(GL_FRONT_AND_BACK,GL_LINE); glColor3f(0.88,0.63,0.19)
        else: glEnable(GL_LIGHTING); glPolygonMode(GL_FRONT_AND_BACK,GL_FILL); glColor3f(1,1,1)
        if self._dl: glCallList(self._dl)
        glDisable(GL_LIGHTING); glDisable(GL_TEXTURE_2D)
        glColor4f(0.2,0.2,0.22,0.5); gs=self._rad*0.25; y=cy-self._rad
        glBegin(GL_LINES)
        for gi in range(-5,6):
            glVertex3f(cx+gi*gs,y,cz-5*gs); glVertex3f(cx+gi*gs,y,cz+5*gs)
            glVertex3f(cx-5*gs,y,cz+gi*gs); glVertex3f(cx+5*gs,y,cz+gi*gs)
        glEnd()
        if not wire: glEnable(GL_LIGHTING); glPolygonMode(GL_FRONT_AND_BACK,GL_FILL)
    def set_texture(self, pil_img):
        self._pil_tex = pil_img
        if self._use_gl and self._gl_ready:
            self.glw.makeCurrent(); self._upload_gl_tex(); self._build_dl(); self.glw.doneCurrent()
    def load_model(self, name, data, cv=2, extra_parts=None):
        self._md=data; self._mn=name; self._cv=cv; bn=name[8:] if name.startswith('Stripped') else name
        self._v=[]; self._uv=[]; self._t=[]; self._nrm=[]; sc=1./256.; gqr=1./1024.
        fv=struct.unpack_from('>I',data,4)[0] if len(data)>=8 else 0
        parsed=False
        if fv==6 and len(data)>=28:
            sm=struct.unpack_from('>I',data,8)[0]; ic=struct.unpack_from('>I',data,12)[0]; vc=struct.unpack_from('>I',data,16)[0]
            if 28+(sm-1)*8+vc*16+ic*2==len(data) and vc<10000:
                vo=28+(sm-1)*8
                for i in range(vc):
                    o=vo+i*16; self._v.append((struct.unpack_from('>h',data,o)[0]*sc,-struct.unpack_from('>h',data,o+2)[0]*sc,-struct.unpack_from('>h',data,o+4)[0]*sc))
                    self._uv.append((struct.unpack_from('>h',data,o+12)[0]/1024.0,struct.unpack_from('>h',data,o+14)[0]/1024.0))
                self._t=asura._tristrip_to_tris([struct.unpack_from('>H',data,vo+vc*16+i*2)[0] for i in range(ic)]); parsed=True
        if not parsed and fv==14 and len(data)>=32:
            sm=struct.unpack_from('>I',data,8)[0]; dls=struct.unpack_from('>I',data,12)[0]; vc=struct.unpack_from('>I',data,16)[0]
            if 32+(sm-1)*12+vc*16+dls==len(data) and vc<10000:
                vo=32+(sm-1)*12
                for i in range(vc):
                    o=vo+i*16; self._v.append((struct.unpack_from('>h',data,o)[0]*sc,-struct.unpack_from('>h',data,o+2)[0]*sc,-struct.unpack_from('>h',data,o+4)[0]*sc))
                    self._uv.append((struct.unpack_from('>h',data,o+12)[0]/1024.0,struct.unpack_from('>h',data,o+14)[0]/1024.0))
                self._t=self._pdl(data[vo+vc*16:vo+vc*16+dls],vc,8); parsed=True
        if not parsed and cv>=3:
            mesh=asura._parse_smoothskin_cv3(data)
            if mesh and mesh.get('triangles'):
                for x,y,z in mesh['positions']: self._v.append((x*gqr,-y*gqr,-z*gqr))
                self._uv=mesh.get('uvs',[]); self._t=mesh['triangles']
                if mesh.get('normals'):
                    # Check if normals are real (not all identical/default)
                    n0 = mesh['normals'][0]
                    all_same = all(abs(n[0]-n0[0])<0.01 and abs(n[1]-n0[1])<0.01 and abs(n[2]-n0[2])<0.01 for n in mesh['normals'][:100])
                    if not all_same:
                        for nx,ny,nz in mesh['normals']: self._nrm.append((nx,-ny,-nz))
                if not self._nrm:
                    # Compute smooth normals from face geometry
                    self._nrm = self._compute_smooth_normals()
                parsed=True
        if not parsed:
            mesh=asura._parse_smoothskin(data,cv)
            if mesh:
                for x,y,z in mesh['positions']: self._v.append((x*gqr,-y*gqr,-z*gqr))
                if 'uvs' in mesh: self._uv=list(mesh['uvs'])
                if mesh.get('normals'):
                    n0 = mesh['normals'][0]
                    all_same = all(abs(n[0]-n0[0])<0.01 and abs(n[1]-n0[1])<0.01 and abs(n[2]-n0[2])<0.01 for n in mesh['normals'][:100])
                    if not all_same:
                        for nx,ny,nz in mesh['normals']: self._nrm.append((nx,-ny,-nz))
                self._t=asura._tristrip_to_tris(mesh['indices'],mesh['nVtx'])
                if not self._nrm:
                    self._nrm = self._compute_smooth_normals()
        # Add extra parts (multi-part character assembly)
        if extra_parts:
            # Determine if main model is SmoothSkin (parts should use character scale ÷1024)
            is_smoothskin = not parsed or (not (fv in (6,14)))  # cv3 or cv0-2
            part_sc = gqr if is_smoothskin else sc  # ÷1024 for character parts, ÷256 for prop parts
            for part in extra_parts:
                pd = part['data']; pfv = struct.unpack_from('>I',pd,4)[0] if len(pd)>=8 else 0
                base = len(self._v)
                if pfv==6 and len(pd)>=28:
                    sm2=struct.unpack_from('>I',pd,8)[0]; ic2=struct.unpack_from('>I',pd,12)[0]; vc2=struct.unpack_from('>I',pd,16)[0]
                    if 28+(sm2-1)*8+vc2*16+ic2*2==len(pd) and vc2<10000:
                        vo2=28+(sm2-1)*8
                        for i in range(vc2):
                            o=vo2+i*16; self._v.append((struct.unpack_from('>h',pd,o)[0]*part_sc,-struct.unpack_from('>h',pd,o+2)[0]*part_sc,-struct.unpack_from('>h',pd,o+4)[0]*part_sc))
                            self._uv.append((struct.unpack_from('>h',pd,o+12)[0]/1024.0,struct.unpack_from('>h',pd,o+14)[0]/1024.0))
                        idx=[struct.unpack_from('>H',pd,vo2+vc2*16+i*2)[0] for i in range(ic2)]
                        for a,b,c in asura._tristrip_to_tris(idx,vc2): self._t.append((a+base,b+base,c+base))
                elif pfv==14 and len(pd)>=32:
                    sm2=struct.unpack_from('>I',pd,8)[0]; dls2=struct.unpack_from('>I',pd,12)[0]; vc2=struct.unpack_from('>I',pd,16)[0]
                    if 32+(sm2-1)*12+vc2*16+dls2==len(pd) and vc2<10000:
                        vo2=32+(sm2-1)*12
                        for i in range(vc2):
                            o=vo2+i*16; self._v.append((struct.unpack_from('>h',pd,o)[0]*part_sc,-struct.unpack_from('>h',pd,o+2)[0]*part_sc,-struct.unpack_from('>h',pd,o+4)[0]*part_sc))
                            self._uv.append((struct.unpack_from('>h',pd,o+12)[0]/1024.0,struct.unpack_from('>h',pd,o+14)[0]/1024.0))
                        for a,b,c in self._pdl(pd[vo2+vc2*16:vo2+vc2*16+dls2],vc2,8): self._t.append((a+base,b+base,c+base))
            # Recompute smooth normals for the full assembled model
            self._nrm = self._compute_smooth_normals()
        # Compute smooth normals for props (v6/v14) that have no per-vertex normals
        if self._v and self._t and not self._nrm:
            self._nrm = self._compute_smooth_normals()
        if self._v:
            xs=[v[0] for v in self._v]; ys=[v[1] for v in self._v]; zs=[v[2] for v in self._v]
            self._cx=[(min(xs)+max(xs))/2,(min(ys)+max(ys))/2,(min(zs)+max(zs))/2]
            self._rad=max(max(xs)-min(xs),max(ys)-min(ys),max(zs)-min(zs))/2 or 1.
            self._zm=1.; self._rx=25.; self._ry=-35.
        if self._use_gl and self._gl_ready:
            self.glw.makeCurrent(); self._upload_gl_tex(); self._build_dl(); self.glw.doneCurrent()
        tex_str="textured" if self._pil_tex else "untextured"
        self.info.setText(f"{bn}  \u00b7  {len(self._v):,} verts  \u00b7  {len(self._t):,} tris  \u00b7  {tex_str}")
        if self._use_gl: self.glw.update()
    def _compute_smooth_normals(self):
        """Compute smooth per-vertex normals by averaging adjacent face normals."""
        nrm = [[0,0,0] for _ in range(len(self._v))]
        for i0,i1,i2 in self._t:
            if max(i0,i1,i2) >= len(self._v): continue
            v0,v1,v2 = self._v[i0],self._v[i1],self._v[i2]
            ex1,ey1,ez1 = v1[0]-v0[0],v1[1]-v0[1],v1[2]-v0[2]
            ex2,ey2,ez2 = v2[0]-v0[0],v2[1]-v0[1],v2[2]-v0[2]
            nx=ey1*ez2-ez1*ey2; ny=ez1*ex2-ex1*ez2; nz=ex1*ey2-ey1*ex2
            nrm[i0][0]+=nx; nrm[i0][1]+=ny; nrm[i0][2]+=nz
            nrm[i1][0]+=nx; nrm[i1][1]+=ny; nrm[i1][2]+=nz
            nrm[i2][0]+=nx; nrm[i2][1]+=ny; nrm[i2][2]+=nz
        result = []
        for nx,ny,nz in nrm:
            nl = (nx*nx+ny*ny+nz*nz)**0.5
            if nl > 1e-8: result.append((nx/nl,ny/nl,nz/nl))
            else: result.append((0,1,0))
        return result
    def _pdl(self,dl,nv,stride):
        tris=[]; d=0
        while d<len(dl)-3:
            cmd=dl[d]
            if 0x98<=cmd<=0x9f:
                cnt=struct.unpack_from('>H',dl,d+1)[0]
                if 3<=cnt<=65535:
                    vd=d+3; ve=vd+cnt*stride
                    if ve<=len(dl):
                        pis=[]; ok=True
                        for vi in range(cnt):
                            pi=struct.unpack_from('>H',dl,vd+vi*stride)[0]
                            if pi>=nv: ok=False; break
                            pis.append(pi)
                        if ok:
                            for i in range(len(pis)-2):
                                a,b,c=pis[i],pis[i+1],pis[i+2]
                                if a==b or b==c or a==c: continue
                                if i%2==0: tris.append((a,b,c))
                                else: tris.append((a,c,b))
                            d=ve; continue
                d+=1
            elif cmd==0: d+=1
            else: d+=1
        return tris
    def _screenshot(self):
        if not self._v: return
        p,_=QFileDialog.getSaveFileName(self,"Save Screenshot","model_screenshot.png","PNG (*.png)")
        if p and self._use_gl:
            # Render with transparent background
            self.glw.makeCurrent()
            glClearColor(0, 0, 0, 0)
            self.glw.paintGL()
            img = self.glw.grabFramebuffer()
            glClearColor(0.067, 0.067, 0.08, 1.0)
            self.glw.paintGL()
            self.glw.doneCurrent()
            img.save(p)
            self.info.setText(self.info.text() + f"  ·  saved {os.path.basename(p)}")
    def _export(self):
        if not self._md: return
        bn=self._mn[8:] if self._mn.startswith('Stripped') else self._mn
        p,_=QFileDialog.getSaveFileName(self,"Export OBJ",bn+".obj","OBJ (*.obj)")
        if p:
            ok,info=asura.convert_model_to_obj(self._mn,self._md,p,self._cv)
            if ok: QMessageBox.information(self,"Export",f"Exported: {info}")
            else: QMessageBox.warning(self,"Error",info)
    def eventFilter(self,obj,event):
        t=event.type()
        if t==QEvent.MouseButtonPress:
            self._lp=event.position()
            if event.button()==Qt.RightButton: self._rdrag=True
            else: self._drag=True
            return True
        elif t==QEvent.MouseButtonRelease: self._drag=False; self._rdrag=False; return True
        elif t==QEvent.MouseMove and self._lp:
            dx=event.position().x()-self._lp.x(); dy=event.position().y()-self._lp.y(); self._lp=event.position()
            if self._drag: self._ry+=dx*0.5; self._rx=max(-89,min(89,self._rx+dy*0.5))
            if self._use_gl: self.glw.update()
            return True
        elif t==QEvent.Wheel:
            self._zm*=1.1 if event.angleDelta().y()>0 else 0.91; self._zm=max(0.01,min(100,self._zm))
            if self._use_gl: self.glw.update()
            return True
        return False

class LevelView(QWidget):
    """Level overview: entity table + stats. The full top-down map is LevelViewer below."""
    def __init__(self):
        super().__init__(); lo = QVBoxLayout(self); lo.setContentsMargins(8,8,8,8); lo.setSpacing(4)
        self.lbl = QLabel("Level Overview"); self.lbl.setObjectName("ptitle"); lo.addWidget(self.lbl)
        self.stats = QLabel(""); self.stats.setWordWrap(True); self.stats.setStyleSheet("color:#aaa;font-size:12px;line-height:1.6;"); lo.addWidget(self.stats)
        lo.addWidget(QLabel("Entity Placements:"))
        self.tbl = QTableWidget(); self.tbl.setAlternatingRowColors(True)
        self.tbl.horizontalHeader().setStretchLastSection(True)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows); lo.addWidget(self.tbl)
    def load_level(self, level_data):
        ents = level_data['entities']
        with_pos = [e for e in ents if e.get('pos')]
        with_model = sum(1 for e in ents if e.get('model'))
        self.lbl.setText(f"Level Overview — {len(ents)} entities")
        self.stats.setText(
            f"Entities: {len(ents)} total · {len(with_pos)} with positions · {with_model} with models\n"
            f"Materials: {len(level_data.get('txet_paths', []))} texture slots\n"
            f"Env bounds: {'Yes' if level_data.get('env_bounds') else 'None'}")
        headers = ['Type','ID','Model','X','Y','Z']
        self.tbl.setColumnCount(6); self.tbl.setHorizontalHeaderLabels(headers)
        rows = [e for e in ents if e.get('pos')]
        self.tbl.setRowCount(len(rows))
        etype_names = {0x8006:'DestrObj',0x8005:'Pickup',0x8003:'NPC',0x8001:'Actor',0x8012:'Trampoline',0x8013:'Interactive',0x8017:'NPCSpawn',0x8021:'LardLad',0x800C:'Player',0x0007:'PhysObj',0x0011:'AdvLight',0x002f:'SpawnPt',0x0029:'StartPt',0x0021:'PFX',0x0033:'CamVol',0x8022:'Objective'}
        for i, e in enumerate(rows):
            self.tbl.setItem(i,0,QTableWidgetItem(etype_names.get(e['type'], f"0x{e['type']:04x}")))
            self.tbl.setItem(i,1,QTableWidgetItem(f"0x{e['eid']:08x}"))
            self.tbl.setItem(i,2,QTableWidgetItem(e.get('model','') or ''))
            self.tbl.setItem(i,3,QTableWidgetItem(f"{e['pos'][0]:.2f}"))
            self.tbl.setItem(i,4,QTableWidgetItem(f"{e['pos'][1]:.2f}"))
            self.tbl.setItem(i,5,QTableWidgetItem(f"{e['pos'][2]:.2f}"))
        self.tbl.resizeColumnsToContents()

class ChunkPropsView(QWidget):
    def __init__(self):
        super().__init__(); lo=QVBoxLayout(self); lo.setContentsMargins(8,8,8,8); lo.setSpacing(4)
        self.title=QLabel("No chunk"); self.title.setObjectName("ptitle"); lo.addWidget(self.title)
        self.props=QLabel(""); self.props.setWordWrap(True); self.props.setStyleSheet("color:#888;font-size:11px;"); lo.addWidget(self.props)
        # Scrollable decoded content (rich HTML)
        self.decoded=QTextBrowser(); self.decoded.setOpenExternalLinks(False)
        self.decoded.setStyleSheet("color:#ccc;font-size:12px;background:#1a1a20;border:1px solid #333;border-radius:4px;padding:6px;")
        lo.addWidget(self.decoded, 3)
        # Collapsible hex view
        self.hex_toggle=QPushButton("▶ Raw Hex"); self.hex_toggle.setFlat(True)
        self.hex_toggle.setStyleSheet("color:#888;font-size:11px;text-align:left;padding:2px 6px;")
        self.hex_toggle.clicked.connect(self._toggle_hex); lo.addWidget(self.hex_toggle)
        self.hv=HexView(); self.hv.hide(); lo.addWidget(self.hv, 2)
    def _toggle_hex(self):
        if self.hv.isVisible():
            self.hv.hide(); self.hex_toggle.setText("▶ Raw Hex")
        else:
            self.hv.show(); self.hex_toggle.setText("▼ Raw Hex")
    def show_chunk(self, c, all_chunks=None):
        self.title.setText(f"Chunk: {c['id']}")
        self.props.setText(f"Size: {c['size']:,}  ·  Version: {c['ver']}  ·  Unk: {c['unk']}  ·  Offset: 0x{c['offset']:08x}")
        info = _decode_chunk_info(c, all_chunks=all_chunks)
        if info:
            self.decoded.setHtml(info); self.decoded.show()
        else:
            self.decoded.hide()
        self.hv.set_data(c['content']); self.hv.hide(); self.hex_toggle.setText("▶ Raw Hex")

def _find_strings(d, min_len=3, max_scan=512):
    strings = []; j = 0
    while j < min(len(d), max_scan):
        if 32 <= d[j] < 127:
            start = j
            while j < len(d) and 32 <= d[j] < 127: j += 1
            s = d[start:j].decode('ascii', errors='replace')
            if len(s) >= min_len: strings.append((start, s))
        else: j += 1
    return strings

_CSS = """<style>
table{border-collapse:collapse;width:100%;margin:4px 0;}
th{background:#2a2a32;color:#aaa;font-size:11px;padding:3px 6px;text-align:left;border:1px solid #333;}
td{padding:3px 6px;border:1px solid #2a2a30;font-size:11px;color:#ccc;}
tr:nth-child(even){background:#1e1e26;}
.hdr{color:#7ca;font-weight:bold;font-size:13px;margin:6px 0 2px 0;}
.sub{color:#888;font-size:11px;margin:2px 0;}
.val{color:#e0a030;font-family:monospace;}
.path{color:#8be;font-family:monospace;font-size:11px;}
.dim{color:#888;}
</style>"""

def _tbl(headers, rows, max_rows=200):
    """Build an HTML table."""
    h = '<table><tr>' + ''.join(f'<th>{c}</th>' for c in headers) + '</tr>'
    for i, row in enumerate(rows):
        if i >= max_rows:
            h += f'<tr><td colspan="{len(headers)}" style="color:#888">... {len(rows)-max_rows} more rows</td></tr>'
            break
        h += '<tr>' + ''.join(f'<td>{c}</td>' for c in row) + '</tr>'
    return h + '</table>'

def _decode_chunk_info(c, all_chunks=None):
    """Decode known chunk types into rich formatted HTML."""
    cid, d, ver, unk = c['id'], c['content'], c['ver'], c.get('unk', 0)
    if len(d) < 4: return None
    try:
        # ── ITNE: Entity Instances ──
        if cid == 'ITNE':
            eid = struct.unpack_from('>I', d, 0)[0]
            etype = struct.unpack_from('>H', d, 4)[0]
            # Use canonical entity type names from tsg_oldgen module
            _ENTITY_DISPLAY_NAMES = {
                0x0001:'Time Trigger',0x0003:'Cutscene Controller',0x0007:'Physics Object',
                0x0009:'Destructible Light',0x000B:'Splitter Block',0x000D:'Counted Trigger',
                0x000E:'Sound Controller',0x0011:'Advanced Light',0x0014:'Adv Volume Trigger',
                0x0015:'Lift',0x0016:'Damage Volume',0x0018:'Music Trigger',
                0x001C:'FMV Trigger',0x001F:'Meta Music Trigger',
                0x0021:'PFX Effect',0x0022:'Template',0x0023:'LookAt Trigger',
                0x0024:'Clock Trigger',0x0026:'Logic Trigger',
                0x0028:'Client Volume Trigger',0x0029:'Start Point',0x002A:'Timeline Trigger',
                0x002B:'Env Texture Anim Ctrl',0x002F:'Debug Message Trigger',
                0x0033:'Camera Volume',0x0034:'Proxy Trigger',
                0x0035:'Node',0x0036:'Oriented Node',0x0037:'Gamescene Node',
                0x0038:'Coverpoint',0x0039:'Guard Zone',0x003A:'Gamescene Attractor',
                0x003B:'Spline',0x003C:'Gamescene Spline',0x003E:'Dialogue Trigger',
                0x003F:'Lift Node',0x0040:'Lift Spline',
                0x0044:'Teleporter',0x0045:'Teleport Destination',
                0x0048:'Force Conveyor',0x0049:'Attractor Controller',
                0x004A:'Console Var',0x004C:'Stream Bg Sound Ctrl',
                0x8001:'Actor',0x8003:'NPC',0x8004:'Usable Object',
                0x8005:'Pickup Object',0x8006:'Destructible Object',0x8007:'Start Point',
                0x800C:'Player',0x800D:'Player',0x800E:'Shover',
                0x800F:'Guard Zone (Simpsons)',0x8010:'Updraft',0x8011:'Bunny',
                0x8012:'Trampoline',0x8013:'Interactive',0x8014:'Hand of Buddha',
                0x8015:'Respawn',0x8016:'Death Volume',0x8017:'NPC Spawner',
                0x8018:'HoB Port',0x8019:'HoB Snap-To Point',
                0x801A:'Interaction Trigger',0x801B:'See-Saw',
                0x801D:'Nav Waypoint',0x801E:'Guard Zone',
                0x801F:'Damager Object',0x8020:'Stubborn Ape',0x8021:'Lard Lad',
                0x8022:'Objective',0x8023:'Selmatty',0x8024:'Transition Trigger',
                0x8025:'Groening',0x8026:'Shakespeare',0x8027:'Bart Ring',
                0x8028:'Lard Lad Flap',0x8029:'State Trigger',0x802A:'Parachute',
            }
            tname = _ENTITY_DISPLAY_NAMES.get(etype, f'Unknown (0x{etype:04x})')
            r = _CSS + f'<div class="hdr">Entity Instance — {tname}</div>'
            rows = [
                ('GUID', f'<span class="val">0x{eid:08X}</span>'),
                ('Type', f'<span class="val">0x{etype:04X}</span> — {tname}'),
                ('Data Size', f'<span class="val">{len(d)}</span> bytes'),
            ]
            if len(d) >= 8:
                flags = struct.unpack_from('>H', d, 6)[0]
                rows.append(('Flags', f'<span class="val">0x{flags:04X}</span>'))
            # Debug Message Trigger: show the dev text prominently
            if etype == 0x002F and len(d) > 50:
                null = d[48:].find(b'\x00')
                if null > 0:
                    try:
                        dtxt = d[48:48+null].decode('ascii')
                        if all(32 <= ord(ch) < 127 for ch in dtxt):
                            rows.append(('Debug Text', f'<span style="color:#ff6;font-weight:bold;font-size:13px">&quot;{dtxt}&quot;</span>'))
                    except: pass
            if len(d) >= 72:
                bp_ref = struct.unpack_from('>I', d, 68)[0]
                rows.append(('Blueprint Ref', f'<span class="val">0x{bp_ref:08X}</span>'))
            if len(d) >= 84:
                px,py,pz = [struct.unpack_from('>f', d, 72+i*4)[0] for i in range(3)]
                rows.append(('Position', f'<span class="val">({px:.3f}, {py:.3f}, {pz:.3f})</span>'))
            if len(d) >= 100:
                qx,qy,qz,qw = [struct.unpack_from('>f', d, 84+i*4)[0] for i in range(4)]
                rows.append(('Rotation (QUAT)', f'<span class="val">({qx:.4f}, {qy:.4f}, {qz:.4f}, {qw:.4f})</span>'))
            if len(d) >= 104:
                radius = struct.unpack_from('>f', d, 100)[0]
                rows.append(('Radius', f'<span class="val">{radius:.3f}</span>'))
            # Show parameter block
            if len(d) >= 68:
                params = []
                for j in range(20, 68, 4):
                    v = struct.unpack_from('>I', d, j)[0]
                    if v != 0: params.append((j, v))
                if params:
                    pstr = ', '.join(f'[{off}]=0x{v:08X}' for off, v in params[:8])
                    rows.append(('Parameters', f'<span class="dim">{pstr}</span>'))
            r += _tbl(['Field', 'Value'], rows)
            return r

        # ── LRTM: Material Properties ──
        elif cid == 'LRTM':
            n = struct.unpack_from('>I', d, 0)[0]
            stride = (len(d) - 4) // n if n > 0 else 0
            r = _CSS + f'<div class="hdr">Material Properties — {n} entries</div>'
            r += f'<div class="sub">Chunk version: {ver} · Entry stride: {stride} bytes</div>'
            if stride == 36 and n > 0:
                # Find matching TXET to show texture names
                txet_paths = []
                if all_chunks:
                    for tc in all_chunks:
                        if tc['id'] == 'TXET':
                            tn = struct.unpack_from('>I', tc['content'], 0)[0]
                            if tn > len(txet_paths):
                                txet_paths = asura._parse_txet_paths(tc['content'])
                rows = []
                for i in range(n):
                    off = 4 + i * stride
                    vals = struct.unpack_from('>9I', d, off)
                    tex0 = vals[0]; flags = vals[6]; extra = vals[7]; anim = vals[8]
                    # Texture name
                    if tex0 < len(txet_paths) and txet_paths[tex0]:
                        tname = txet_paths[tex0].split('/')[-1]
                        tex_cell = f'<span class="val">{tex0}</span> {tname}'
                    elif tex0 == 0xFFFFFFFF:
                        tex_cell = '<span class="dim">-1</span>'
                    else:
                        tex_cell = f'<span class="val">{tex0}</span> <span class="dim">(empty)</span>'
                    # Flags
                    flag_parts = []
                    if flags & 0x2: flag_parts.append('<span style="color:#6af">TRANSPARENT</span>')
                    if flags & 0x20000: flag_parts.append('<span style="color:#fa6">TEX_ANIM</span>')
                    if flags & 0x40000: flag_parts.append('<span style="color:#fa6">TEX_ANIM2</span>')
                    flags_cell = ', '.join(flag_parts) if flag_parts else ('0x%X' % flags if flags else '—')
                    # Material type
                    mat_type = extra & 0xFF
                    type_names = {0:'default',1:'concrete',2:'carpet',3:'wood',4:'metal',
                                  5:'grass',6:'dirt',7:'water',8:'tile',9:'gravel',
                                  0xA:'sand',0xB:'snow',0xC:'mud',0xD:'glass'}
                    type_name = type_names.get(mat_type, f'0x{mat_type:X}')
                    render_bits = extra >> 8
                    type_cell = type_name
                    if render_bits: type_cell += f' <span class="dim">+0x{render_bits:X}00</span>'
                    # Extra
                    extra_cell = f'0x{anim:08X}' if anim else '—'
                    rows.append([f'{i}', tex_cell, flags_cell, type_cell, extra_cell])
                r += _tbl(['#', 'Texture', 'Flags', 'Surface Type', 'Anim Data'], rows)
            elif stride > 0:
                rows = []
                for i in range(min(n, 50)):
                    off = 4 + i * stride
                    vals = [struct.unpack_from('>I', d, off + j*4)[0] for j in range(min(stride//4, 6))]
                    rows.append([f'{i}'] + [f'0x{v:X}' for v in vals])
                hdr = ['#'] + [f'Field {j}' for j in range(len(rows[0])-1)] if rows else ['#']
                r += _tbl(hdr, rows)
            return r

        # ── TXET: Texture Names ──
        elif cid == 'TXET':
            n = struct.unpack_from('>I', d, 0)[0]
            # Parse ALL strings in content (including extras after n_strings)
            all_strings = []; pos = 4
            while pos < len(d):
                null = d[pos:].find(b'\x00')
                if null == -1: break
                all_strings.append(d[pos:pos+null].decode('ascii', errors='replace'))
                pos += null + 1
            r = _CSS + f'<div class="hdr">Texture Names — {n} indexed</div>'
            if len(all_strings) > n:
                r += f'<div class="sub">{len(all_strings)} total strings ({len(all_strings)-n} extra after index)</div>'
            rows = []
            for i, s in enumerate(all_strings):
                idx_str = f'{i}' if i < n else f'+{i-n}'
                if s:
                    short = s.split('\\')[-1].split('/')[-1]
                    rows.append([idx_str, f'<span class="path">{short}</span>',
                                f'<span class="dim">{s}</span>'])
                else:
                    rows.append([idx_str, '<span class="dim">(empty)</span>', ''])
            r += _tbl(['#', 'Texture', 'Full Path'], rows)
            return r

        # ── LFXT: Texture Filter Flags ──
        elif cid == 'LFXT':
            n = struct.unpack_from('>I', d, 0)[0] if len(d) >= 4 else 0
            entry_sz = (len(d) - 4) // n if n > 0 else 0
            r = _CSS + f'<div class="hdr">Texture Filter Flags — {n} entries</div>'
            r += f'<div class="sub">Entry size: {entry_sz} bytes</div>'
            if entry_sz >= 4 and n > 0:
                rows = []
                for i in range(min(n, 100)):
                    off = 4 + i * entry_sz
                    val = struct.unpack_from('>I', d, off)[0]
                    flags = []
                    if val & 0x8000: flags.append('REPEAT')
                    if val & 0x1: flags.append('CLAMP_S')
                    if val & 0x200000: flags.append('ANIM')
                    rows.append([f'{i}', f'<span class="val">0x{val:08X}</span>',
                                ' '.join(flags) if flags else '—'])
                r += _tbl(['#', 'Flags', 'Decoded'], rows)
            return r

        # ── MSDS: Sound Events ──
        elif cid == 'MSDS':
            null = d.find(b'\x00')
            if null < 0: return None
            name = d[:null].decode('ascii', errors='replace')
            off = (null+4)&~3
            h1 = struct.unpack_from('>I', d, off)[0] if off+4 <= len(d) else 0
            h2 = struct.unpack_from('>I', d, off+4)[0] if off+8 <= len(d) else 0
            wav = ''
            for i in range(null+1, len(d)-6):
                if d[i:i+7] in (b'\\Sounds', b'\\sounds', b'Sounds\\', b'sounds\\'):
                    end = d[i:].find(b'\x00')
                    if end > 0: wav = d[i:i+end].decode('ascii', errors='replace')
                    break
            r = _CSS + f'<div class="hdr">Sound Event</div>'
            rows = [('Name', f'<span class="val">{name}</span>'),
                    ('Hash 1', f'<span class="val">0x{h1:08X}</span>'),
                    ('Hash 2', f'<span class="val">0x{h2:08X}</span>')]
            if wav: rows.append(('Audio File', f'<span class="path">{wav}</span>'))
            r += _tbl(['Field', 'Value'], rows)
            return r

        # ── VELD: Voice Events ──
        elif cid == 'VELD':
            null = d.find(b'\x00')
            if null < 0: return None
            name = d[:null].decode('ascii', errors='replace')
            off = (null+4)&~3
            count = struct.unpack_from('>I', d, off)[0] if off+4 <= len(d) else 0
            sids = []; soff = off + 4
            for i in range(min(count, 50)):
                n2 = d[soff:].find(b'\x00')
                if n2 <= 0: break
                sids.append(d[soff:soff+n2].decode('ascii', errors='replace'))
                soff += n2 + 1
            r = _CSS + f'<div class="hdr">Voice Event — {name}</div>'
            r += f'<div class="sub">{count} sound IDs</div>'
            rows = [(f'{i}', f'<span class="val">{sid}</span>') for i, sid in enumerate(sids)]
            r += _tbl(['#', 'Sound ID'], rows)
            return r

        # ── BBSH: Bounding Shapes ──
        elif cid == 'BBSH':
            null = d.find(b'\x00')
            if null < 0: return None
            name = d[:null].decode('ascii', errors='replace')
            off = (null+4)&~3
            n_sub = struct.unpack_from('>I', d, off)[0] if off+4 <= len(d) else 0
            off += 4
            r = _CSS + f'<div class="hdr">Bounding Shape — {name}</div>'
            r += f'<div class="sub">{n_sub} sub-shapes</div>'
            rows = []
            for i in range(min(n_sub, 50)):
                if off + 24 > len(d): break
                vals = [struct.unpack_from('>f', d, off+j*4)[0] for j in range(6)]
                rows.append([f'{i}',
                    f'({vals[0]:.2f}, {vals[1]:.2f}, {vals[2]:.2f})',
                    f'({vals[3]:.2f}, {vals[4]:.2f}, {vals[5]:.2f})'])
                off += 24
            r += _tbl(['#', 'Min (X,Y,Z)', 'Max (X,Y,Z)'], rows)
            return r

        # ── DNSH: Hit/Dodge Shapes ──
        elif cid == 'DNSH':
            v0 = struct.unpack_from('>I', d, 0)[0]
            null = d[4:].find(b'\x00')
            name = d[4:4+null].decode('ascii', errors='replace') if null > 0 else '?'
            off = ((4+null+1)+3)&~3
            r = _CSS + f'<div class="hdr">Hit/Dodge Shape — {name}</div>'
            rows = [('Count/Type', f'<span class="val">{v0}</span>')]
            floats = []
            for j in range(off, min(len(d), off+60), 4):
                f = struct.unpack_from('>f', d, j)[0]
                if abs(f) < 10000 and f == f: floats.append(f)
            if floats:
                labels = ['Damage','Timing','Range','Width','Height','Speed','Duration']
                for i, fv in enumerate(floats[:7]):
                    rows.append((labels[i] if i < len(labels) else f'Param[{i}]',
                                f'<span class="val">{fv:.4f}</span>'))
            r += _tbl(['Field', 'Value'], rows)
            return r

        # ── VEDS: Debris/VFX ──
        elif cid == 'VEDS':
            null = d.find(b'\x00')
            if null < 0: return None
            name = d[:null].decode('ascii', errors='replace')
            off = (null+4)&~3
            r = _CSS + f'<div class="hdr">Debris/VFX — {name}</div>'
            # Parse scale/color/count fields
            labels = ['Scale X','Scale Y','Scale Z','Color Scale','Offset X','Offset Y',
                      'Gravity','Lifetime','Speed','Count','Flags','Extra']
            rows = []
            for j in range(min(12, (len(d)-off)//4)):
                fv = struct.unpack_from('>f', d, off+j*4)[0]
                iv = struct.unpack_from('>I', d, off+j*4)[0]
                lab = labels[j] if j < len(labels) else f'[{j}]'
                if abs(fv) < 10000 and fv == fv and abs(fv) > 0.0001:
                    rows.append((lab, f'<span class="val">{fv:.4f}</span>'))
                elif iv != 0:
                    rows.append((lab, f'<span class="val">0x{iv:08X}</span> ({iv})'))
            r += _tbl(['Parameter', 'Value'], rows)
            return r

        # ── STUC: Cutscene ──
        elif cid == 'STUC':
            cs = asura.parse_stuc_chunk(d, c['ver'])
            if cs:
                r = _CSS + f'<div class="hdr">Cutscene — {cs["name"]}</div>'
                px, py, pz = cs['position']
                r += f'<div class="sub">Duration: <b>{cs["duration"]:.1f}s</b> · '
                r += f'Position: ({px:.1f}, {py:.1f}, {pz:.1f}) · '
                r += f'Flags: 0x{cs["flags"]:04X} · Speed: {cs["playback_speed"]:.1f}×</div>'
                if cs['display_name'] or cs['target_name']:
                    r += f'<div class="sub">Display: "{cs["display_name"]}" Target: "{cs["target_name"]}"</div>'
                if cs['actors']:
                    r += '<div class="sub" style="margin-top:6px"><b>Actors</b></div>'
                    actor_rows = []
                    for i, (idx, name) in enumerate(cs['actors']):
                        asr = cs['asr_paths'][i][1] if i < len(cs['asr_paths']) else ''
                        actor_rows.append((str(idx), f'<span class="val">{name}</span>', f'<span class="path">{asr}</span>'))
                    r += _tbl(['Slot', 'Model Name', 'ASR Path'], actor_rows)
                if cs['anim_names']:
                    r += '<div class="sub" style="margin-top:6px"><b>Animations</b></div>'
                    anim_rows = []
                    for i, (idx, name) in enumerate(cs['anim_names']):
                        asr = cs['anim_paths'][i][1] if i < len(cs['anim_paths']) else ''
                        anim_rows.append((str(idx), f'<span class="val">{name}</span>', f'<span class="path">{asr}</span>'))
                    r += _tbl(['Slot', 'Animation', 'ASR Path'], anim_rows)
                if not cs['actors'] and not cs['anim_names']:
                    r += '<div class="sub" style="margin-top:6px;color:#888;">Camera-only cutscene (no actors/animations)</div>'
                r += f'<div class="sub" style="margin-top:8px;color:#666;">field1={cs["field1"]} field2={cs["field2"]} '
                r += f'param1={cs["param1"]} param2={cs["param2"]} playback_ctrl=0x{cs["playback_ctrl"]:08X}</div>'
                return r
            else:
                strings = _find_strings(d, 3, len(d))
                r = _CSS + f'<div class="hdr">Cutscene (parse failed)</div>'
                r += _tbl(['Offset','String'], [(f'0x{o:04X}', s) for o, s in strings[:20]])
                return r

        # ── GOF: Fog Settings ──
        elif cid == ' GOF':
            # Ghidra: Chunk_Fog::Process (line 70760)
            # flags: bit0=fog enabled, bit1=graph fog enabled
            # v1: 0x28 (40) bytes: RGBA color + near + far + value_at_far + skybox_blend + graph_point
            floats = [struct.unpack_from('>f', d, j*4)[0] for j in range(min(10, len(d)//4))]
            flags = unk  # chunk_unknown field
            r = _CSS + f'<div class="hdr">Fog Settings</div>'
            labels = ['Red','Green','Blue','Alpha','Near Plane','Far Plane','Value at Far','Skybox Blend','Graph X','Graph Y']
            rows = []
            if len(floats) >= 3:
                cr,cg,cb = [min(255,int(floats[i]*255)) for i in range(3)]
                rows.append(('Color Preview', f'<span style="background:rgb({cr},{cg},{cb});padding:2px 20px;border:1px solid #555;">&nbsp;</span> RGB({cr},{cg},{cb})'))
            rows.append(('Fog Enabled', f'<span class="val">{"Yes" if flags & 1 else "No"}</span>'))
            rows.append(('Graph Fog', f'<span class="val">{"Yes" if flags & 2 else "No"}</span>'))
            for i, f in enumerate(floats[:10]):
                rows.append((labels[i] if i < len(labels) else f'[{i}]', f'<span class="val">{f:.4f}</span>'))
            r += _tbl(['Parameter', 'Value'], rows)
            return r

        # ── RHTW: Weather ──
        elif cid == 'RHTW':
            # Ghidra: Chunk_WeatherSystem::Process (line 81364)
            # v<10: 24-byte header (first u32=particle count, skip particle_count*0x24 bytes)
            # Then: wind(4) + ambient_color(12) + cloud_density(4) + precip_rate(4) + precip_speed(4)
            #       + ARGB glow tint packed u32(4) + glow_level(4) + wind_params(8)
            r = _CSS + f'<div class="hdr">Weather Settings</div>'
            rows = []
            off2 = 0
            if ver < 10 and len(d) >= 24:
                particle_count = struct.unpack_from('>I', d, 0)[0]
                rows.append(('Particle Types', f'<span class="val">{particle_count}</span> (each 0x24=36 bytes)'))
                off2 = 24 + particle_count * 0x24
            if off2 + 4 <= len(d):
                wind = struct.unpack_from('>f', d, off2)[0]; off2 += 4
                rows.append(('Wind Direction', f'<span class="val">{wind:.4f}</span>'))
            if off2 + 12 <= len(d):
                ar, ag, ab = [struct.unpack_from('>f', d, off2+j*4)[0] for j in range(3)]; off2 += 12
                cr,cg,cb = [min(255,int(v*255)) for v in (ar,ag,ab)]
                rows.append(('Ambient Color', f'<span style="background:rgb({cr},{cg},{cb});padding:2px 12px;border:1px solid #555;">&nbsp;</span> ({ar:.2f}, {ag:.2f}, {ab:.2f})'))
            if off2 + 4 <= len(d):
                cd = struct.unpack_from('>f', d, off2)[0]; off2 += 4
                rows.append(('Cloud Density', f'<span class="val">{cd:.4f}</span>'))
            if off2 + 4 <= len(d):
                pr = struct.unpack_from('>f', d, off2)[0]; off2 += 4
                rows.append(('Precip Rate', f'<span class="val">{pr:.4f}</span>'))
            if off2 + 4 <= len(d):
                ps = struct.unpack_from('>f', d, off2)[0]; off2 += 4
                rows.append(('Precip Speed', f'<span class="val">{ps:.4f}</span>'))
            if off2 + 4 <= len(d):
                glow_packed = struct.unpack_from('>I', d, off2)[0]; off2 += 4
                ga,gr2,gg,gb = (glow_packed>>24)&0xFF,(glow_packed>>16)&0xFF,(glow_packed>>8)&0xFF,glow_packed&0xFF
                rows.append(('Glow Tint', f'<span style="background:rgb({gr2},{gg},{gb});padding:2px 12px;border:1px solid #555;">&nbsp;</span> ARGB(0x{glow_packed:08X})'))
            if off2 + 4 <= len(d):
                gl = struct.unpack_from('>f', d, off2)[0]; off2 += 4
                rows.append(('Glow Level', f'<span class="val">{gl:.4f}</span>'))
            if off2 + 8 <= len(d):
                w1 = struct.unpack_from('>f', d, off2)[0]; w2 = struct.unpack_from('>f', d, off2+4)[0]
                rows.append(('Wind Params', f'{w1:.4f}, {w2:.4f}'))
            r += _tbl(['Parameter', 'Value'], rows)
            return r

        # ── BYKS: Skybox ──
        elif cid == 'BYKS':
            r = _CSS + f'<div class="hdr">Skybox</div>'
            # RGB color
            if len(d) >= 12:
                cr,cg,cb = [int(struct.unpack_from('>f', d, i*4)[0]*255) for i in range(3)]
                r += f'<div class="sub">Ambient: <span style="background:rgb({min(255,cr)},{min(255,cg)},{min(255,cb)});padding:2px 12px;border:1px solid #555;">&nbsp;</span> ({cr},{cg},{cb})</div>'
            strings = _find_strings(d, 5, len(d))
            paths = [s for _, s in strings if '\\' in s or '/' in s]
            face_names = ['Top','Front','Right','Back','Left','Bottom']
            rows = [(face_names[i] if i < 6 else f'Face {i}',
                     f'<span class="path">{p.split(chr(92))[-1]}</span>',
                     f'<span class="dim">{p}</span>') for i, p in enumerate(paths)]
            r += _tbl(['Face', 'Texture', 'Full Path'], rows)
            return r

        # ── TATC: Bone Attachments ──
        elif cid == 'TATC':
            v0 = struct.unpack_from('>I', d, 0)[0]
            null = d[4:].find(b'\x00')
            name = d[4:4+null].decode('ascii', errors='replace') if null > 0 else '?'
            r = _CSS + f'<div class="hdr">Bone Attachment — {name}</div>'
            r += _tbl(['Field', 'Value'], [
                ('Attachment', f'<span class="val">{name}</span>'),
                ('Bone Index', f'<span class="val">{v0}</span>')
            ])
            return r

        # ── AMDS: Code Sounds ──
        elif cid == 'AMDS':
            null = d.find(b'\x00')
            name = d[:null].decode('ascii', errors='replace') if null > 0 else '?'
            off = (null+4)&~3
            h1 = struct.unpack_from('>I', d, off)[0] if off+4 <= len(d) else 0
            h2 = struct.unpack_from('>I', d, off+4)[0] if off+8 <= len(d) else 0
            r = _CSS + f'<div class="hdr">Code-Triggered Sound — {name}</div>'
            r += _tbl(['Field', 'Value'], [
                ('Name', f'<span class="val">{name}</span>'),
                ('Hash 1', f'<span class="val">0x{h1:08X}</span>'),
                ('Hash 2', f'<span class="val">0x{h2:08X}</span>')
            ])
            return r

        # ── NSBS: Streaming Audio ──
        elif cid == 'NSBS':
            null = d.find(b'\x00')
            path = d[:null].decode('ascii', errors='replace') if null > 0 else '?'
            r = _CSS + f'<div class="hdr">Streaming Audio</div>'
            r += f'<div class="sub"><span class="path">{path}</span></div>'
            return r

        # ── NKSH: Skeleton ──
        elif cid == 'NKSH':
            if len(d) > 12:
                morph_count = struct.unpack_from('>I', d, 0)[0]
                bone_count = struct.unpack_from('>I', d, 4)[0]
                null = d[8:].find(b'\x00')
                name = d[8:8+null].decode('ascii', errors='replace') if null > 0 else '?'
                r = _CSS + f'<div class="hdr">Character Skeleton — {name}</div>'
                rows = [('Character', f'<span class="val">{name}</span>'),
                        ('Bones', f'<span class="val">{bone_count}</span>'),
                        ('Morphs/Capsules', f'<span class="val">{morph_count}</span>')]
                # Parse bone names if possible
                name_end = ((8+null+1)+3)&~3
                off = name_end + morph_count * 72  # skip morph entries
                off += bone_count * 4  # skip parent indices
                off += bone_count * 28  # skip inverse bind
                bone_names = []
                for bi in range(bone_count):
                    if off >= len(d): break
                    bnull = d[off:].find(b'\x00')
                    if bnull < 0: break
                    bname = d[off:off+bnull].decode('ascii', errors='replace')
                    bone_names.append(bname)
                    off = ((off+bnull+1)+3)&~3
                if bone_names:
                    r += '<div class="sub" style="margin-top:6px">Bone hierarchy:</div>'
                    # Read parent indices
                    poff = name_end + morph_count * 72
                    parents = []
                    for bi in range(bone_count):
                        if poff + 4 <= len(d):
                            parents.append(struct.unpack_from('>I', d, poff)[0])
                            poff += 4
                    brows = []
                    for bi, bn in enumerate(bone_names):
                        par = parents[bi] if bi < len(parents) else 0
                        par_name = bone_names[par] if par < len(bone_names) else '?'
                        brows.append([f'{bi}', f'<span class="val">{bn}</span>',
                                     f'{par_name}' if bi != 0 else '(root)'])
                    r += _tbl(['#', 'Bone', 'Parent'], brows)
                r += _tbl(['Field', 'Value'], rows)
                return r
            else:
                n0 = struct.unpack_from('>I', d, 0)[0]
                n1 = struct.unpack_from('>I', d, 4)[0] if len(d) >= 8 else 0
                strings = _find_strings(d, 3, min(len(d), 200))
                names = [s for _, s in strings]
                r = _CSS + f'<div class="hdr">Node Hierarchy</div>'
                r += f'<div class="sub">unk={c["unk"]} · Fields: {n0}, {n1}</div>'
                if names:
                    r += _tbl(['Name'], [(n,) for n in names[:20]])
                return r

        # ── FCSR: File Container ──
        elif cid == 'FCSR':
            if len(d) < 16: return None
            purpose = struct.unpack_from('>I', d, 0)[0]
            subtype = struct.unpack_from('>I', d, 4)[0]
            fsize = struct.unpack_from('>I', d, 8)[0]
            null = d[12:].find(b'\x00')
            fname = d[12:12+null].decode('ascii', errors='replace') if null > 0 else '?'
            # Ghidra: Chunk_ResourceFile::Process (line 77994)
            PURPOSE = {0:'Model/Generic',1:'(Skip)',2:'Texture+Flags',3:'Sound',
                       4:'Sound Properties',5:'Sound ID Mapping',6:'Localized Resource'}
            r = _CSS + f'<div class="hdr">File Container — {fname}</div>'
            r += _tbl(['Field', 'Value'], [
                ('Filename', f'<span class="path">{fname}</span>'),
                ('File Size', f'<span class="val">{fsize:,}</span> bytes'),
                ('Purpose', f'{purpose} — {PURPOSE.get(purpose, "Unknown")}'),
                ('Sub-type', f'{subtype}')
            ])
            return r

        # ── GSMS: Scripts ──
        elif cid == 'GSMS':
            if len(d) < 8: return None
            n_groups = struct.unpack_from('>I', d, 0)[0]
            if n_groups < 1 or n_groups > 500: return None
            counts = [d[4+i] for i in range(min(n_groups, len(d)-4))]
            total = sum(counts)
            ds = (4+n_groups+3)&~3
            r = _CSS + f'<div class="hdr">Level Script — {total} messages in {n_groups} events</div>'
            r += f'<div class="sub">Messages per event: {counts}</div>'
            _OPS = {0:'NOP',1:'CREATE',2:'DESTROY',3:'ENABLE',4:'DISABLE',5:'ACTIVATE',
                   6:'DEACTIVATE',7:'RESET',8:'ATTACH',0xC:'SET_ANIM',0x12:'SPAWN',
                   0x14:'DESPAWN',0x18:'SET_PARENT',0x1B:'CHECKPOINT',0x1C:'LEVEL_SECTION',
                   0x1E:'SET_PROPERTY',0x28:'TELEPORT',0x30:'STOP',0x3D:'SET_SPEED',
                   0x3E:'TRIGGER_GRP',0x3F:'SEND_TRIGGER',0x53:'PLAY_DIALOGUE',
                   0x80:'SET_TIMER',0x8B:'PLAY_SOUND',0x100:'CAMERA_CUT',
                   0x186:'DLG_START',0x187:'DLG_STOP',0x188:'DLG_PAUSE',
                   0x189:'SUBTITLE',0x18A:'PLAY_VO',0x18B:'STOP_VO',
                   0x190:'OBJ_COMPLETE',0x191:'SET_OBJECTIVE',0x200:'SET_FLAG',
                   0x300:'PLAY_ANIM',0x400:'STOP_ANIM',0x500:'SET_POSE',
                   0x800:'SET_HEALTH',0xC00:'SPEED_MULT',0x1800:'CAM_SHAKE',0xEC00:'PLAY_FMV',
                   0x8000:'SERVER_MSG',0x8003:'PICKUP',0x8005:'DAMAGE',
                   0x8010:'FORCE_ANIM',0x8012:'SPAWN_ACTOR',0x801A:'PHYS_IMPULSE',
                   0x801E:'PROP_ACTION',0x8023:'CHECKPOINT',0x8029:'SET_VISIBILITY',
                   0x802F:'SET_SPAWN_PT',0x8030:'CLEAR_SPAWN'}
            rows = []; off = ds + 1
            for gi in range(n_groups):
                cnt = counts[gi] if gi < len(counts) else 0
                for mi in range(cnt):
                    if off+20 > len(d): break
                    op = struct.unpack_from('>H', d, off)[0]
                    flags = struct.unpack_from('>H', d, off+2)[0]
                    eid = struct.unpack_from('>I', d, off+4)[0]
                    p2f = struct.unpack_from('>f', d, off+12)[0]
                    off += 20
                    if 0 < flags <= 256:
                        aligned = (flags+3)&~3
                        if off+aligned <= len(d): off += aligned
                    opname = _OPS.get(op, f'0x{op:04X}')
                    estr = f'<span class="val">0x{eid:06X}</span>' if eid else '—'
                    p2s = f'{p2f:.2f}' if 0.001<abs(p2f)<10000 and p2f==p2f else ''
                    rows.append([f'{gi}', f'<span class="val">{opname}</span>', estr,
                                f'{p2s}' if p2s and p2s != '1.00' else ''])
            r += _tbl(['Event', 'Message', 'Entity', 'Param'], rows)
            return r

        # ── NACH: Animation Channels ──
        elif cid == 'NACH':
            # Ghidra: Asura_Chunk_Hierarchy_CompressedAnim::Process
            # Extended header 0x2C (44 bytes): 28 bytes after standard 16
            # content[0]=nBones, [4]=field_14, [8]=field_18, [12]=field_1C,
            # [16]=nUniqueQuats, [20]=nUniquePositions, [24]=nSoundEvents
            flags = c['unk']
            if len(d) < 28: return _CSS + '<div class="hdr">Animation Channel</div><div class="sub">Too small to decode</div>'
            n_bones = struct.unpack_from('>I', d, 0)[0]
            field_14 = struct.unpack_from('>I', d, 4)[0]
            field_18 = struct.unpack_from('>I', d, 8)[0]
            field_1c = struct.unpack_from('>I', d, 12)[0]
            n_unique_quats = struct.unpack_from('>I', d, 16)[0]
            n_unique_pos = struct.unpack_from('>I', d, 20)[0]
            n_sound_events = struct.unpack_from('>I', d, 24)[0]
            null = d[28:].find(b'\x00')
            name = d[28:28+null].decode('ascii', errors='replace') if null > 0 else '?'
            name_end = ((28 + null + 1) + 3) & ~3
            # Decode flags
            flag_parts = []
            if flags & 0x02: flag_parts.append('LOOP')
            if flags & 0x10: flag_parts.append('ROOT_MOTION')
            if flags & 0x20: flag_parts.append('PRE_PACKED')
            if flags & 0x200: flag_parts.append('FLAG_0x200')
            if flags & 0x400: flag_parts.append('FLAG_0x400')
            flags_str = ' | '.join(flag_parts) if flag_parts else 'none'
            r = _CSS + f'<div class="hdr">Animation — {name}</div>'
            r += _tbl(['Field', 'Value'], [
                ('Name', f'<span class="val">{name}</span>'),
                ('Version', f'<span class="val">{ver}</span>'),
                ('Flags', f'<span class="val">0x{flags:04X}</span> ({flags_str})'),
                ('Bones', f'<span class="val">{n_bones}</span>'),
                ('Unique Quaternions', f'<span class="val">{n_unique_quats:,}</span>'),
                ('Unique Positions', f'<span class="val">{n_unique_pos:,}</span>'),
                ('Sound Events', f'<span class="val">{n_sound_events}</span>'),
                ('Field 0x14', f'<span class="dim">{field_14}</span>'),
                ('Field 0x18', f'<span class="dim">{field_18}</span>'),
                ('Field 0x1C', f'<span class="dim">{field_1c}</span>'),
            ])
            # Parse bone table
            has_root_motion = bool(flags & 0x10)
            n_bone_entries = (n_bones + 1) if has_root_motion else n_bones
            off = name_end
            if off + n_bone_entries * 4 <= len(d):
                animated = []
                for bi in range(n_bone_entries):
                    nrk = struct.unpack_from('>h', d, off + bi*4)[0]
                    fri = struct.unpack_from('>h', d, off + bi*4 + 2)[0]
                    if nrk > 0: animated.append((bi, nrk, fri))
                if animated:
                    label = 'root_motion' if has_root_motion else ''
                    r += f'<div class="sub" style="margin-top:6px">Animated bones ({len(animated)}/{n_bone_entries}):</div>'
                    rows = [(f'{bi}{"*" if bi==0 and has_root_motion else ""}', f'{nk}', f'{fi}') for bi, nk, fi in animated[:30]]
                    r += _tbl(['Bone #', 'Rot Keys', 'First Idx'], rows)
            return r

        # ── DOME: Level Sections ──
        elif cid == 'DOME':
            n = struct.unpack_from('>I', d, 0)[0] if len(d) >= 4 else 0
            r = _CSS + f'<div class="hdr">Level Sections — {n} entries</div>'
            r += f'<div class="sub">Version: {ver} · {len(d):,} bytes</div>'
            # Format: nSections + level_name(null,4-align) + per-section: name(null,4-align) + 96 bytes data
            null = d[4:].find(b'\x00')
            level_name = d[4:4+null].decode('ascii', errors='replace') if null > 0 else '?'
            off2 = ((4 + null + 1) + 3) & ~3
            r += f'<div class="sub">Level: <b>{level_name}</b></div>'
            rows = []
            for i in range(min(n, 400)):
                if off2 >= len(d): break
                snull = d[off2:off2+128].find(b'\x00')
                if snull < 0: break
                try: sname = d[off2:off2+snull].decode('ascii')
                except: sname = '?'
                off2 = ((off2 + snull + 1) + 3) & ~3
                pos = '—'
                if off2 + 96 <= len(d):
                    fx, fy, fz = struct.unpack_from('>fff', d, off2)
                    if all(abs(v) < 100000 and v == v for v in (fx, fy, fz)):
                        pos = f'({fx:.1f}, {-fy:.1f}, {-fz:.1f})'
                    off2 += 96
                rows.append([f'{i}', f'<span class="val">{sname}</span>', pos])
            r += _tbl(['#', 'Section Name', 'Center Position'], rows)
            return r

        # ── NEHP: Physics Collision ──
        elif cid == 'NEHP':
            # Ghidra: Physics_Zone::Process (line 77587)
            # Header: nSectors(u32), nVertices(u32), nFaces(u32), then more counts
            # Sector = 0x38 (56) bytes, Vertex = 0x0C (12) bytes, Face = 8 bytes
            n = struct.unpack_from('>I', d, 0)[0]
            nv = struct.unpack_from('>I', d, 4)[0] if len(d) >= 8 else 0
            nf = struct.unpack_from('>I', d, 8)[0] if len(d) >= 12 else 0
            r = _CSS + f'<div class="hdr">Physics Collision Mesh</div>'
            rows = [('Sections', f'<span class="val">{n}</span>'),
                    ('Total Vertices', f'<span class="val">{nv:,}</span> (12 bytes each: float32 XYZ)'),
                    ('Total Faces', f'<span class="val">{nf:,}</span> (8 bytes each)')]
            if len(d) >= 24:
                ne = struct.unpack_from('>I', d, 12)[0]
                nm = struct.unpack_from('>I', d, 16)[0]
                flags = struct.unpack_from('>I', d, 20)[0]
                rows.append(('Edges', f'{ne:,}'))
                rows.append(('Material Refs', f'{nm:,} (int16 → Material ConvertIndex)'))
                rows.append(('Flags', f'0x{flags:08X}'))
            rows.append(('Sector size', '56 bytes (nVerts, nFaces, vOff, fOff, BBox[24], matGroup, flags, u16+pad)'))
            rows.append(('Data Size', f'{len(d):,} bytes'))
            # Parse first few sectors if data available
            sec_off = 24 if len(d) >= 24 else 12
            if n > 0 and sec_off + 56 <= len(d):
                sec_rows = []
                for si in range(min(n, 8)):
                    so = sec_off + si * 56
                    if so + 56 > len(d): break
                    sv = struct.unpack_from('>I', d, so)[0]
                    sf = struct.unpack_from('>I', d, so+4)[0]
                    bb = [struct.unpack_from('>f', d, so+16+j*4)[0] for j in range(6)]
                    sec_rows.append((f'#{si}', f'{sv} verts, {sf} faces',
                                     f'({bb[0]:.1f},{bb[1]:.1f},{bb[2]:.1f})→({bb[3]:.1f},{bb[4]:.1f},{bb[5]:.1f})'))
                if sec_rows:
                    r += f'<div class="hdr" style="margin-top:8px">Sectors (first {len(sec_rows)})</div>'
                    r += _tbl(['#', 'Geometry', 'Bounding Box'], sec_rows)
            r += _tbl(['Field', 'Value'], rows)
            return r

        # ── 1VAN: Navigation Mesh ──
        elif cid == '1VAN':
            # Ghidra: Asura_Chunk_Navigation::Process — validated stride=21 across all levels
            # NavVertex on-disk: float3 pos(12) + float radius(4) + u8 nConns(1) + u8 zone(1) + u16 flags(2) + pad(1) = 21
            # NavConnection: u16 target(2) + u16 flags(2) + float cost(4) = 8
            nv = struct.unpack_from('>I', d, 0)[0]
            n_cp = struct.unpack_from('>I', d, 4)[0]
            r = _CSS + f'<div class="hdr">Navigation Mesh</div>'
            stride = 21
            rows = [('Version', f'{ver}'),
                    ('Vertices', f'<span class="val">{nv:,}</span>'),
                    ('Coverpoints', f'<span class="val">{n_cp}</span>')]
            vert_end = 8 + nv * stride
            nc = 0
            if vert_end + 4 <= len(d):
                nc = struct.unpack_from('>I', d, vert_end)[0]
                rows.append(('Connections', f'<span class="val">{nc:,}</span>'))
            rows.append(('Data Size', f'{len(d):,} bytes'))
            # Zone statistics
            if nv > 0:
                zones = set()
                for vi in range(nv):
                    vo = 8 + vi * stride
                    if vo + stride > len(d): break
                    zones.add(d[vo + 17])
                rows.append(('Zones', f'{sorted(zones)}'))
            # Parse first few vertices
            if nv > 0 and 8 + stride <= len(d):
                vrows = []
                for vi in range(min(nv, 15)):
                    vo = 8 + vi * stride
                    if vo + stride > len(d): break
                    px, py, pz = struct.unpack_from('>fff', d, vo)
                    rad = struct.unpack_from('>f', d, vo+12)[0]
                    n_c = d[vo+16]; zone = d[vo+17]
                    vf = struct.unpack_from('>H', d, vo+18)[0]
                    vrows.append((f'{vi}', f'({px:.1f}, {py:.1f}, {pz:.1f})',
                                  f'{rad:.2f}', f'{n_c}', f'{zone}', f'0x{vf:04X}'))
                r += _tbl(['#', 'Position', 'Radius', 'Conns', 'Zone', 'Flags'], vrows)
            # Parse first few connections
            if nc > 0 and vert_end + 4 + 8 <= len(d):
                crows = []
                co = vert_end + 4
                for ci in range(min(nc, 10)):
                    if co + 8 > len(d): break
                    target = struct.unpack_from('>H', d, co)[0]
                    cflags = struct.unpack_from('>H', d, co+2)[0]
                    cost = struct.unpack_from('>f', d, co+4)[0]
                    crows.append((f'{ci}', f'→ {target}', f'0x{cflags:04X}', f'{cost:.2f}'))
                    co += 8
                if crows:
                    r += f'<div class="hdr" style="margin-top:8px">Connections (first {len(crows)})</div>'
                    r += _tbl(['#', 'Target', 'Flags', 'Cost'], crows)
            r += _tbl(['Field', 'Value'], rows)
            return r

        # ── Particle FX types ──
        elif cid in ('TPXF', 'TSXF', 'TEXF'):
            names = {'TPXF': 'Particle Source FX', 'TSXF': 'Texture Source FX', 'TEXF': 'Emitter FX'}
            h = struct.unpack_from('>I', d, 0)[0]
            null = d[4:].find(b'\x00')
            name = d[4:4+null].decode('ascii', errors='replace') if null > 0 else '?'
            off = ((4+null+1)+3)&~3
            r = _CSS + f'<div class="hdr">{names[cid]} — {name}</div>'
            rows = [('Name', f'<span class="val">{name}</span>'),
                    ('Hash', f'<span class="val">0x{h:08X}</span>')]
            if off + 8 <= len(d):
                count = struct.unpack_from('>I', d, off+4)[0]
                rows.append(('Parameters', f'{count} entries, {len(d)-off} bytes'))
            r += _tbl(['Field', 'Value'], rows)
            return r

        # ── RTTC/CATC/VETC: Controllers ──
        elif cid in ('RTTC', 'CATC', 'VETC'):
            cnames = {'RTTC':'Trigger Controller','CATC':'Camera Trigger','VETC':'Entity Controller'}
            r = _CSS + f'<div class="hdr">{cnames[cid]}</div>'
            rows = [('Version', f'{ver}'), ('Size', f'{len(d):,} bytes')]
            # Parse header u32s
            if len(d) >= 8:
                v0 = struct.unpack_from('>I', d, 0)[0]
                v1 = struct.unpack_from('>I', d, 4)[0]
                rows.append(('Type/Count', f'<span class="val">{v0}</span>'))
                rows.append(('Flags', f'<span class="val">0x{v1:08X}</span>'))
            # Find entity name
            strings = _find_strings(d, 3, min(len(d), 200))
            if strings:
                rows.insert(0, ('Entity', f'<span class="val">{strings[0][1]}</span>'))
            # Parse floats (positions, radii etc.)
            float_vals = []
            for j in range(0, min(len(d), 64), 4):
                fv = struct.unpack_from('>f', d, j)[0]
                if 0.01 < abs(fv) < 10000 and fv == fv:
                    float_vals.append((j, fv))
            if float_vals:
                fstr = ', '.join(f'{fv:.2f}' for _, fv in float_vals[:6])
                rows.append(('Parameters', f'<span class="dim">{fstr}</span>'))
            # CATC: show camera entity refs
            if cid == 'CATC' and len(d) >= 20:
                ref1 = struct.unpack_from('>I', d, 16)[0]
                ref2 = struct.unpack_from('>I', d, 20)[0] if len(d) >= 24 else 0
                if ref1 > 0 and ref1 < 0x100000:
                    rows.append(('Entity Ref 1', f'<span class="val">0x{ref1:08X}</span>'))
                if ref2 > 0 and ref2 < 0x100000:
                    rows.append(('Entity Ref 2', f'<span class="val">0x{ref2:08X}</span>'))
            r += _tbl(['Field', 'Value'], rows)
            return r

        # ── PMIU: GUI Menu ──
        elif cid == 'PMIU':
            strings = _find_strings(d, 2, len(d))
            names = [s for _, s in strings]
            menu_name = names[0] if names else '?'
            r = _CSS + f'<div class="hdr">GUI Menu — {menu_name}</div>'
            r += f'<div class="sub">{len(names)} widgets · {len(d):,} bytes</div>'
            if len(names) > 1:
                # Categorize widgets
                buttons = [n for n in names[1:] if 'btn' in n.lower() or 'button' in n.lower()]
                texts = [n for n in names[1:] if 'text' in n.lower() or 'label' in n.lower() or 'txt' in n.lower()]
                images = [n for n in names[1:] if 'img' in n.lower() or 'icon' in n.lower() or 'pic' in n.lower()]
                others = [n for n in names[1:] if n not in buttons and n not in texts and n not in images]
                rows = [(f'<span class="val">{n}</span>',
                        'Button' if n in buttons else ('Text' if n in texts else ('Image' if n in images else '')))
                       for n in names[1:30]]
                r += _tbl(['Widget', 'Type'], rows)
            return r

        # ── LFSR: Resource File List ──
        elif cid == 'LFSR':
            r = _CSS + f'<div class="hdr">Resource File List</div>'
            if len(d) >= 4:
                n_entries = struct.unpack_from('>I', d, 0)[0]
                r += f'<div class="sub">{n_entries} entries · {len(d):,} bytes</div>'
                # Parse entries: null-terminated path + padding + metadata
                paths = []; off = 4
                for i in range(min(n_entries, 500)):
                    if off >= len(d): break
                    null = d[off:].find(b'\x00')
                    if null <= 0: break
                    path = d[off:off+null].decode('ascii', errors='replace')
                    paths.append(path)
                    off = ((off+null+1)+3)&~3
                    # Skip metadata bytes (variable, typically 8-12 bytes)
                    while off < len(d) and off < len(d) - 4:
                        # Next entry starts with a printable ASCII char
                        if 0x20 < d[off] < 0x7F: break
                        off += 1
                if paths:
                    # Group by directory
                    from collections import defaultdict
                    dirs = defaultdict(list)
                    for p in paths:
                        parts = p.replace('\\', '/').split('/')
                        d_name = parts[0] if len(parts) > 1 else '(root)'
                        dirs[d_name].append(parts[-1])
                    rows = []
                    for d_name in sorted(dirs.keys()):
                        files = dirs[d_name]
                        rows.append([f'<b>{d_name}</b>', f'{len(files)} files',
                                    ', '.join(f[:25] for f in files[:5]) + ('...' if len(files) > 5 else '')])
                    r += _tbl(['Directory', 'Count', 'Files'], rows)
            else:
                null = d.find(b'\x00')
                path = d[:null].decode('ascii', errors='replace') if null > 0 else '?'
                r += f'<div class="sub"><span class="path">{path}</span></div>'
            return r

        # ── SUMM: Level Summary ──
        elif cid == 'SUMM':
            n = struct.unpack_from('>I', d, 0)[0] if len(d) >= 4 else 0
            per_sec = (len(d) - 4) // n if n > 0 else 0
            r = _CSS + f'<div class="hdr">Level Summary — {n} sections</div>'
            r += f'<div class="sub">{per_sec} bytes per section · {len(d):,} total</div>'
            rows = []
            for si in range(n):
                off = 4 + si * per_sec
                null = d[off:off+per_sec].find(b'\x00')
                name = d[off:off+null].decode('ascii','replace') if null > 0 else '?'
                hoff = ((off+null+1)+3)&~3
                hash_val = struct.unpack_from('>I', d, hoff)[0] if hoff+4 <= len(d) else 0
                # Find music path string within this section
                music = ''
                for j in range(off, min(off+per_sec, len(d))-6):
                    if d[j:j+7] in (b'Sounds\\', b'sounds\\'):
                        end = d[j:].find(b'\x00')
                        if end > 0: music = d[j:j+end].decode('ascii','replace')
                        break
                music_str = f'<span class="path">{music}</span>' if music else '—'
                rows.append([f'{si}', f'<span class="val">{name}</span>',
                            f'<span class="dim">0x{hash_val:08X}</span>', music_str])
            r += _tbl(['#', 'Section', 'Hash', 'Music'], rows)
            return r

        # ── OFNF: File Info ──
        elif cid == 'OFNF':
            sz = struct.unpack_from('>I', d, 0)[0]
            v2 = struct.unpack_from('>I', d, 4)[0] if len(d) >= 8 else 0
            return _CSS + f'<div class="hdr">File Info</div>' + _tbl(['Field','Value'], [
                ('Decompressed Size', f'<span class="val">{sz:,}</span> bytes'),
                ('Flags', f'<span class="val">0x{v2:08X}</span>')
            ])

        # ── TPMH: Morph Targets ──
        elif cid == 'TPMH':
            strings = _find_strings(d, 2, len(d))
            names = [s for _, s in strings]
            char_name = names[0] if names else '?'
            # First u32 might be count
            count = struct.unpack_from('>I', d, 0)[0] if len(d) >= 4 else 0
            r = _CSS + f'<div class="hdr">Morph Target — {char_name}</div>'
            r += f'<div class="sub">{count} targets · {len(names)-1} bone attachments</div>'
            if len(names) > 1:
                bone_names = names[1:]
                rows = [(f'{i}', f'<span class="val">{n}</span>') for i, n in enumerate(bone_names[:20])]
                r += _tbl(['#', 'Bone/Attachment'], rows)
            return r

        # ── NAXT: UV Animation ──
        elif cid == 'NAXT':
            null = d[:64].find(b'\x00')
            name = d[:null].decode('ascii', errors='replace') if null and null > 0 else '?'
            r = _CSS + f'<div class="hdr">UV Animation — {name}</div>'
            r += f'<div class="sub">Version: {ver} · {len(d):,} bytes</div>'
            off2 = ((null + 1) + 3) & ~3 if null and null > 0 else 4
            if off2 + 4 <= len(d):
                n_layers = struct.unpack_from('>I', d, off2)[0]
                r += f'<div class="sub">Layers: {n_layers}</div>'
                off2 += 4
                rows = []
                for li in range(n_layers):
                    if off2 + 8 > len(d): break
                    vals = []
                    for fi in range(min(6, (len(d) - off2) // 4)):
                        vals.append(struct.unpack_from('>f', d, off2 + fi*4)[0])
                    u_spd = f'{vals[0]:.3f}' if vals else '?'
                    v_spd = f'{vals[1]:.3f}' if len(vals) > 1 else '?'
                    extra = ', '.join(f'{v:.3f}' for v in vals[2:]) if len(vals) > 2 else ''
                    rows.append([f'{li}', f'<span class="val">{u_spd}</span>',
                                f'<span class="val">{v_spd}</span>', extra])
                    off2 += 24
                r += _tbl(['Layer', 'U Speed', 'V Speed', 'Extra Params'], rows)
            return r

        # ── Remaining simple types ──
        elif cid == 'BABL':
            # Ghidra: AABB_Tree_Node::ReadFromChunkStream (line 23430)
            # Node = 12 bytes: 6×u8 compressed AABB + u16 left + u16 right + u8 flags + 1 pad
            n = struct.unpack_from('>I', d, 0)[0]
            r = _CSS + f'<div class="hdr">AABB Spatial Tree — {n} entries</div>'
            if len(d) >= 28:
                bb = [struct.unpack_from('>f', d, 4+i*4)[0] for i in range(6)]
                r += f'<div class="sub">Root bounds: ({bb[0]:.1f}, {bb[1]:.1f}, {bb[2]:.1f}) → ({bb[3]:.1f}, {bb[4]:.1f}, {bb[5]:.1f})</div>'
            r += f'<div class="sub">Node format: 12 bytes (6×u8 AABB + u16 left + u16 right + u8 flags + pad)</div>'
            # Parse first few nodes after header (header = 4 + 24 = 28 bytes)
            node_off = 28
            if n > 0 and node_off + 12 <= len(d):
                nrows = []
                for ni in range(min(n, 12)):
                    no = node_off + ni * 12
                    if no + 12 > len(d): break
                    aabb = [d[no+j] for j in range(6)]
                    left = struct.unpack_from('>H', d, no+6)[0]
                    right = struct.unpack_from('>H', d, no+8)[0]
                    flags = d[no+10]
                    lstr = f'{left}' if left != 0xFFFF else '—'
                    rstr = f'{right}' if right != 0xFFFF else '—'
                    nrows.append((f'#{ni}', f'({aabb[0]},{aabb[1]},{aabb[2]})→({aabb[3]},{aabb[4]},{aabb[5]})',
                                  lstr, rstr, f'0x{flags:02X}'))
                r += _tbl(['#', 'AABB (compressed)', 'Left', 'Right', 'Flags'], nrows)
            return r
        elif cid == 'DNER':
            strings = _find_strings(d, 3, len(d))
            paths = [s for _, s in strings if '\\' in s or '/' in s]
            r = _CSS + f'<div class="hdr">Render Environment</div>'
            if paths: r += f'<div class="sub">Spheremap: <span class="path">{paths[0]}</span></div>'
            return r
        elif cid == 'NAIU':
            strings = _find_strings(d, 2, len(d))
            names = [s for _, s in strings]
            r = _CSS + f'<div class="hdr">UI Animation — {names[0] if names else "?"}</div>'
            r += f'<div class="sub">{len(names)} keyframe sequences · {len(d):,} bytes</div>'
            if len(names) > 1:
                r += _tbl(['Sequence'], [(f'<span class="val">{n}</span>',) for n in names[:30]])
            return r
        elif cid == 'NSIG':
            # Parse AI signal names
            strings = _find_strings(d, 3, len(d))
            r = _CSS + f'<div class="hdr">AI Signal Config</div>'
            r += f'<div class="sub">Version: {ver} · {len(d):,} bytes</div>'
            if strings:
                r += _tbl(['Offset', 'Signal'], [(f'0x{off:04X}', f'<span class="val">{s}</span>') for off, s in strings[:20]])
            return r
        elif cid == 'NILM':
            n = struct.unpack_from('>I', d, 0)[0]
            r = _CSS + f'<div class="hdr">Lightmap Data — {n} sections</div>'
            r += f'<div class="sub">{len(d):,} bytes total · ~{(len(d)-4)//max(1,n):,} bytes/section</div>'
            return r
        elif cid == 'LBTA':
            n = struct.unpack_from('>I', d, 0)[0]
            r = _CSS + f'<div class="hdr">Animation Blend Table — {n} entries</div>'
            r += f'<div class="sub">Version: {ver} · {len(d):,} bytes</div>'
            # Parse blend entries if possible
            if len(d) > 4:
                strings = _find_strings(d, 3, len(d))
                if strings:
                    r += _tbl(['Offset', 'Name'], [(f'0x{off:04X}', f'<span class="val">{s}</span>') for off, s in strings[:20]])
            return r
        elif cid == 'XETA':
            r = _CSS + f'<div class="hdr">Texture Animation Data</div>'
            r += f'<div class="sub">Version: {ver} · {len(d):,} bytes</div>'
            strings = _find_strings(d, 3, len(d))
            if strings:
                r += _tbl(['Offset', 'Name'], [(f'0x{off:04X}', f'<span class="val">{s}</span>') for off, s in strings[:15]])
            # Show header values
            if len(d) >= 16:
                vals = [struct.unpack_from('>I', d, j*4)[0] for j in range(min(4, len(d)//4))]
                r += '<div class="sub" style="margin-top:4px">Header:</div>'
                r += _tbl(['Offset', 'Value'], [(f'0x{j*4:02X}', f'0x{v:08X}') for j, v in enumerate(vals)])
            return r
        elif cid in ('TRTA','BVRM','HPDS','ANRC','gulp'):
            labels = {'TRTA':'Trigger/Animation Data','BVRM':'Render Volume',
                      'HPDS':'Physics Shape Data','ANRC':'Crowd/NPC Config','gulp':'Streaming Marker'}
            n = struct.unpack_from('>I', d, 0)[0] if len(d) >= 4 else 0
            r = _CSS + f'<div class="hdr">{labels.get(cid, cid)}</div>'
            r += f'<div class="sub">Count: {n} · Version: {ver} · {len(d):,} bytes</div>'
            strings = _find_strings(d, 3, min(len(d), 500))
            if strings:
                r += _tbl(['Offset', 'String'], [(f'0x{off:04X}', f'<span class="val">{s}</span>') for off, s in strings[:10]])
            return r
        elif cid == 'PAHS':
            h = struct.unpack_from('>I', d, 0)[0]
            n1 = struct.unpack_from('>I', d, 4)[0]
            n2 = struct.unpack_from('>I', d, 8)[0]
            r = _CSS + f'<div class="hdr">Physics Shape</div>'
            r += _tbl(['Field','Value'], [('Hash', f'0x{h:08X}'), ('Count 1', f'{n1}'), ('Count 2', f'{n2}')])
            strings = _find_strings(d, 5, len(d))
            asr = [s for _, s in strings if '.asr' in s.lower()]
            if asr: r += f'<div class="sub"><span class="path">{asr[0]}</span></div>'
            return r
        elif cid == 'DPHS':
            h = struct.unpack_from('>I', d, 0)[0]
            n = struct.unpack_from('>I', d, 4)[0] if len(d) >= 8 else 0
            return _CSS + f'<div class="hdr">Physics Shape Descriptor</div>' + _tbl(['Field','Value'], [('Hash', f'0x{h:08X}'), ('Count', f'{n}')])

        # ── gulp: Player Upgrades ──
        elif cid == 'gulp':
            # Ghidra: Simp_ChunkLoading_System - PlayerUpgrades
            r = _CSS + '<div class="hdr">Player Upgrades</div>'
            if len(d) >= 20:
                upgrade_bits = struct.unpack_from('>I', d, 0)[0]
                vals = struct.unpack_from('>4I', d, 4)
                rows = [('Upgrade Bitmask', f'<span class="val">0x{upgrade_bits:08X}</span>')]
                # Decode upgrade bits
                upgrades = []
                names = {0:'Homer_HB', 1:'Homer_Ball', 2:'Lisa_HoB', 3:'Lisa_Lightning',
                         4:'Bart_Cape', 5:'Bart_Slingshot', 6:'Marge_Megaphone', 7:'Marge_Maggie'}
                for bit in range(32):
                    if upgrade_bits & (1 << bit):
                        upgrades.append(names.get(bit, f'bit_{bit}'))
                if upgrades: rows.append(('Active Upgrades', ', '.join(upgrades)))
                for i, v in enumerate(vals):
                    rows.append((f'Field {i+1}', f'{v}'))
            else:
                rows = [('Size', f'{len(d)} bytes')]
            r += _tbl(['Field', 'Value'], rows)
            return r

        # ── TRTA: Attractors ──
        elif cid == 'TRTA':
            # Ghidra: Asura_Chunk_Attractors::Process
            r = _CSS + '<div class="hdr">Attractors</div>'
            if len(d) >= 8:
                field0 = struct.unpack_from('>I', d, 0)[0]
                n_entries = struct.unpack_from('>I', d, 4)[0]
                rows = [('Version', f'{ver}'), ('Base Index', f'{field0}'), ('Entries', f'{n_entries}')]
                # Parse attractor entries (each has position pair + direction)
                off = 8; arows = []
                stride = (len(d) - 8) // n_entries if n_entries > 0 else 0
                rows.append(('Entry Stride', f'{stride} bytes'))
                for i in range(min(n_entries, 15)):
                    if off + 52 > len(d): break
                    vals = struct.unpack_from('>13f', d, off)
                    arows.append((f'{i}', f'({vals[3]:.1f}, {vals[4]:.1f}, {vals[5]:.1f})',
                                  f'({vals[6]:.1f}, {vals[7]:.1f}, {vals[8]:.1f})',
                                  f'({vals[10]:.3f}, {vals[11]:.3f}, {vals[12]:.3f})'))
                    off += stride if stride > 0 else 52
                r += _tbl(['Field', 'Value'], rows)
                if arows:
                    r += f'<div class="sub" style="margin-top:6px">Entries ({min(n_entries, 15)}/{n_entries}):</div>'
                    r += _tbl(['#', 'Pos A', 'Pos B', 'Direction'], arows)
            else:
                r += f'<div class="sub">{len(d)} bytes</div>'
            return r

        # ── ANRC: Corona (Light Glow) ──
        elif cid == 'ANRC':
            # Ghidra: Asura_Chunk_Corona::Process
            r = _CSS + '<div class="hdr">Corona / Light Glow Effects</div>'
            if len(d) >= 8:
                n_coronas = struct.unpack_from('>I', d, 0)[0]
                n_links = struct.unpack_from('>I', d, 4)[0]
                rows = [('Version', f'{ver}'), ('Coronas', f'{n_coronas}'), ('Links', f'{n_links}')]
                # Each corona entry has: u32 flags, u32 entity_ref, float size, float intensity, floats color/atten
                off = 8; crows = []
                for i in range(min(n_coronas, 10)):
                    if off + 40 > len(d): break
                    flags = struct.unpack_from('>I', d, off)[0]
                    eref = struct.unpack_from('>I', d, off+4)[0]
                    size = struct.unpack_from('>f', d, off+20)[0]
                    intensity = struct.unpack_from('>f', d, off+24)[0]
                    crows.append((f'{i}', f'0x{flags:X}', f'0x{eref:08X}', f'{size:.2f}', f'{intensity:.3f}'))
                    off += 40
                r += _tbl(['Field', 'Value'], rows)
                if crows:
                    r += f'<div class="sub" style="margin-top:6px">Corona entries:</div>'
                    r += _tbl(['#', 'Flags', 'Entity Ref', 'Size', 'Intensity'], crows)
            else:
                r += f'<div class="sub">{len(d)} bytes</div>'
            return r

        # ── EULB: Entity Blueprints ──
        elif cid == 'EULB':
            # Ghidra: Asura_Blueprint_System::ReadFromChunkStream
            # Format: int32 version, int32 nTypes, per type: hash + nBP + name + blueprints
            r = _CSS + '<div class="hdr">Entity Blueprints</div>'
            if len(d) >= 8:
                bp_ver = struct.unpack_from('>i', d, 0)[0]
                n_types = struct.unpack_from('>i', d, 4)[0]
                rows = [('Version', f'{bp_ver}'), ('Blueprint Tables', f'{n_types}')]
                off = 8
                type_rows = []
                for ti in range(n_types):
                    if off + 8 > len(d): break
                    th = struct.unpack_from('>I', d, off)[0]; off += 4
                    nb = struct.unpack_from('>i', d, off)[0]; off += 4
                    null = d[off:off+200].find(b'\x00')
                    tname = d[off:off+null].decode('ascii', errors='replace') if null > 0 else '?'
                    off = ((off + null + 1) + 3) & ~3
                    type_rows.append((f'{ti}', f'0x{th:08X}', tname, f'{nb}'))
                    # Try to read first few blueprint names
                    bp_names = []
                    for bi in range(min(nb, 8)):
                        if off + 12 > len(d): break
                        bv = struct.unpack_from('>i', d, off)[0]; off += 4
                        bh = struct.unpack_from('>I', d, off)[0]; off += 4
                        bt = struct.unpack_from('>I', d, off)[0]; off += 4
                        null = d[off:off+200].find(b'\x00')
                        bname = d[off:off+null].decode('ascii', errors='replace') if null > 0 else ''
                        off = ((off + null + 1) + 3) & ~3
                        if off + 4 > len(d): break
                        np = struct.unpack_from('>i', d, off)[0]; off += 4
                        if bname and len(bname) < 60:
                            bp_names.append(f'{bname} ({np} params)')
                        # Can't reliably skip params (variable size), so stop
                        break
                    if bp_names:
                        type_rows[-1] = type_rows[-1] + (bp_names[0],)
                r += _tbl(['Field', 'Value'], rows)
                if type_rows:
                    headers = ['#', 'Hash', 'Table Name', 'Count']
                    if any(len(tr) > 4 for tr in type_rows): headers.append('First Blueprint')
                    type_rows = [tr + ('',) * (len(headers) - len(tr)) for tr in type_rows]
                    r += _tbl(headers, type_rows)
            else:
                r += f'<div class="sub">{len(d)} bytes</div>'
            return r

        # ── TAXA: Animation Templates ──
        elif cid == 'TAXA':
            # Ghidra: Axon_Chunk_AnimTemplates::Process
            r = _CSS + '<div class="hdr">Animation Templates</div>'
            if len(d) >= 4:
                n_entries = struct.unpack_from('>I', d, 0)[0]
                rows = [('Version', f'{ver}'), ('Entries', f'{n_entries}')]
                # Scan for animation names in the data
                names = _find_strings(d, 4, min(len(d), 4000))
                anim_names = [s for _, s in names if not s.startswith('?')]
                if anim_names:
                    rows.append(('Sample Names', ', '.join(anim_names[:20])))
                    if len(anim_names) > 20: rows.append(('', f'... +{len(anim_names)-20} more'))
                r += _tbl(['Field', 'Value'], rows)
            else:
                r += f'<div class="sub">{len(d)} bytes</div>'
            return r

    except: pass
    # Fallback: show any strings found + field dump
    r = _CSS + f'<div class="hdr">{cid}</div>'
    r += f'<div class="sub">Version: {ver} · {len(d):,} bytes · No specific decoder</div>'
    strings = _find_strings(d, 3, min(len(d), 256))
    if strings:
        r += '<div class="sub" style="margin-top:4px">Strings found:</div>'
        r += _tbl(['Offset', 'String'], [(f'0x{off:04X}', f'<span class="val">{s}</span>') for off, s in strings[:15]])
    # Show first few u32 values
    if len(d) >= 16:
        vals = [struct.unpack_from('>I', d, j*4)[0] for j in range(min(8, len(d)//4))]
        r += '<div class="sub" style="margin-top:4px">Header values:</div>'
        r += _tbl(['Offset', 'u32', 'Float'], [
            (f'0x{j*4:02X}', f'0x{v:08X}',
             f'{struct.unpack_from(">f", d, j*4)[0]:.4f}' if abs(struct.unpack_from('>f', d, j*4)[0]) < 100000 else '—')
            for j, v in enumerate(vals)
        ])
    return r

class DialogueView(QWidget):
    """Displays NLLD dialogue or TXTH text entries with optional editing."""
    editApplied = Signal(str, int, bytes, bytes)  # desc, chunk_idx, old_content, new_content

    def __init__(self):
        super().__init__(); lo=QVBoxLayout(self); lo.setContentsMargins(8,8,8,8); lo.setSpacing(6)
        self.title=QLabel("Dialogue"); self.title.setObjectName("ptitle"); lo.addWidget(self.title)
        # Search + buttons row
        bar = QHBoxLayout(); bar.setSpacing(6)
        self.search=QLineEdit(); self.search.setPlaceholderText("Search..."); self.search.textChanged.connect(self._f); bar.addWidget(self.search)
        self.apply_btn = QPushButton("Apply Changes"); self.apply_btn.setFixedWidth(120)
        self.apply_btn.clicked.connect(self._apply); self.apply_btn.hide(); bar.addWidget(self.apply_btn)
        self.csv_exp_btn = QPushButton("Export CSV"); self.csv_exp_btn.setFixedWidth(90)
        self.csv_exp_btn.clicked.connect(self._export_csv); bar.addWidget(self.csv_exp_btn)
        self.csv_imp_btn = QPushButton("Import CSV"); self.csv_imp_btn.setFixedWidth(90)
        self.csv_imp_btn.clicked.connect(self._import_csv); self.csv_imp_btn.hide(); bar.addWidget(self.csv_imp_btn)
        lo.addLayout(bar)
        self.tbl=QTableWidget(); self.tbl.setAlternatingRowColors(True); self.tbl.horizontalHeader().setStretchLastSection(True)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows); lo.addWidget(self.tbl)
        self._rows=[]; self._mode='dlg'  # 'dlg' or 'txth'
        self._entries=[]  # parsed NLLD or TXTH entries (dicts)
        self._chunk_indices=[]  # index into main chunks list per entry
        self._chunks_ref=None  # reference to main window's chunk list
        self._edit_mode=False; self._modified=set()
    def set_edit_mode(self, enabled):
        self._edit_mode = enabled
        self.apply_btn.setVisible(enabled)
        self.csv_imp_btn.setVisible(enabled)
        self._pop()
    def load_dialogue(self, entries, chunks=None, chunk_indices=None):
        """Load NLLD dialogue entries. chunks/chunk_indices enable editing."""
        self._mode = 'dlg'
        self._entries = entries
        self._chunks_ref = chunks
        self._chunk_indices = chunk_indices or []
        self._modified.clear()
        self.title.setText(f"Dialogue — {len(entries)} entries"); self.tbl.setColumnCount(4)
        self.tbl.setHorizontalHeaderLabels(["#", "Sound ID", "Duration", "Subtitle"])
        self._rows = [(str(i), e['sound_id'], f"{e['duration']:.2f}", e['text']) for i, e in enumerate(entries)]
        self._pop()
    def load_text(self, entries, chunks=None, chunk_index=None):
        """Load TXTH text entries. chunks/chunk_index enables editing."""
        self._mode = 'txth'
        self._entries = entries if isinstance(entries, list) and entries and isinstance(entries[0], dict) else []
        self._chunks_ref = chunks
        self._chunk_indices = [chunk_index] if chunk_index is not None else []
        self._modified.clear()
        if not self._entries and isinstance(entries, list) and entries and isinstance(entries[0], tuple):
            # Legacy tuple format from tree: (label, hash, text)
            self._entries = [{'label': e[0], 'hash': int(e[1], 16) if isinstance(e[1], str) and e[1].startswith('0x') else 0, 'text': e[2]} for e in entries]
        self.title.setText(f"Localized Text — {len(self._entries)} entries"); self.tbl.setColumnCount(4)
        self.tbl.setHorizontalHeaderLabels(["#", "Label", "Hash", "Text"])
        self._rows = [(str(i), e.get('label', '?'), f"0x{e.get('hash', 0):08x}", e.get('text', '')) for i, e in enumerate(self._entries)]
        self._pop()
    def _pop(self):
        f=self.search.text().lower()
        rows=[r for r in self._rows if f in ' '.join(r).lower()] if f else self._rows
        self.tbl.blockSignals(True)
        self.tbl.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j, v in enumerate(row):
                # Format icon characters in text column for display (not in edit mode)
                is_text_col = (j == 3)
                display_val = str(v)
                if is_text_col and not self._edit_mode and hasattr(asura, 'format_text_with_icons'):
                    display_val = asura.format_text_with_icons(display_val)
                item = QTableWidgetItem(display_val)
                # Make text column editable in edit mode
                is_dur_col = (j == 2 and self._mode == 'dlg')  # Duration for NLLD
                if self._edit_mode and (is_text_col or is_dur_col):
                    item.setFlags(item.flags() | Qt.ItemIsEditable)
                else:
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                # Highlight modified rows
                orig_idx = int(row[0]) if row[0].isdigit() else -1
                if orig_idx in self._modified:
                    item.setForeground(QColor(0xe0, 0xa0, 0x30))
                self.tbl.setItem(i, j, item)
        self.tbl.resizeColumnsToContents()
        self.tbl.blockSignals(False)
        if not hasattr(self, '_cell_connected'):
            self.tbl.cellChanged.connect(self._on_cell_edit)
            self._cell_connected = True
    def _f(self): self._pop()
    def _on_cell_edit(self, row, col):
        if not self._edit_mode: return
        idx_item = self.tbl.item(row, 0)
        if not idx_item: return
        try: entry_idx = int(idx_item.text())
        except: return
        if entry_idx < 0 or entry_idx >= len(self._entries): return
        val = self.tbl.item(row, col).text()
        e = self._entries[entry_idx]
        changed = False
        if self._mode == 'dlg':
            if col == 3 and val != e['text']:
                e['text'] = val; changed = True
            elif col == 2:
                try:
                    new_dur = float(val)
                    if abs(new_dur - e['duration']) > 0.001:
                        e['duration'] = new_dur; changed = True
                except: pass
        elif self._mode == 'txth':
            if col == 3 and val != e.get('text', ''):
                e['text'] = val; changed = True
        if changed:
            self._modified.add(entry_idx)
            self._rows[entry_idx] = tuple(
                (str(entry_idx), e.get('sound_id', e.get('label', '?')),
                 f"{e.get('duration', 0):.2f}" if self._mode == 'dlg' else f"0x{e.get('hash', 0):08x}",
                 e.get('text', ''))[j] for j in range(4))
            self.apply_btn.setStyleSheet("background:#e0a030; color:#111;")
    def _apply(self):
        """Apply text edits back to chunks."""
        if not self._modified or not self._chunks_ref: return
        applied = 0
        if self._mode == 'dlg':
            for idx in sorted(self._modified):
                if idx >= len(self._entries) or idx >= len(self._chunk_indices): continue
                ci = self._chunk_indices[idx]
                if ci < 0 or ci >= len(self._chunks_ref): continue
                old_content = self._chunks_ref[ci]['content']
                new_content = asura.repack_nlld_chunk(self._entries[idx])
                self._chunks_ref[ci] = {**self._chunks_ref[ci], 'content': new_content,
                                         'size': len(new_content) + 16}
                self.editApplied.emit(f"Edit dialogue [{idx}] subtitle", ci, old_content, new_content)
                applied += 1
        elif self._mode == 'txth' and self._chunk_indices:
            ci = self._chunk_indices[0]
            if 0 <= ci < len(self._chunks_ref):
                old_content = self._chunks_ref[ci]['content']
                # Extract hash_seed from original
                hash_seed = struct.unpack_from('>I', old_content, 4)[0] if len(old_content) >= 8 else 0
                new_content = asura.repack_txth_chunk(self._entries, hash_seed)
                self._chunks_ref[ci] = {**self._chunks_ref[ci], 'content': new_content,
                                         'size': len(new_content) + 16}
                self.editApplied.emit(f"Edit {len(self._modified)} text entries", ci, old_content, new_content)
                applied = len(self._modified)
        self._modified.clear()
        self.apply_btn.setStyleSheet("")
        self._pop()
        self.title.setText(self.title.text().split(" — ")[0] + f" — {len(self._entries)} entries (applied {applied} changes)")
    def _export_csv(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export CSV", "", "CSV (*.csv)")
        if not path: return
        import csv
        with open(path, 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.writer(f)
            if self._mode == 'dlg':
                w.writerow(['index', 'sound_id', 'duration', 'subtitle'])
                for i, e in enumerate(self._entries):
                    txt = e['text'].replace('\u2018',"'").replace('\u2019',"'").replace('\u201C','"').replace('\u201D','"')
                    w.writerow([i, e['sound_id'], f"{e['duration']:.2f}", txt])
            else:
                w.writerow(['index', 'label', 'hash', 'text'])
                for i, e in enumerate(self._entries):
                    w.writerow([i, e.get('label',''), f"0x{e.get('hash',0):08x}", e.get('text','')])
    def _import_csv(self):
        """Import CSV to update text/subtitle values (matches by index)."""
        path, _ = QFileDialog.getOpenFileName(self, "Import CSV", "", "CSV (*.csv)")
        if not path: return
        import csv
        try:
            with open(path, 'r', encoding='utf-8-sig') as f:
                reader = csv.reader(f)
                header = next(reader, None)
                if not header: return
                # Find the text column
                text_col = -1
                for ci, h in enumerate(header):
                    if h.lower() in ('subtitle', 'text'): text_col = ci; break
                if text_col < 0:
                    QMessageBox.warning(self, "Import", "CSV must have a 'subtitle' or 'text' column"); return
                dur_col = -1
                for ci, h in enumerate(header):
                    if h.lower() == 'duration': dur_col = ci; break
                updated = 0
                for row in reader:
                    try: idx = int(row[0])
                    except: continue
                    if idx < 0 or idx >= len(self._entries): continue
                    if text_col < len(row):
                        new_text = row[text_col]
                        if new_text != self._entries[idx].get('text', ''):
                            self._entries[idx]['text'] = new_text
                            self._modified.add(idx); updated += 1
                    if dur_col >= 0 and dur_col < len(row) and self._mode == 'dlg':
                        try:
                            new_dur = float(row[dur_col])
                            if abs(new_dur - self._entries[idx].get('duration', 0)) > 0.001:
                                self._entries[idx]['duration'] = new_dur
                                self._modified.add(idx); updated += 1
                        except: pass
                # Rebuild display rows
                if self._mode == 'dlg':
                    self._rows = [(str(i), e['sound_id'], f"{e['duration']:.2f}", e['text']) for i, e in enumerate(self._entries)]
                else:
                    self._rows = [(str(i), e.get('label','?'), f"0x{e.get('hash',0):08x}", e.get('text','')) for i, e in enumerate(self._entries)]
                self._pop()
                self.apply_btn.setStyleSheet("background:#e0a030; color:#111;")
                self.title.setText(self.title.text().split(" — ")[0] + f" — {len(self._entries)} entries ({updated} updated from CSV)")
        except Exception as e:
            QMessageBox.warning(self, "Import Error", str(e))

class ScriptView(QWidget):
    editApplied = Signal(str, int, bytes, bytes)  # desc, chunk_idx, old_content, new_content

    def __init__(self):
        super().__init__(); lo=QVBoxLayout(self); lo.setContentsMargins(8,8,8,8); lo.setSpacing(4)
        self.title=QLabel("Script"); self.title.setObjectName("ptitle"); lo.addWidget(self.title)
        # Search + buttons row
        bar = QHBoxLayout(); bar.setSpacing(6)
        self.search=QLineEdit(); self.search.setPlaceholderText("Filter messages..."); self.search.textChanged.connect(self._filter); bar.addWidget(self.search)
        self.add_btn = QPushButton("+ Add"); self.add_btn.setFixedWidth(60); self.add_btn.clicked.connect(self._add_msg); self.add_btn.hide(); bar.addWidget(self.add_btn)
        self.del_btn = QPushButton("Delete"); self.del_btn.setFixedWidth(60); self.del_btn.clicked.connect(self._del_msg); self.del_btn.hide(); bar.addWidget(self.del_btn)
        self.apply_btn = QPushButton("Apply Changes"); self.apply_btn.setFixedWidth(120); self.apply_btn.clicked.connect(self._apply); self.apply_btn.hide(); bar.addWidget(self.apply_btn)
        lo.addLayout(bar)
        self.tbl=QTableWidget(); self.tbl.setAlternatingRowColors(True); self.tbl.horizontalHeader().setStretchLastSection(True)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows); lo.addWidget(self.tbl)
        self._rows=[]; self._messages=[]; self._slot_counts=[]; self._chunk=None; self._chunk_idx=-1
        self._chunks_ref=None; self._edit_mode=False; self._ver=6; self._flags=0; self._nTables=0
        self._modified=False

    def set_edit_mode(self, enabled):
        self._edit_mode = enabled
        self.add_btn.setVisible(enabled)
        self.del_btn.setVisible(enabled)
        self.apply_btn.setVisible(enabled)
        self._populate()

    def load_gsms(self, chunk, em, chunks=None, chunk_idx=-1):
        g = chunk['content']; self._ver = chunk['ver']; self._flags = chunk.get('unk', 0)
        self._chunk = chunk; self._chunk_idx = chunk_idx; self._chunks_ref = chunks
        self._nTables = struct.unpack_from('>I', g, 4)[0] if self._ver >= 2 and len(g) >= 8 else 0
        self._messages, self._slot_counts = asura.parse_gsms_messages(g, self._ver, self._flags)
        self._modified = False
        if not self._messages: self.title.setText("GSMS — empty"); return
        self.title.setText(f"GSMS — {len(self._messages)} messages in {len(self._slot_counts)} slots (ver {self._ver})")
        hd = ['#', 'Slot', 'Opcode', 'Entity GUID', 'Delay', 'Param', 'Extra', 'Name']
        self.tbl.setColumnCount(len(hd)); self.tbl.setHorizontalHeaderLabels(hd)
        self._build_rows()
        self._populate()

    def _build_rows(self):
        self._rows = []
        for i, m in enumerate(self._messages):
            opname = asura.GSMS_OPCODE_NAMES.get(m['opcode'], f"0x{m['opcode']:04X}")
            guid_s = f"0x{m['guid']:08X}" if m['guid'] else '0x00000000'
            delay_s = f"{m['delay']:.2f}" if m['delay'] else '0.00'
            param_s = f"{m['param']:.4f}" if m['param'] else '0.0000'
            extra_s = f"0x{m['extra']:08X}" if m['extra'] else '0x00000000'
            self._rows.append((str(i), str(m['slot']), opname, guid_s, delay_s, param_s, extra_s, m['name']))

    def _populate(self):
        f=self.search.text().lower()
        rows=[r for r in self._rows if f in ' '.join(r).lower()] if f else self._rows
        self.tbl.blockSignals(True)
        self.tbl.setRowCount(len(rows))
        editable_cols = {4, 5, 6, 7} if self._edit_mode else set()  # Delay, Param, Extra, Name
        for i, row in enumerate(rows):
            for j, v in enumerate(row):
                item = QTableWidgetItem(v)
                if j in editable_cols:
                    item.setFlags(item.flags() | Qt.ItemIsEditable)
                else:
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.tbl.setItem(i, j, item)
        self.tbl.resizeColumnsToContents()
        self.tbl.blockSignals(False)
        if not hasattr(self, '_gsms_cell_connected'):
            self.tbl.cellChanged.connect(self._on_cell_edit)
            self._gsms_cell_connected = True

    def _filter(self): self._populate()

    def _on_cell_edit(self, row, col):
        if not self._edit_mode: return
        idx_item = self.tbl.item(row, 0)
        if not idx_item: return
        try: mi = int(idx_item.text())
        except: return
        if mi < 0 or mi >= len(self._messages): return
        val = self.tbl.item(row, col).text()
        m = self._messages[mi]
        try:
            if col == 4:  # Delay
                m['delay'] = float(val); self._modified = True
            elif col == 5:  # Param
                m['param'] = float(val); self._modified = True
            elif col == 6:  # Extra
                m['extra'] = int(val.replace('0x',''), 16) if val.startswith('0x') else int(val); self._modified = True
            elif col == 7:  # Name
                m['name'] = val
                m['_raw_name'] = val.encode('ascii') + b'\x00' if val else b''
                m['_name_len'] = len(m['_raw_name']) if val else 0
                self._modified = True
        except: pass
        if self._modified:
            self.apply_btn.setStyleSheet("background:#e0a030; color:#111;")

    def _add_msg(self):
        """Add a new ENABLE_ENTITY message to slot 0."""
        new_msg = {
            'slot': 0, 'opcode': 0x0003,  # ENABLE_ENTITY
            'guid': 0, 'delay': 0.0, 'param': 1.0, 'extra': 0,
            'name': '', '_raw_name': b'', '_name_len': 0,
        }
        # Insert at beginning of slot 0
        self._messages.insert(0, new_msg)
        self._slot_counts[0] += 1
        self._modified = True
        self._build_rows()
        self._populate()
        self.apply_btn.setStyleSheet("background:#e0a030; color:#111;")
        self.title.setText(f"GSMS — {len(self._messages)} messages (modified)")

    def _del_msg(self):
        """Delete selected message."""
        sel = self.tbl.currentRow()
        if sel < 0: return
        idx_item = self.tbl.item(sel, 0)
        if not idx_item: return
        try: mi = int(idx_item.text())
        except: return
        if mi < 0 or mi >= len(self._messages): return
        m = self._messages[mi]
        slot = m['slot']
        self._messages.pop(mi)
        if slot < len(self._slot_counts) and self._slot_counts[slot] > 0:
            self._slot_counts[slot] -= 1
        self._modified = True
        self._build_rows()
        self._populate()
        self.apply_btn.setStyleSheet("background:#e0a030; color:#111;")
        self.title.setText(f"GSMS — {len(self._messages)} messages (modified)")

    def _apply(self):
        """Repack and apply changes to chunk."""
        if not self._modified or not self._chunks_ref or self._chunk_idx < 0: return
        old_content = self._chunks_ref[self._chunk_idx]['content']
        new_content = asura.repack_gsms(self._messages, self._slot_counts,
                                         self._ver, self._flags, self._nTables)
        self._chunks_ref[self._chunk_idx] = {
            **self._chunks_ref[self._chunk_idx], 'content': new_content,
            'size': len(new_content) + 16
        }
        self.editApplied.emit(f"Edit GSMS ({len(self._messages)} msgs)", self._chunk_idx,
                              old_content, new_content)
        self._modified = False
        self.apply_btn.setStyleSheet("")
        self.title.setText(f"GSMS — {len(self._messages)} messages in {len(self._slot_counts)} slots (applied)")

class AnimationView(QWidget):
    """Animation viewer: lists animations, timeline scrubber, skinned mesh + skeleton preview."""
    def __init__(self):
        super().__init__()
        lo = QVBoxLayout(self); lo.setContentsMargins(8,8,8,8); lo.setSpacing(4)
        self.lbl = QLabel("Animations"); self.lbl.setObjectName("ptitle"); lo.addWidget(self.lbl)

        # Top bar: search + play controls
        bar = QHBoxLayout(); bar.setSpacing(8)
        self.search = QLineEdit(); self.search.setPlaceholderText("Filter animations...")
        self.search.textChanged.connect(self._filter); bar.addWidget(self.search, 1)
        self.play_btn = QPushButton("▶ Play"); self.play_btn.setFixedWidth(70)
        self.play_btn.clicked.connect(self._toggle_play); bar.addWidget(self.play_btn)
        self.speed_lbl = QLabel("1.0×"); self.speed_lbl.setFixedWidth(35); bar.addWidget(self.speed_lbl)
        self.frame_lbl = QLabel("0/0"); self.frame_lbl.setFixedWidth(60); bar.addWidget(self.frame_lbl)
        self.export_btn = QPushButton("Export"); self.export_btn.setFixedWidth(60)
        self.export_btn.clicked.connect(self._export_animation); bar.addWidget(self.export_btn)
        lo.addLayout(bar)

        # Timeline slider
        self.slider = QSlider(Qt.Horizontal); self.slider.setRange(0, 1000)
        self.slider.valueChanged.connect(self._on_slider); lo.addWidget(self.slider)

        # View toggles
        tog_bar = QHBoxLayout(); tog_bar.setSpacing(8)
        self.mesh_cb = QCheckBox("Mesh"); self.mesh_cb.setChecked(True)
        self.mesh_cb.toggled.connect(self._on_toggle); tog_bar.addWidget(self.mesh_cb)
        self.skel_cb = QCheckBox("Skeleton"); self.skel_cb.setChecked(True)
        self.skel_cb.toggled.connect(self._on_toggle); tog_bar.addWidget(self.skel_cb)
        self.wire_cb = QCheckBox("Wire")
        self.wire_cb.toggled.connect(self._on_toggle); tog_bar.addWidget(self.wire_cb)
        tog_bar.addStretch()
        self.skel_info = QLabel(""); self.skel_info.setStyleSheet("color:#888;font-size:10px;"); tog_bar.addWidget(self.skel_info)
        lo.addLayout(tog_bar)

        # Split: table left, preview right
        split = QHBoxLayout(); split.setSpacing(8)
        self.tbl = QTableWidget(); self.tbl.setAlternatingRowColors(True)
        self.tbl.horizontalHeader().setStretchLastSection(True)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl.currentCellChanged.connect(self._on_select)
        split.addWidget(self.tbl, 3)

        # Preview canvas — OpenGL 3D viewer if available, else fallback label
        self._use_anim_gl = _HAS_MODEL_GL
        if self._use_anim_gl:
            self.skel_canvas = _QOpenGLWidget()
            self.skel_canvas.setMinimumSize(300, 350)
            self.skel_canvas.initializeGL = self._anim_initGL
            self.skel_canvas.paintGL = self._anim_paintGL
            self.skel_canvas.resizeGL = self._anim_resizeGL
            self.skel_canvas.mousePressEvent = self._anim_mousePress
            self.skel_canvas.mouseReleaseEvent = self._anim_mouseRelease
            self.skel_canvas.mouseMoveEvent = self._anim_mouseMove
            self.skel_canvas.wheelEvent = self._anim_wheel
            self._agl_rx = 15.0; self._agl_ry = -30.0; self._agl_zoom = 1.0
            self._agl_cx = 0.0; self._agl_cy = 0.8; self._agl_cz = 0.0; self._agl_rad = 1.2
            self._agl_drag = False; self._agl_rdrag = False; self._agl_lp = None
            self._agl_fitted = False; self._agl_ready = False
            self._agl_verts = []; self._agl_tris = []; self._agl_bones = []; self._agl_links = []
            self._agl_parts = []
        else:
            self.skel_canvas = QLabel()
            self.skel_canvas.setMinimumSize(300, 350)
            self.skel_canvas.setStyleSheet("background:#1a1a2e; border:1px solid #333;")
            self.skel_canvas.setAlignment(Qt.AlignCenter)
        split.addWidget(self.skel_canvas, 3)
        lo.addLayout(split, 1)

        self._rows = []; self._anims = []; self._skeletons = []; self._files = []
        self._cur_anim = None; self._cur_skeleton = None; self._cur_mesh = None
        self._cur_weights = None; self._cur_tris = None; self._cur_uvs = None
        self._cur_tex_img = None; self._owner_ref = None
        self._cur_part_verts = []  # split part geometry [{name, verts, tris}]
        self._cur_costume_parts = []  # raw costume part files
        self._playing = False; self._frame_t = 0.0
        self._timer = QTimer(); self._timer.timeout.connect(self._tick); self._speed = 1.0

    def load_data(self, chunks):
        """Load animations, skeletons, and character meshes from parsed chunks."""
        import tsg_oldgen as _a
        self._skeletons = []
        for c in chunks:
            if c['id'] == 'NKSH':
                sk = _a.parse_nksh_skeleton(c['content'])
                if sk: self._skeletons.append(sk)

        self._anims = _a.parse_nach_keyframes(chunks)
        self._files = _a.extract_fcsr_files(chunks)

        skel_names = ', '.join(f"'{sk['char_name']}'" for sk in self._skeletons)
        self.lbl.setText(f"Animations — {len(self._anims)} sequences · {len(self._skeletons)} skeletons ({skel_names})")

        headers = ['Name','Skeleton','Bones','Quats','Pos','Keys','Loop','RootMot']
        self.tbl.setColumnCount(len(headers)); self.tbl.setHorizontalHeaderLabels(headers)
        self._rows = []
        for a in self._anims:
            sk = _a.find_skeleton_for_animation(self._skeletons, a)
            sk_name = sk['char_name'] if sk else '?'
            self._rows.append((
                a['name'], sk_name, str(a['n_bones']), str(len(a['quats'])),
                str(len(a['positions'])), str(a['total_rot_keys']),
                '✓' if a['loop'] else '', '✓' if a['root_motion'] else '',
            ))
        self._populate()

    def _populate(self):
        f = self.search.text().lower()
        rows = [r for r in self._rows if f in ' '.join(r).lower()] if f else self._rows
        self.tbl.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j, v in enumerate(row): self.tbl.setItem(i, j, QTableWidgetItem(v))
        self.tbl.resizeColumnsToContents()

    def _filter(self): self._populate()

    def _on_select(self, row, col, prev_row, prev_col):
        import tsg_oldgen as _a
        f = self.search.text().lower()
        filtered = [i for i, r in enumerate(self._rows) if f in ' '.join(r).lower()] if f else list(range(len(self._rows)))
        if 0 <= row < len(filtered):
            idx = filtered[row]
            if idx < len(self._anims):
                self._cur_anim = self._anims[idx]
                self._cur_skeleton = _a.find_skeleton_for_animation(self._skeletons, self._cur_anim)
                if hasattr(self, '_agl_fitted'): self._agl_fitted = False
                # Find matching mesh (main + optional split parts)
                self._cur_mesh = None; self._cur_weights = None; self._cur_tris = None
                self._cur_uvs = None; self._cur_tex_img = None
                self._cur_part_verts = []  # list of (positions, tris) for split parts
                if self._cur_skeleton:
                    # Check if this animation belongs to a costume variant
                    costume, body_name = _a.get_costume_for_animation(self._cur_anim['name'])
                    if costume and body_name:
                        # Load the costume's body mesh instead of skeleton-name mesh
                        body_file, _, costume_parts = _a.get_body_mesh_for_animation(
                            self._files, self._cur_anim['name'], self._skeletons)
                        if body_file:
                            cv = body_file.get('chunk_ver', 2)
                            mesh = _a._parse_smoothskin_cv3(body_file['data'])
                            if not mesh: mesh = _a._parse_smoothskin(body_file['data'], cv)
                            if mesh: self._cur_mesh = mesh
                        # Parse split parts for rendering
                        self._cur_costume_parts = []
                        for pf in costume_parts:
                            pd = pf['data']
                            pfv = struct.unpack_from('>I', pd, 4)[0] if len(pd) >= 8 else 0
                            part_verts = []
                            part_tris = []
                            if pfv == 14 and len(pd) >= 32:
                                sm = struct.unpack_from('>I', pd, 8)[0]
                                vc = struct.unpack_from('>I', pd, 16)[0]
                                dls = struct.unpack_from('>I', pd, 12)[0]
                                vo = 32 + (sm-1)*12
                                if vo + vc*16 + dls == len(pd) and vc < 10000:
                                    gqr = 1.0/1024.0
                                    for i in range(vc):
                                        o = vo + i*16
                                        x = struct.unpack_from('>h', pd, o)[0] * gqr
                                        y = -struct.unpack_from('>h', pd, o+2)[0] * gqr
                                        z = -struct.unpack_from('>h', pd, o+4)[0] * gqr
                                        part_verts.append((x, y, z))
                                    # Parse display list triangles
                                    dl_data = pd[vo+vc*16:vo+vc*16+dls]
                                    d_off = 0
                                    while d_off < len(dl_data) - 3:
                                        cmd = dl_data[d_off]; d_off += 1
                                        if cmd in (0x98, 0x9E):
                                            stride = 7 if cmd == 0x98 else 8
                                            cnt = struct.unpack_from('>H', dl_data, d_off)[0]; d_off += 2
                                            idxs = []
                                            for vi in range(cnt):
                                                if d_off + stride > len(dl_data): break
                                                pi = struct.unpack_from('>H', dl_data, d_off)[0]
                                                idxs.append(pi)
                                                d_off += stride
                                            for ti in range(2, len(idxs)):
                                                a, b, c = idxs[ti-2], idxs[ti-1], idxs[ti]
                                                if a != b and b != c and a != c and max(a,b,c) < vc:
                                                    if ti % 2 == 0: part_tris.append((a, b, c))
                                                    else: part_tris.append((a, c, b))
                                        elif cmd == 0: break
                                        else: break
                            if part_verts and part_tris:
                                self._cur_part_verts.append({
                                    'name': pf['name'][8:],
                                    'verts': part_verts,
                                    'tris': part_tris
                                })
                        info = f"Costume: {costume} → body={body_name}"
                        if self._cur_mesh:
                            info += f" ({self._cur_mesh['nVtx']}v)"
                        if self._cur_part_verts:
                            info += f" +{len(self._cur_part_verts)} parts"
                        info += f" · Skel: {self._cur_skeleton['char_name']}"
                        self.skel_info.setText(info)
                    else:
                        # Regular animation — use standard mesh lookup
                        parts_info = _a.find_character_parts(self._files, self._cur_skeleton)
                        if parts_info and parts_info['main']:
                            self._cur_mesh = parts_info['main']
                        else:
                            self._cur_mesh = _a.find_mesh_for_skeleton(self._files, self._cur_skeleton)
                        if self._cur_mesh:
                            info = f"Mesh: {self._cur_mesh['nVtx']}v"
                            if parts_info and parts_info['parts']:
                                info += f" +{len(parts_info['parts'])} parts"
                            info += f" · Skel: {self._cur_skeleton['char_name']}"
                            self.skel_info.setText(info)
                        else:
                            self.skel_info.setText(f"Skel: {self._cur_skeleton['char_name']} (no mesh found)")
                    # Parse bone weights + tris for main mesh rendering
                    if self._cur_mesh:
                        if 'bone_info' in self._cur_mesh:
                            self._cur_weights = _a.parse_bone_weights(self._cur_mesh)
                        self._cur_uvs = self._cur_mesh.get('uvs', [])
                        if self._cur_mesh.get('triangles'):
                            self._cur_tris = self._cur_mesh['triangles']
                        elif self._cur_mesh.get('indices'):
                            self._cur_tris = _a._tristrip_to_tris(self._cur_mesh['indices'], self._cur_mesh['nVtx'])
                        else:
                            self._cur_tris = []
                        if self._owner_ref:
                            try:
                                sn = self._cur_skeleton['char_name']
                                tex = self._owner_ref._get_model_texture(f'Stripped{sn}')
                                if tex: self._cur_tex_img = tex.convert('RGBA')
                            except: pass
                else:
                    self.skel_info.setText("No matching skeleton")
                self._frame_t = 0.0
                self.frame_lbl.setText(f"0/{len(self._cur_anim['quats'])}q")
                self._draw_skeleton()

    def _toggle_play(self):
        self._playing = not self._playing
        if self._playing:
            self.play_btn.setText("⏸ Pause")
            self._timer.start(33)  # ~30fps
        else:
            self.play_btn.setText("▶ Play")
            self._timer.stop()

    def _tick(self):
        if not self._cur_anim: return
        self._frame_t += 0.02 * self._speed
        if self._frame_t > 1.0:
            if self._cur_anim.get('loop'):
                self._frame_t -= 1.0
            else:
                self._frame_t = 0.0
        self.slider.blockSignals(True)
        self.slider.setValue(int(self._frame_t * 1000))
        self.slider.blockSignals(False)
        self._draw_skeleton()

    def _on_slider(self, val):
        self._frame_t = val / 1000.0
        self._draw_skeleton()

    def _sample_anim_tex(self, u, v):
        """Sample the character texture at UV coordinates for animation viewer coloring."""
        if self._cur_tex_img is None: return None
        try:
            import numpy as np
            w, h = self._cur_tex_img.size
            px = int((u % 1.0) * w) % w
            py = int((v % 1.0) * h) % h
            arr = np.array(self._cur_tex_img)
            r, g, b = int(arr[py, px, 0]), int(arr[py, px, 1]), int(arr[py, px, 2])
            return (r, g, b)
        except: return None

    def _on_toggle(self, *_args):
        """Handle mesh/skeleton/wire checkbox toggles."""
        if self._use_anim_gl:
            self._draw_skeleton()

    # ── OpenGL Animation Viewer Methods ──
    def _anim_initGL(self):
        glClearColor(0.067, 0.067, 0.08, 1.0)
        glEnable(GL_DEPTH_TEST); glEnable(GL_LIGHTING); glEnable(GL_LIGHT0)
        glLightfv(GL_LIGHT0, GL_POSITION, [0.3, 1.0, 0.8, 0.0])
        glLightfv(GL_LIGHT0, GL_DIFFUSE, [0.85, 0.85, 0.85, 1.0])
        glLightfv(GL_LIGHT0, GL_AMBIENT, [0.3, 0.3, 0.3, 1.0])
        glEnable(GL_COLOR_MATERIAL); glColorMaterial(GL_FRONT_AND_BACK, GL_AMBIENT_AND_DIFFUSE)
        glShadeModel(GL_SMOOTH); glEnable(GL_NORMALIZE)
        self._agl_ready = True

    def _anim_resizeGL(self, w, h):
        glViewport(0, 0, w, h); glMatrixMode(GL_PROJECTION); glLoadIdentity()
        gluPerspective(45.0, w / max(1, h), 0.01, 100.0)

    def _anim_paintGL(self):
        import math
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        glMatrixMode(GL_MODELVIEW); glLoadIdentity()
        dist = self._agl_rad * 3.0 / max(0.1, self._agl_zoom)
        rx = math.radians(self._agl_rx); ry = math.radians(self._agl_ry)
        ex = self._agl_cx + dist * math.sin(ry) * math.cos(rx)
        ey = self._agl_cy + dist * math.sin(rx)
        ez = self._agl_cz + dist * math.cos(ry) * math.cos(rx)
        gluLookAt(ex, ey, ez, self._agl_cx, self._agl_cy, self._agl_cz, 0, 1, 0)
        # Grid
        glDisable(GL_LIGHTING); gs = self._agl_rad * 0.25
        glColor4f(0.15, 0.15, 0.18, 0.5)
        glBegin(GL_LINES)
        for gi in range(-5, 6):
            glVertex3f(gi*gs, 0, -5*gs); glVertex3f(gi*gs, 0, 5*gs)
            glVertex3f(-5*gs, 0, gi*gs); glVertex3f(5*gs, 0, gi*gs)
        glEnd()
        # Mesh
        show_mesh = self.mesh_cb.isChecked()
        show_wire = self.wire_cb.isChecked() if hasattr(self, 'wire_cb') else False
        if show_mesh and self._agl_verts and self._agl_tris:
            if show_wire:
                glDisable(GL_LIGHTING); glPolygonMode(GL_FRONT_AND_BACK, GL_LINE); glColor3f(0.88, 0.63, 0.19)
            else:
                glEnable(GL_LIGHTING); glPolygonMode(GL_FRONT_AND_BACK, GL_FILL); glColor3f(0.95, 0.85, 0.30)
            self._gl_draw_tris(self._agl_verts, self._agl_tris)
            if self._agl_parts:
                if not show_wire: glColor3f(0.75, 0.90, 0.35)
                for part in self._agl_parts:
                    self._gl_draw_tris(part['verts'], part['tris'])
            glPolygonMode(GL_FRONT_AND_BACK, GL_FILL)
        # Skeleton
        show_skel = self.skel_cb.isChecked()
        if show_skel and self._agl_bones:
            glDisable(GL_LIGHTING); glDisable(GL_DEPTH_TEST)
            glLineWidth(2.0); glColor3f(0.4, 0.8, 1.0)
            glBegin(GL_LINES)
            for pi, ci in self._agl_links:
                if pi < len(self._agl_bones) and ci < len(self._agl_bones):
                    glVertex3f(*self._agl_bones[pi]); glVertex3f(*self._agl_bones[ci])
            glEnd()
            glPointSize(5.0); glColor3f(1.0, 0.85, 0.2)
            glBegin(GL_POINTS)
            for bp in self._agl_bones: glVertex3f(*bp)
            glEnd()
            glEnable(GL_DEPTH_TEST); glLineWidth(1.0)

    def _gl_draw_tris(self, verts, tris):
        import math
        glBegin(GL_TRIANGLES)
        for i0, i1, i2 in tris:
            if max(i0, i1, i2) >= len(verts): continue
            v0, v1, v2 = verts[i0], verts[i1], verts[i2]
            ax, ay, az = v1[0]-v0[0], v1[1]-v0[1], v1[2]-v0[2]
            bx, by, bz = v2[0]-v0[0], v2[1]-v0[1], v2[2]-v0[2]
            nx, ny, nz = ay*bz-az*by, az*bx-ax*bz, ax*by-ay*bx
            nl = math.sqrt(nx*nx+ny*ny+nz*nz)
            if nl > 1e-8: glNormal3f(nx/nl, ny/nl, nz/nl)
            glVertex3f(*v0); glVertex3f(*v1); glVertex3f(*v2)
        glEnd()

    def _anim_mousePress(self, ev):
        if ev.button() == Qt.LeftButton: self._agl_drag = True; self._agl_lp = ev.position()
        elif ev.button() == Qt.RightButton: self._agl_rdrag = True; self._agl_lp = ev.position()

    def _anim_mouseRelease(self, ev):
        if ev.button() == Qt.LeftButton: self._agl_drag = False
        elif ev.button() == Qt.RightButton: self._agl_rdrag = False

    def _anim_mouseMove(self, ev):
        import math
        if not self._agl_lp: return
        pos = ev.position(); dx = pos.x() - self._agl_lp.x(); dy = pos.y() - self._agl_lp.y()
        self._agl_lp = pos
        if self._agl_drag:
            self._agl_ry -= dx * 0.5; self._agl_rx += dy * 0.5
            self._agl_rx = max(-89, min(89, self._agl_rx)); self.skel_canvas.update()
        elif self._agl_rdrag:
            sc = self._agl_rad * 0.003 / max(0.1, self._agl_zoom)
            ry = math.radians(self._agl_ry)
            self._agl_cx += (-math.cos(ry)*dx)*sc; self._agl_cy += dy*sc
            self._agl_cz += (math.sin(ry)*dx)*sc; self.skel_canvas.update()

    def _anim_wheel(self, ev):
        d = ev.angleDelta().y()
        if d > 0: self._agl_zoom *= 1.1
        elif d < 0: self._agl_zoom /= 1.1
        self._agl_zoom = max(0.1, min(20.0, self._agl_zoom)); self.skel_canvas.update()

    def _anim_autofit(self, verts):
        if not verts: return
        xs = [v[0] for v in verts]; ys = [v[1] for v in verts]; zs = [v[2] for v in verts]
        self._agl_cx = (min(xs)+max(xs))/2; self._agl_cy = (min(ys)+max(ys))/2; self._agl_cz = (min(zs)+max(zs))/2
        self._agl_rad = max(max(xs)-min(xs), max(ys)-min(ys), max(zs)-min(zs), 0.1) / 2

    def _draw_skeleton(self, *_args):
        """Compute animation frame and update viewer."""
        if not self._cur_skeleton or not self._cur_anim:
            if not self._use_anim_gl:
                self.skel_canvas.setText("Select an animation")
            return
        import tsg_oldgen as _a
        try:
            bone_pos, bone_links = _a.get_animation_bone_positions(
                self._cur_skeleton, self._cur_anim, self._frame_t)
        except:
            if not self._use_anim_gl:
                self.skel_canvas.setText("Eval error")
            return

        # Get skinned mesh positions
        mesh_pos = None
        if self._cur_mesh and self._cur_skeleton and self._cur_weights:
            try:
                mesh_pos = _a.skin_character_mesh(
                    self._cur_mesh, self._cur_skeleton, self._cur_anim,
                    self._frame_t, self._cur_weights)
            except: pass

        # Update info label
        info = f"{self._cur_anim['name'][:35]} t={self._frame_t:.2f}"
        info += f"\nskel:{self._cur_skeleton['char_name']} {self._cur_skeleton['count']}b"
        if self._cur_mesh:
            info += f"\nmesh:{self._cur_mesh['nVtx']}v {len(self._cur_tris or [])}t"
        self.skel_info.setText(info)

        if self._use_anim_gl:
            # Send data to OpenGL viewer
            self._agl_bones = bone_pos or []
            self._agl_links = bone_links or []
            if mesh_pos and self._cur_tris:
                self._agl_verts = mesh_pos
                self._agl_tris = self._cur_tris
            else:
                self._agl_verts = []; self._agl_tris = []
            # Parts
            self._agl_parts = []
            if hasattr(self, '_cur_part_verts') and self._cur_part_verts:
                self._agl_parts = self._cur_part_verts
            # Auto-fit camera on first frame
            all_pts = list(bone_pos or [])
            if mesh_pos: all_pts.extend(mesh_pos)
            if all_pts and not self._agl_fitted:
                self._anim_autofit(all_pts); self._agl_fitted = True
            self.skel_canvas.update()
            return

        # ── Fallback: QPainter 2D rendering (if no OpenGL) ──
        if not bone_pos:
            self.skel_canvas.setText("No bone data"); return

        w = max(self.skel_canvas.width(), 250); h = max(self.skel_canvas.height(), 300)
        from PySide6.QtGui import QPixmap, QPainter, QColor, QPen, QBrush, QFont, QPolygonF
        pm = QPixmap(w, h); pm.fill(QColor(26, 26, 46))
        p = QPainter(pm); p.setRenderHint(QPainter.Antialiasing)

        all_pts = list(bone_pos)
        if mesh_pos: all_pts.extend(mesh_pos)
        def get_2d(pt): return pt[0], pt[1]  # Front view
        xs2d = [get_2d(p2)[0] for p2 in all_pts]; ys2d = [get_2d(p2)[1] for p2 in all_pts]
        if not xs2d or max(xs2d) == min(xs2d):
            p.end(); self.skel_canvas.setPixmap(pm); return
        cx2 = (min(xs2d)+max(xs2d))/2; cy2 = (min(ys2d)+max(ys2d))/2
        span = max(max(xs2d)-min(xs2d), max(ys2d)-min(ys2d), 0.1) * 1.15
        scale = min(w, h) / span
        def proj(pt):
            x2, y2 = get_2d(pt); return w/2+(x2-cx2)*scale, h/2-(y2-cy2)*scale

        # Draw mesh
        if self.mesh_cb.isChecked() and mesh_pos and self._cur_tris:
            for i0, i1, i2 in self._cur_tris:
                if max(i0,i1,i2) >= len(mesh_pos): continue
                p0,p1,p2 = mesh_pos[i0],mesh_pos[i1],mesh_pos[i2]
                sx0,sy0 = proj(p0); sx1,sy1 = proj(p1); sx2,sy2 = proj(p2)
                p.setPen(Qt.NoPen); p.setBrush(QBrush(QColor(140,130,60)))
                poly = QPolygonF([QPointF(sx0,sy0),QPointF(sx1,sy1),QPointF(sx2,sy2)])
                p.drawPolygon(poly)

        # Draw skeleton
        if self.skel_cb.isChecked():
            p.setPen(QPen(QColor(100, 200, 255), 2))
            for pi, ci in bone_links:
                x1,y1 = proj(bone_pos[pi]); x2,y2 = proj(bone_pos[ci])
                p.drawLine(int(x1),int(y1),int(x2),int(y2))
            p.setPen(QPen(QColor(255,255,100),1)); p.setBrush(QBrush(QColor(255,200,50)))
            for bp in bone_pos:
                sx,sy = proj(bp); p.drawEllipse(int(sx-3),int(sy-3),6,6)

        p.end(); self.skel_canvas.setPixmap(pm)

    def _export_animation(self):
        """Export current animation as GIF or MP4."""
        if not self._cur_anim or not self._cur_skeleton:
            return
        from PySide6.QtWidgets import QFileDialog, QMessageBox, QProgressDialog
        from PySide6.QtCore import QCoreApplication
        
        name = self._cur_anim['name']
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Animation", f"{name}.gif",
            "GIF Animation (*.gif);;MP4 Video (*.mp4);;PNG Sequence (*.png)")
        if not path: return
        
        is_gif = path.lower().endswith('.gif')
        is_mp4 = path.lower().endswith('.mp4')
        is_png = path.lower().endswith('.png')
        
        fps = 30
        n_keys = max(a['n_rot_keys'] for a in self._cur_anim['bone_table']) if self._cur_anim['bone_table'] else 10
        n_frames = max(min(n_keys * 2, 120), 20)  # 20-120 frames
        
        progress = QProgressDialog(f"Exporting {n_frames} frames...", "Cancel", 0, n_frames, self)
        progress.setWindowTitle("Export Animation")
        progress.setMinimumDuration(0)
        progress.show()
        
        frames = []
        old_t = self._frame_t
        
        for fi in range(n_frames):
            if progress.wasCanceled(): break
            progress.setValue(fi)
            QCoreApplication.processEvents()
            
            self._frame_t = fi / max(1, n_frames - 1) if not self._cur_anim.get('loop') else (fi / n_frames)
            self._draw_skeleton()
            QCoreApplication.processEvents()
            
            # Grab frame from the viewer
            if self._use_anim_gl:
                self.skel_canvas.makeCurrent()
                self._anim_paintGL()
                img = self.skel_canvas.grabFramebuffer()
                self.skel_canvas.doneCurrent()
            else:
                pm = self.skel_canvas.pixmap()
                img = pm.toImage() if pm else None
            
            if img:
                # Convert QImage to PIL Image
                try:
                    from PIL import Image
                    w, h = img.width(), img.height()
                    ptr = img.constBits()
                    if hasattr(ptr, 'tobytes'):
                        raw = ptr.tobytes()
                    else:
                        raw = bytes(ptr)
                    pil_img = Image.frombytes('RGBA', (w, h), raw, 'raw', 'BGRA')
                    pil_img = pil_img.convert('RGB')
                    frames.append(pil_img)
                except ImportError:
                    QMessageBox.warning(self, "Export", "Pillow (PIL) is required for export.\npip install Pillow")
                    break
        
        self._frame_t = old_t
        self._draw_skeleton()
        progress.setValue(n_frames)
        
        if not frames:
            return
        
        try:
            if is_gif:
                duration = int(1000 / fps)
                frames[0].save(path, save_all=True, append_images=frames[1:],
                              duration=duration, loop=0, optimize=True)
                QMessageBox.information(self, "Export", f"Saved {len(frames)}-frame GIF to:\n{path}")
            
            elif is_mp4:
                try:
                    import imageio
                    import numpy as np
                    writer = imageio.get_writer(path, fps=fps, codec='libx264', quality=8)
                    for frame in frames:
                        writer.append_data(np.array(frame))
                    writer.close()
                    QMessageBox.information(self, "Export", f"Saved {len(frames)}-frame MP4 to:\n{path}")
                except ImportError:
                    # Fallback: save as GIF with .mp4 name changed
                    gif_path = path.rsplit('.', 1)[0] + '.gif'
                    duration = int(1000 / fps)
                    frames[0].save(gif_path, save_all=True, append_images=frames[1:],
                                  duration=duration, loop=0, optimize=True)
                    QMessageBox.information(self, "Export", 
                        f"imageio not available for MP4.\nSaved as GIF instead:\n{gif_path}\n\nFor MP4: pip install imageio imageio-ffmpeg")
            
            elif is_png:
                import os
                base = path.rsplit('.', 1)[0]
                for i, frame in enumerate(frames):
                    frame.save(f"{base}_{i:04d}.png")
                QMessageBox.information(self, "Export", f"Saved {len(frames)} PNG frames to:\n{base}_XXXX.png")
        
        except Exception as e:
            QMessageBox.warning(self, "Export Error", str(e))



    def load_animations(self, anims):
        """Legacy interface for old-style animation list."""
        self.lbl.setText("Animations — {} sequences".format(len(anims)))
        headers = ['Set','Name','Duration','Bones','Channels','Types','Keys']
        self.tbl.setColumnCount(len(headers)); self.tbl.setHorizontalHeaderLabels(headers)
        self._rows = [(str(a.get('set','')), a.get('name',''), "{:.3f}s".format(a.get('duration',0)),
                       str(a.get('n_bones',0)), str(a.get('channels',0)),
                       a.get('types',''), str(a.get('total_keys',0)))
                      for a in anims]
        self._populate()

# ============================================================
# Phase 2: Level Viewer
# ============================================================

def mul31_hash(s):
    h = 0
    for c in s.lower(): h = (h * 31 + ord(c)) & 0xFFFFFFFF
    return h

def parse_level_data(chunks, files):
    """Extract complete level placement data from parsed chunks."""
    import tsg_oldgen as _a
    
    # Build model hash lookup
    model_hashes = {}
    model_data = {}  # name → file data
    for f in files:
        if f['name'].startswith('Stripped') and f['name'] != 'StrippedEnv':
            name = f['name'][8:]
            model_hashes[mul31_hash(name)] = name
            model_data[name] = f
    for c in chunks:
        if c['id'] == 'BBSH':
            d = c['content']; null = d.find(b'\x00')
            if null > 0:
                name = d[:null].decode('ascii', errors='replace')
                model_hashes[mul31_hash(name)] = name
    
    # Build ordered TXET texture paths
    txet_paths = []
    for c in chunks:
        if c['id'] == 'TXET':
            d = c['content']
            n = struct.unpack_from('>I', d, 0)[0]
            if n == 0: txet_paths.append(None)
            else:
                null = d[4:].find(b'\x00')
                txet_paths.append(d[4:4+null].decode('ascii', errors='replace') if null > 0 else None)
    
    # Parse entities using position+quaternion scanner (optimized for 3D viewport)
    entities = []
    for c in chunks:
        if c['id'] != 'ITNE': continue
        d = c['content']
        if len(d) < 8: continue
        eid = struct.unpack_from('>I', d, 0)[0]
        etype = struct.unpack_from('>H', d, 4)[0]
        result = _a._find_entity_position(d)
        pos = result[0] if result else None
        quat = result[1] if result else None
        # NPCSpawner (0x8017): rotation matrix at +24, position at +60
        if pos is None and etype == 0x8017 and len(d) >= 72:
            px = struct.unpack_from('>f', d, 60)[0]
            py = struct.unpack_from('>f', d, 64)[0]
            pz = struct.unpack_from('>f', d, 68)[0]
            if all(abs(v) < 10000 for v in (px, py, pz)):
                pos = (px, -py, -pz)
        # Scan for model hash
        model = None
        for off in range(8, min(len(d) - 3, 140), 4):
            v = struct.unpack_from('>I', d, off)[0]
            if v in model_hashes: model = model_hashes[v]; break
        if pos and (abs(pos[0]) > 10000 or abs(pos[1]) > 10000 or abs(pos[2]) > 10000):
            pos = None
        ent = {'eid': eid, 'type': etype, 'model': model}
        if pos: ent['pos'] = pos
        if quat: ent['quat'] = quat
        entities.append(ent)
    
    # Parse env mesh bounds
    env_bounds = None
    for f in files:
        if f['name'] == 'StrippedEnv':
            d = f['data']
            if len(d) > 28:
                off = 0; ver = struct.unpack_from('>I', d, off)[0]; off += 4
                nM = struct.unpack_from('>I', d, off)[0]; off += 4
                fl = struct.unpack_from('>I', d, off)[0]; off += 4
                off += 4; off += 4
                if fl & 2: off += 4
                off += 4
                all_pos = []
                for m in range(nM):
                    if off + 12 > len(d): break
                    nP = struct.unpack_from('>I', d, off)[0]; off += 4
                    nV = struct.unpack_from('>I', d, off)[0]; off += 4
                    nS = struct.unpack_from('>I', d, off)[0]; off += 4
                    off += 24
                    for i in range(nP):
                        if off + 12 > len(d): break
                        fx = struct.unpack_from('>f', d, off)[0]
                        fy = struct.unpack_from('>f', d, off+4)[0]
                        fz = struct.unpack_from('>f', d, off+8)[0]
                        all_pos.append((fx, -fy, -fz))
                        off += 12
                    off += nV * 4 + nV * 3
                    for s in range(nS):
                        if fl & 1:
                            off += 4+4+4
                            dls = struct.unpack_from('>I', d, off)[0]; off += 4 + dls
                        else:
                            off += 4
                            nc = struct.unpack_from('>I', d, off)[0]; off += 4+4+nc*2
                if all_pos:
                    xs=[p[0] for p in all_pos]; ys=[p[1] for p in all_pos]; zs=[p[2] for p in all_pos]
                    env_bounds = (min(xs),min(ys),min(zs),max(xs),max(ys),max(zs))
            break
    
    # Skybox + fog
    skybox_paths = []
    for c in chunks:
        if c['id'] == 'BYKS':
            d = c['content']; j = 16
            while j < len(d):
                if d[j] == 0x5c or (32 <= d[j] < 127 and j > 16):
                    start = j
                    while j < len(d) and 32 <= d[j] < 127: j += 1
                    s = d[start:j].decode('ascii', errors='replace')
                    if len(s) >= 5: skybox_paths.append(s)
                else: j += 1
            break
    
    fog = None
    for c in chunks:
        if c['id'] == ' GOF':
            d = c['content']
            fog = [struct.unpack_from('>f', d, j*4)[0] for j in range(min(10, len(d)//4))]
            break
    
    return {
        'entities': entities, 'txet_paths': txet_paths, 'model_data': model_data,
        'env_bounds': env_bounds, 'skybox': skybox_paths, 'fog': fog
    }

ETYPE_COLORS = {
    0x8006: (0xFF, 0x80, 0x00),  # DestructibleObj: orange
    0x8005: (0x40, 0xFF, 0x40),  # Pickup: green
    0x8003: (0xFF, 0x40, 0x40),  # NPC: red
    0x8001: (0xFF, 0x60, 0x60),  # Actor: light red
    0x8012: (0xFF, 0xFF, 0x40),  # Trampoline: yellow
    0x8013: (0xFF, 0xA0, 0x40),  # Interactive: amber
    0x8021: (0x80, 0x40, 0xFF),  # LardLad: purple
    0x8017: (0xFF, 0x60, 0x80),  # NPCSpawner: pink
    0x800C: (0x00, 0xFF, 0x80),  # Player: bright green
    0x800D: (0x00, 0xFF, 0x80),  # Player: bright green
    0x0007: (0xFF, 0xFF, 0x80),  # PhysicsObj: light yellow
    0x0011: (0xFF, 0xFF, 0x80),  # AdvancedLight: light yellow
    0x0009: (0xFF, 0xFF, 0x60),  # DestrLight: light yellow
    0x002F: (0x00, 0xFF, 0xFF),  # SpawnPoint: cyan
    0x0029: (0x00, 0xDD, 0xFF),  # StartPoint: light cyan
    0x0021: (0x60, 0x80, 0xFF),  # PFX_Effect: blue
    0x0033: (0x40, 0x40, 0x80),  # CameraVolume: dark blue
    0x0014: (0x80, 0x60, 0x60),  # AdvVolumeTrigger: brown
    0x801E: (0x60, 0x60, 0x60),  # GuardZone: gray
    0x8022: (0xFF, 0xCC, 0x00),  # Objective: gold
}

class LevelViewer(QWidget):
    """Top-down level map with entity placement visualization."""
    def __init__(self):
        super().__init__()
        lo = QVBoxLayout(self); lo.setContentsMargins(0,0,0,0); lo.setSpacing(0)
        bar = QHBoxLayout(); bar.setContentsMargins(8,4,8,4)
        self.info = QLabel("No level loaded"); self.info.setObjectName("ptitle"); bar.addWidget(self.info)
        bar.addStretch()
        self.show_props = QCheckBox("Objects"); self.show_props.setChecked(True); self.show_props.toggled.connect(self.update); bar.addWidget(self.show_props)
        self.show_destr = QCheckBox("NPCs"); self.show_destr.setChecked(True); self.show_destr.toggled.connect(self.update); bar.addWidget(self.show_destr)
        self.show_pickups = QCheckBox("Pickups"); self.show_pickups.setChecked(True); self.show_pickups.toggled.connect(self.update); bar.addWidget(self.show_pickups)
        self.show_other = QCheckBox("Other"); self.show_other.setChecked(False); self.show_other.toggled.connect(self.update); bar.addWidget(self.show_other)
        self.show_labels = QCheckBox("Labels"); self.show_labels.setChecked(False); self.show_labels.toggled.connect(self.update); bar.addWidget(self.show_labels)
        self.ss_btn = QPushButton("Screenshot"); self.ss_btn.setFixedWidth(90); self.ss_btn.clicked.connect(self._screenshot); bar.addWidget(self.ss_btn)
        lo.addLayout(bar)
        self._canvas = QLabel(); self._canvas.setStyleSheet("background:#111114;")
        self._canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        lo.addWidget(self._canvas)
        self._level = None; self._zoom = 1.0; self._pan_x = 0; self._pan_y = 0
        self._drag = False; self._lp = None
        self._canvas.installEventFilter(self)
    
    def load_level(self, level_data):
        self._level = level_data
        ents = level_data['entities']
        with_model = sum(1 for e in ents if e['model'])
        from collections import Counter
        tc = Counter(e['type'] for e in ents)
        self.info.setText(f"Level: {len(ents)} entities ({with_model} placed), {len(level_data['txet_paths'])} materials")
        self._zoom = 1.0; self._pan_x = 0; self._pan_y = 0
        self._paint()
    
    def _paint(self):
        if not self._level: return
        w = max(self._canvas.width(), 200); h = max(self._canvas.height(), 200)
        img = QImage(w, h, QImage.Format_ARGB32); img.fill(QColor(0x11, 0x11, 0x14))
        pa = QPainter(img); pa.setRenderHint(QPainter.Antialiasing, True)
        
        ents = self._level['entities']
        if not ents: pa.end(); self._canvas.setPixmap(QPixmap.fromImage(img)); return
        
        # Compute bounds from entity positions (using XZ as the ground plane)
        xs = [e['pos'][0] for e in ents if 'pos' in e]; zs = [e['pos'][2] for e in ents if 'pos' in e]
        if self._level['env_bounds']:
            b = self._level['env_bounds']
            xs.extend([b[0], b[3]]); zs.extend([b[2], b[5]])
        
        cx, cz = (min(xs)+max(xs))/2, (min(zs)+max(zs))/2
        span = max(max(xs)-min(xs), max(zs)-min(zs)) or 1
        scale = min(w, h) * 0.85 / span * self._zoom
        
        # Draw env bounds
        if self._level['env_bounds']:
            b = self._level['env_bounds']
            pa.setPen(QPen(QColor(0x33, 0x44, 0x33), 1))
            x1 = w/2 + (b[0]-cx)*scale + self._pan_x; z1 = h/2 + (b[2]-cz)*scale + self._pan_y
            x2 = w/2 + (b[3]-cx)*scale + self._pan_x; z2 = h/2 + (b[5]-cz)*scale + self._pan_y
            pa.drawRect(int(min(x1,x2)), int(min(z1,z2)), int(abs(x2-x1)), int(abs(z2-z1)))
        
        # Draw grid
        pa.setPen(QPen(QColor(0x22, 0x22, 0x28), 1))
        gs = 10
        while gs * scale < 20: gs *= 2
        while gs * scale > 100: gs /= 2
        gx_start = int((min(xs) - cx) // gs) * gs
        gz_start = int((min(zs) - cz) // gs) * gs
        for gx in range(int(gx_start), int(max(xs)-cx)+int(gs), int(gs)):
            sx = w/2 + gx*scale + self._pan_x
            pa.drawLine(int(sx), 0, int(sx), h)
        for gz in range(int(gz_start), int(max(zs)-cz)+int(gs), int(gs)):
            sy = h/2 + gz*scale + self._pan_y
            pa.drawLine(0, int(sy), w, int(sy))
        
        # Draw entities
        show_types = set()
        if self.show_props.isChecked(): show_types.update([0x8006, 0x8013, 0x8012, 0x8004])
        if self.show_destr.isChecked(): show_types.update([0x8003, 0x8001, 0x8017])
        if self.show_pickups.isChecked(): show_types.add(0x8005)
        if self.show_other.isChecked(): show_types.update([0x0007, 0x0011, 0x002f, 0x0029, 0x0021, 0x8021, 0x0014, 0x0033, 0x801E, 0x8022, 0x800C, 0x800D, 0x801B, 0x8014])
        
        for e in ents:
            if e['type'] not in show_types: continue
            if 'pos' not in e: continue
            px, py, pz = e['pos']
            sx = w/2 + (px - cx) * scale + self._pan_x
            sy = h/2 + (pz - cz) * scale + self._pan_y
            
            r, g, b = ETYPE_COLORS.get(e['type'], (0x88, 0x88, 0x88))
            sz = 4 if e['model'] else 2
            pa.setPen(Qt.NoPen)
            pa.setBrush(QBrush(QColor(r, g, b, 200)))
            pa.drawEllipse(QPointF(sx, sy), sz, sz)
            
            if self.show_labels.isChecked() and e['model'] and scale > 2:
                pa.setPen(QPen(QColor(r, g, b, 150), 1))
                pa.setFont(QFont("Consolas", 7))
                pa.drawText(int(sx)+6, int(sy)+3, e['model'])
        
        # Legend
        pa.setPen(Qt.NoPen); ly = 10
        for etype, (r, g, b) in sorted(ETYPE_COLORS.items()):
            names = {0x8006:'DestrObj',0x8005:'Pickup',0x8003:'NPC',0x8001:'Actor',0x8012:'Trampoline',0x8013:'Interactive',0x8021:'LardLad',0x8017:'NPCSpawn',0x800C:'Player',0x800D:'Player',0x0007:'PhysObj',0x0011:'AdvLight',0x0009:'DestrLight',0x002F:'SpawnPt',0x0029:'StartPt',0x0021:'PFX',0x0033:'CamVol',0x0014:'AdvVolTrig',0x801E:'GuardZone',0x8022:'Objective'}
            name = names.get(etype, f'0x{etype:04x}')
            cnt = sum(1 for e in ents if e['type'] == etype)
            if cnt == 0: continue
            pa.setBrush(QBrush(QColor(r, g, b))); pa.drawEllipse(w-120, ly, 8, 8)
            pa.setPen(QPen(QColor(0xaa, 0xaa, 0xaa), 1)); pa.setFont(QFont("Consolas", 9))
            pa.drawText(w-106, ly+8, f"{name} ({cnt})")
            pa.setPen(Qt.NoPen); ly += 14
        
        pa.end(); self._canvas.setPixmap(QPixmap.fromImage(img))
    
    def _screenshot(self):
        if not self._level: return
        p,_ = QFileDialog.getSaveFileName(self, "Save Screenshot", "level_map.png", "PNG (*.png)")
        if p:
            pm = self._canvas.pixmap()
            if pm: pm.save(p)
    
    def eventFilter(self, obj, event):
        if obj != self._canvas: return False
        t = event.type()
        if t == QEvent.MouseButtonPress:
            self._lp = event.position(); self._drag = True; return True
        elif t == QEvent.MouseButtonRelease:
            self._drag = False; return True
        elif t == QEvent.MouseMove and self._drag and self._lp:
            dx = event.position().x() - self._lp.x()
            dy = event.position().y() - self._lp.y()
            self._lp = event.position()
            self._pan_x += dx; self._pan_y += dy
            self._paint(); return True
        elif t == QEvent.Wheel:
            self._zoom *= 1.15 if event.angleDelta().y() > 0 else 1/1.15
            self._zoom = max(0.1, min(50, self._zoom))
            self._paint(); return True
        return False
    
    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._level: self._paint()


# ============================================================
# ELF / DOL Browser Widget
# ============================================================

class ElfBrowser(QWidget):
    """Browse ELF/DOL symbol tables and Ghidra decompilation with search, filter, and Gecko code generation."""
    def __init__(self):
        super().__init__()
        lo = QVBoxLayout(self); lo.setContentsMargins(6,6,6,6); lo.setSpacing(4)
        # Header
        self._info = QLabel("No executable loaded"); self._info.setStyleSheet("font-weight:bold; font-size:13px; color: #e0a030;"); lo.addWidget(self._info)
        self._sec_info = QLabel(""); self._sec_info.setStyleSheet("color: #888; font-size: 11px;"); lo.addWidget(self._sec_info)
        # Controls bar
        bar = QHBoxLayout(); bar.setSpacing(6)
        self._search = QLineEdit(); self._search.setPlaceholderText("Search symbols (name, address, class)..."); self._search.textChanged.connect(self._filter); bar.addWidget(self._search, 3)
        self._type_filter = QComboBox(); self._type_filter.addItems(["All Types", "FUNC", "OBJECT", "NOTYPE"]); self._type_filter.currentTextChanged.connect(self._filter); bar.addWidget(self._type_filter)
        self._bind_filter = QComboBox(); self._bind_filter.addItems(["All Scope", "GLOBAL", "LOCAL", "WEAK"]); self._bind_filter.currentTextChanged.connect(self._filter); bar.addWidget(self._bind_filter)
        self._ns_filter = QComboBox(); self._ns_filter.addItem("All Classes"); self._ns_filter.currentTextChanged.connect(self._filter); bar.addWidget(self._ns_filter)
        self._demangle_cb = QCheckBox("Demangle"); self._demangle_cb.setChecked(True); self._demangle_cb.toggled.connect(self._refresh_table); bar.addWidget(self._demangle_cb)
        lo.addLayout(bar)
        # Main splitter: table on top, code on bottom
        splitter = QSplitter(Qt.Vertical)
        # Symbol table
        self._table = QTableWidget(); self._table.setColumnCount(6)
        self._table.setHorizontalHeaderLabels(["Address", "Size", "Type", "Bind", "Name", "Demangled / Signature"])
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setColumnWidth(0, 100); self._table.setColumnWidth(1, 70); self._table.setColumnWidth(2, 65)
        self._table.setColumnWidth(3, 65); self._table.setColumnWidth(4, 250)
        self._table.setAlternatingRowColors(True); self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setContextMenuPolicy(Qt.CustomContextMenu); self._table.customContextMenuRequested.connect(self._ctx_menu)
        self._table.currentCellChanged.connect(self._on_row_changed)
        splitter.addWidget(self._table)
        # Code preview panel
        code_w = QWidget(); code_lo = QVBoxLayout(code_w); code_lo.setContentsMargins(0,0,0,0); code_lo.setSpacing(2)
        self._code_label = QLabel("Select a function to view decompiled code"); self._code_label.setStyleSheet("color:#888; font-size:11px; padding:2px;"); code_lo.addWidget(self._code_label)
        self._code_view = QTextEdit(); self._code_view.setReadOnly(True)
        self._code_view.setFont(QFont("Consolas", 10)); self._code_view.setStyleSheet("background:#111; color:#ddd;")
        code_lo.addWidget(self._code_view)
        splitter.addWidget(code_w)
        splitter.setSizes([350, 250])
        lo.addWidget(splitter)
        # Status bar
        self._status = QLabel(""); self._status.setStyleSheet("color: #666; font-size: 11px;"); lo.addWidget(self._status)
        # Data
        self._symbols = []; self._filtered = []; self._sections = []; self._elf_info = None
        self._build = None
        # Ghidra data
        self._ghidra_funcs = []  # from parse_ghidra_c
        self._ghidra_xml_funcs = []  # from parse_ghidra_xml
        self._ghidra_lines = []  # raw .c file lines for code extraction
        self._func_by_name = {}  # name → ghidra_func for code lookup
        self._xml_func_by_addr = {}  # addr → xml_func for metadata

    def load_elf(self, elf_info, symbols=None, label="", build=None):
        """Load parsed ELF/DOL info and optionally override symbols (from .map file)."""
        self._elf_info = elf_info; self._build = build
        self._sections = elf_info.get('sections', [])
        self._symbols = symbols if symbols else elf_info.get('symbols', [])
        t = elf_info.get('type', 'elf').upper()
        dwarf = " + DWARF debug" if elf_info.get('has_dwarf') else ""
        ghidra_note = f" + {len(self._ghidra_funcs)} decompiled functions" if self._ghidra_funcs else ""
        self._info.setText(f"{label} — {t} | {len(self._symbols):,} symbols{dwarf}{ghidra_note}")
        ts = elf_info.get('text_size', 0); ds = elf_info.get('data_size', 0); bs = elf_info.get('bss_size', 0)
        sec_parts = []
        if ts: sec_parts.append(f".text: {ts:,}B")
        if ds: sec_parts.append(f".data: {ds:,}B")
        if bs: sec_parts.append(f".bss: {bs:,}B")
        sec_parts.append(f"Entry: 0x{elf_info.get('entry',0):08X}")
        self._sec_info.setText(" | ".join(sec_parts))
        self._filter()

    def load_ghidra_c(self, text, label=""):
        """Load and index a Ghidra .c decompilation file."""
        self._ghidra_lines = text.split('\n')
        self._ghidra_funcs = asura.parse_ghidra_c(text)
        self._func_by_name = {}
        for f in self._ghidra_funcs:
            self._func_by_name[f['name']] = f
            # Also index by short method name and various name forms
            if f['class'] and f['method']:
                self._func_by_name[f['method']] = f
                # Index by "Namespace::Class::Method" style from XML
                self._func_by_name[f'{f["class"]}::{f["method"]}'] = f
        # Cross-reference with XML if already loaded — link addresses to .c functions
        if self._ghidra_xml_funcs:
            for xf in self._ghidra_xml_funcs:
                xname = xf['name']  # e.g. "Global::ReadNewEnvironmentFormat"
                short = xname.rsplit('::', 1)[-1] if '::' in xname else xname
                ns = xf.get('namespace', '').rstrip('::')
                # Try multiple name forms to find match in .c
                for try_name in [xname, short, f"{ns}::{short}" if ns else None]:
                    if try_name and try_name in self._func_by_name:
                        self._func_by_name[f"addr_{xf['addr']:08x}"] = self._func_by_name[try_name]
                        break
        # If no symbols loaded yet, generate from .c + XML
        if not self._symbols:
            self._symbols = []
            if self._ghidra_xml_funcs:
                # Use XML as base (has addresses), link to .c code
                for f in self._ghidra_xml_funcs:
                    self._symbols.append({
                        'name': f['name'], 'addr': f['addr'], 'size': f['size'],
                        'bind': 'GLOBAL', 'type': 'FUNC', 'section': 4,
                        '_ghidra_sig': f['signature'], '_ghidra_class': f['namespace'].rstrip('::'),
                    })
                func_addrs = set(f['addr'] for f in self._ghidra_xml_funcs)
                for s in getattr(self, '_pending_xml_syms', []):
                    if s['addr'] not in func_addrs:
                        self._symbols.append(s)
            else:
                for f in self._ghidra_funcs:
                    self._symbols.append({
                        'name': f['name'], 'addr': 0, 'size': f['lines'],
                        'bind': 'GLOBAL', 'type': 'FUNC', 'section': 4,
                        '_ghidra_sig': f['signature'], '_ghidra_class': f['class'],
                    })
        # Update namespace filter
        classes = sorted(set(f['class'] for f in self._ghidra_funcs if f['class']))
        self._ns_filter.clear(); self._ns_filter.addItem("All Classes")
        for c in classes: self._ns_filter.addItem(c)
        info_txt = f"{label} — {len(self._ghidra_funcs):,} decompiled functions"
        if self._elf_info:
            t = self._elf_info.get('type', '').upper()
            info_txt = f"{label} — {t} + {len(self._symbols):,} symbols + {len(self._ghidra_funcs):,} decompiled"
        self._info.setText(info_txt)
        self._code_label.setText(f"Decompiled code loaded: {len(self._ghidra_funcs):,} functions ({len(self._ghidra_lines):,} lines)")
        self._filter()

    def load_ghidra_xml(self, path, label=""):
        """Load Ghidra XML export for rich metadata."""
        result = asura.parse_ghidra_xml(path)
        self._ghidra_xml_funcs = result['functions']
        self._xml_func_by_addr = {f['addr']: f for f in result['functions']}
        # If no symbols, use XML functions as symbols
        if not self._symbols:
            self._symbols = []
            for f in result['functions']:
                self._symbols.append({
                    'name': f['name'], 'addr': f['addr'], 'size': f['size'],
                    'bind': 'GLOBAL', 'type': 'FUNC', 'section': 4,
                    '_ghidra_sig': f['signature'], '_ghidra_class': f['namespace'].rstrip('::'),
                })
            # Add non-function symbols
            func_addrs = set(f['addr'] for f in result['functions'])
            for s in result['symbols']:
                if s['addr'] not in func_addrs:
                    self._symbols.append({
                        'name': s['name'], 'addr': s['addr'], 'size': 0,
                        'bind': 'GLOBAL', 'type': 'OBJECT', 'section': 1,
                    })
        # Update namespace filter from XML
        classes = sorted(set(ns.rstrip('::') for ns in result['namespaces'] if ns.rstrip('::')))
        self._ns_filter.clear(); self._ns_filter.addItem("All Classes")
        for c in classes[:500]: self._ns_filter.addItem(c)  # cap at 500 for UI performance
        n_funcs = len(result['functions']); n_syms = len(result['symbols'])
        self._sec_info.setText(f"XML: {n_funcs:,} functions | {n_syms:,} symbols | {len(result['namespaces']):,} classes")
        self._filter()

    def _filter(self):
        q = self._search.text().lower()
        tf = self._type_filter.currentText(); bf = self._bind_filter.currentText()
        nf = self._ns_filter.currentText()
        self._filtered = []
        for s in self._symbols:
            if tf != "All Types" and s.get('type','') != tf: continue
            if bf != "All Scope" and s.get('bind','') != bf: continue
            if nf != "All Classes":
                cls = s.get('_ghidra_class', '')
                if not cls:
                    # Try to extract from name
                    if '::' in s['name']:
                        cls = s['name'].rsplit('::', 1)[0]
                if cls != nf: continue
            if q:
                nm = s['name'].lower()
                dm = asura.demangle_ppc_name(s['name']).lower()
                sig = s.get('_ghidra_sig', '').lower()
                addr_s = f"{s['addr']:08x}"
                if q not in nm and q not in dm and q not in addr_s and q not in sig: continue
            self._filtered.append(s)
        self._refresh_table()

    def _refresh_table(self):
        dm = self._demangle_cb.isChecked()
        self._table.setRowCount(len(self._filtered))
        self._table.setSortingEnabled(False)
        for i, s in enumerate(self._filtered):
            self._table.setItem(i, 0, QTableWidgetItem(f"0x{s['addr']:08X}"))
            self._table.setItem(i, 1, QTableWidgetItem(f"0x{s['size']:X}" if s['size'] else ""))
            self._table.setItem(i, 2, QTableWidgetItem(s.get('type','')))
            self._table.setItem(i, 3, QTableWidgetItem(s.get('bind','')))
            self._table.setItem(i, 4, QTableWidgetItem(s['name']))
            # Show Ghidra signature if available, else demangled
            gsig = s.get('_ghidra_sig', '')
            col5 = gsig if gsig else (asura.demangle_ppc_name(s['name']) if dm else "")
            self._table.setItem(i, 5, QTableWidgetItem(col5))
        self._table.setSortingEnabled(True)
        self._status.setText(f"Showing {len(self._filtered):,} of {len(self._symbols):,} symbols")

    def _on_row_changed(self, row, col, prev_row, prev_col):
        if row < 0 or row >= len(self._filtered): return
        s = self._filtered[row]
        name = s['name']
        code = None; fi = None
        # Method 1: address-based cross-ref (most reliable when both XML+C loaded)
        if s['addr'] and f"addr_{s['addr']:08x}" in self._func_by_name:
            fi = self._func_by_name[f"addr_{s['addr']:08x}"]
        # Method 2: name match
        if not fi:
            short = name.rsplit('::', 1)[-1] if '::' in name else name
            for try_name in [name, short]:
                if try_name in self._func_by_name:
                    fi = self._func_by_name[try_name]; break
        # Method 3: fuzzy — match by class+method ignoring namespace prefix
        if not fi and '::' in name:
            parts = name.split('::')
            if len(parts) >= 2:
                for try_name in [f"{parts[-2]}::{parts[-1]}", parts[-1]]:
                    if try_name in self._func_by_name:
                        fi = self._func_by_name[try_name]; break
        if fi and self._ghidra_lines:
            code = asura.get_ghidra_func_code(self._ghidra_lines, fi)
            self._code_label.setText(f"{fi['name']} — lines {fi['start_line']}–{fi['end_line']} ({fi['lines']} lines)")
        if code:
            self._code_view.setPlainText(code)
        else:
            xml_f = self._xml_func_by_addr.get(s['addr'])
            if xml_f:
                lines = [f"// Function: {xml_f['name']}", f"// Address: 0x{xml_f['addr']:08X}", f"// Size: {xml_f['size']} bytes"]
                if xml_f['signature']: lines.append(f"// Signature: {xml_f['signature']}")
                if xml_f['return_type']: lines.append(f"// Returns: {xml_f['return_type']}")
                if xml_f['params']:
                    lines.append(f"// Parameters:")
                    for p in xml_f['params']: lines.append(f"//   {p['reg']:4s} {p['type']:20s} {p['name']}")
                if xml_f['stack_vars']:
                    lines.append(f"// Stack ({xml_f['stack_size']} bytes):")
                    for sv in xml_f['stack_vars'][:20]: lines.append(f"//   {sv['offset']:8s} {sv['type']:20s} {sv['name']}")
                lines.append("\n// (Load matching .c file for decompiled code)")
                self._code_view.setPlainText('\n'.join(lines))
                self._code_label.setText(f"{xml_f['name']} — XML metadata only")
            else:
                self._code_view.setPlainText(f"// No decompiled code for: {name}\n// Address: 0x{s['addr']:08X}")
                self._code_label.setText("No code — load Ghidra .c export")

    def _ctx_menu(self, pos):
        row = self._table.rowAt(pos.y())
        if row < 0 or row >= len(self._filtered): return
        s = self._filtered[row]
        menu = QMenu(self)
        menu.addAction("Copy Address").triggered.connect(lambda: QApplication.clipboard().setText(f"0x{s['addr']:08X}"))
        menu.addAction("Copy Name").triggered.connect(lambda: QApplication.clipboard().setText(s['name']))
        menu.addAction("Copy Demangled").triggered.connect(lambda: QApplication.clipboard().setText(asura.demangle_ppc_name(s['name'])))
        if s.get('_ghidra_sig'):
            menu.addAction("Copy Signature").triggered.connect(lambda: QApplication.clipboard().setText(s['_ghidra_sig']))
        # Copy current code
        if self._code_view.toPlainText():
            menu.addAction("Copy Code").triggered.connect(lambda: QApplication.clipboard().setText(self._code_view.toPlainText()))
        if self._build and s.get('type') in ('OBJECT',):
            menu.addSeparator()
            menu.addAction("Gecko: Set to 1 (enable)").triggered.connect(
                lambda: QApplication.clipboard().setText(asura.generate_gecko_code(s['addr'], 1)))
            menu.addAction("Gecko: Set to 0 (disable)").triggered.connect(
                lambda: QApplication.clipboard().setText(asura.generate_gecko_code(s['addr'], 0)))
        menu.exec(self._table.viewport().mapToGlobal(pos))

    def export_csv(self, path):
        import csv
        with open(path, 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(['Address', 'Size', 'Type', 'Bind', 'Name', 'Demangled', 'Signature'])
            for s in self._symbols:
                w.writerow([f'0x{s["addr"]:08X}', f'0x{s["size"]:X}', s.get('type',''),
                           s.get('bind',''), s['name'], asura.demangle_ppc_name(s['name']),
                           s.get('_ghidra_sig', '')])


class GeckoCodeDialog(QDialog):
    """Dialog for generating Dolphin Gecko cheat codes."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Gecko Code Generator — The Simpsons Game (Wii)")
        self.resize(700, 600)
        lo = QVBoxLayout(self); lo.setSpacing(8)
        # Build selector
        brow = QHBoxLayout()
        brow.addWidget(QLabel("Build:"))
        self._build = QComboBox(); self._build.addItems(["Final", "Proto"]); brow.addWidget(self._build)
        brow.addStretch()
        lo.addLayout(brow)
        # Tabs for different code categories
        tabs = QTabWidget(); lo.addWidget(tabs)
        # Tab 1: Debug Variables
        var_w = QWidget(); var_lo = QVBoxLayout(var_w); var_lo.setContentsMargins(4,4,4,4)
        var_search = QLineEdit(); var_search.setPlaceholderText("Filter variables..."); var_lo.addWidget(var_search)
        self._var_table = QTableWidget(); self._var_table.setColumnCount(5)
        self._var_table.setHorizontalHeaderLabels(["Enable", "Variable", "Category", "Type", "Value"])
        self._var_table.horizontalHeader().setStretchLastSection(True)
        self._var_table.setColumnWidth(0, 50); self._var_table.setColumnWidth(1, 250)
        self._var_table.setColumnWidth(2, 100); self._var_table.setColumnWidth(3, 50)
        self._var_table.setAlternatingRowColors(True)
        var_lo.addWidget(self._var_table)
        self._vars = asura.get_debug_variables()
        self._var_checks = []
        self._var_table.setRowCount(len(self._vars))
        for i, v in enumerate(self._vars):
            cb = QCheckBox(); self._var_checks.append(cb)
            self._var_table.setCellWidget(i, 0, cb)
            self._var_table.setItem(i, 1, QTableWidgetItem(v['name']))
            self._var_table.setItem(i, 2, QTableWidgetItem(v['category']))
            self._var_table.setItem(i, 3, QTableWidgetItem(v['type']))
            val_item = QTableWidgetItem(str(v['max'] if v['type'] == 'bool' else v['default']))
            self._var_table.setItem(i, 4, val_item)
        var_search.textChanged.connect(self._filter_vars)
        tabs.addTab(var_w, "Debug Variables")
        # Tab 2: Splitscreen
        split_w = QWidget(); split_lo = QVBoxLayout(split_w); split_lo.setContentsMargins(4,4,4,4)
        prow = QHBoxLayout()
        prow.addWidget(QLabel("Players:"))
        self._players = QComboBox(); self._players.addItems(["2", "3", "4"]); self._players.setCurrentIndex(2); prow.addWidget(self._players)
        prow.addStretch()
        gen_btn = QPushButton("Generate Splitscreen Codes"); gen_btn.clicked.connect(self._gen_split); prow.addWidget(gen_btn)
        split_lo.addLayout(prow)
        self._split_out = QTextEdit(); self._split_out.setReadOnly(True); self._split_out.setFont(QFont("Consolas", 10))
        split_lo.addWidget(self._split_out)
        tabs.addTab(split_w, "Splitscreen")
        # Tab 3: Custom Gecko
        custom_w = QWidget(); custom_lo = QVBoxLayout(custom_w); custom_lo.setContentsMargins(4,4,4,4)
        crow = QHBoxLayout()
        crow.addWidget(QLabel("Address: 0x")); self._custom_addr = QLineEdit(); self._custom_addr.setMaximumWidth(120); crow.addWidget(self._custom_addr)
        crow.addWidget(QLabel("Value:")); self._custom_val = QLineEdit(); self._custom_val.setMaximumWidth(120); crow.addWidget(self._custom_val)
        crow.addWidget(QLabel("Size:"))
        self._custom_size = QComboBox(); self._custom_size.addItems(["Byte (1)", "Half (2)", "Word (4)"]); self._custom_size.setCurrentIndex(2); crow.addWidget(self._custom_size)
        gen_c = QPushButton("Generate"); gen_c.clicked.connect(self._gen_custom); crow.addWidget(gen_c)
        crow.addStretch()
        custom_lo.addLayout(crow)
        self._custom_out = QTextEdit(); self._custom_out.setReadOnly(True); self._custom_out.setFont(QFont("Consolas", 10))
        custom_lo.addWidget(self._custom_out)
        tabs.addTab(custom_w, "Custom Code")
        # Bottom buttons
        brow2 = QHBoxLayout()
        gen_all = QPushButton("Generate All Selected"); gen_all.clicked.connect(self._gen_all); brow2.addWidget(gen_all)
        copy_btn = QPushButton("Copy to Clipboard"); copy_btn.clicked.connect(self._copy_all); brow2.addWidget(copy_btn)
        brow2.addStretch()
        close_btn = QPushButton("Close"); close_btn.clicked.connect(self.close); brow2.addWidget(close_btn)
        lo.addLayout(brow2)
        self._output = QTextEdit(); self._output.setReadOnly(True); self._output.setFont(QFont("Consolas", 10))
        self._output.setMaximumHeight(150)
        lo.addWidget(QLabel("Generated Codes:")); lo.addWidget(self._output)
        self._symbols_proto = []; self._symbols_final = []

    def set_symbols(self, proto_syms, final_syms):
        self._symbols_proto = proto_syms
        self._symbols_final = final_syms

    def _filter_vars(self, text):
        t = text.lower()
        for i, v in enumerate(self._vars):
            show = not t or t in v['name'].lower() or t in v['category'].lower()
            self._var_table.setRowHidden(i, not show)

    def _get_build(self):
        return 'proto' if self._build.currentText() == 'Proto' else 'final'

    def _gen_split(self):
        build = self._get_build()
        n = int(self._players.currentText())
        codes = asura.generate_splitscreen_gecko(build, n)
        lines = [f"${n}-Player Splitscreen ({build.title()} Build)"]
        for desc, code in codes:
            lines.append(f"{code}  # {desc}")
        self._split_out.setText('\n'.join(lines))

    def _gen_custom(self):
        try:
            addr = int(self._custom_addr.text(), 16)
            val_str = self._custom_val.text().strip()
            if '.' in val_str:
                code = asura.generate_gecko_float(addr, float(val_str))
            else:
                sz = [1,2,4][self._custom_size.currentIndex()]
                code = asura.generate_gecko_code(addr, int(val_str, 0), sz)
            self._custom_out.setText(code)
        except Exception as e:
            self._custom_out.setText(f"Error: {e}")

    def _gen_all(self):
        build = self._get_build()
        syms = self._symbols_proto if build == 'proto' else self._symbols_final
        lines = [f"$Debug Variables ({build.title()} Build)"]
        for i, v in enumerate(self._vars):
            if not self._var_checks[i].isChecked(): continue
            # Find address in symbol table
            addr = asura.find_symbol_address(syms, v['name']) if syms else None
            if addr is None:
                lines.append(f"# {v['name']} — address not found in {build} symbols")
                continue
            val_str = self._var_table.item(i, 4).text()
            if v['type'] == 'float':
                try: code = asura.generate_gecko_float(addr, float(val_str))
                except: code = f"# Error parsing float for {v['name']}"
            elif v['type'] == 'bool':
                code = asura.generate_gecko_code(addr, 1 if val_str.strip() in ('1','True','true') else 0, 1)
            else:
                try: code = asura.generate_gecko_code(addr, int(val_str, 0))
                except: code = f"# Error parsing int for {v['name']}"
            lines.append(f"{code}  # {v['name']}")
        self._output.setText('\n'.join(lines))

    def _copy_all(self):
        QApplication.clipboard().setText(self._output.toPlainText())


# ============================================================
# New-Gen (EARS Engine) Support Classes
# ============================================================

NG_RTYPE_ICONS = {
    'EARS_MESH': '🔷', 'MetaModel': '📦', 'EARS_ITXD': '🖼️',
    'HKO': '⚙️', 'HKT': '⚙️', 'VFX': '✨', 'BNK': '🔊',
    'SBK': '🎵', 'AMX': '🎶', 'LH2': '📝', 'TOB': '💬',
    'CHA': '🗣️', 'CHT': '🗣️', 'GRAPH': '🗺️', 'BSP': '🏗️',
    'StreamTOC': '📂', 'UIX': '🖥️', 'FFN': '🔤', 'RCB': '🎬',
    'SMB': '📻', 'TRINITY_SEQ_MASTER': '🎥',
}

class NGResourceManager:
    """Cross-file resource manager for new-gen STR archives."""
    def __init__(self):
        self.game_dir = None
        self.str_files = {}     # path → entry dict
        self.resources = {}     # (filename, type) → resource data
        self.entity_cache = {}

    def set_game_directory(self, path):
        self.game_dir = path
        self.str_files.clear()
        self.resources.clear()
        self.entity_cache.clear()
        str_paths = []
        for root, dirs, files in os.walk(path):
            for f in sorted(files):
                if f.lower().endswith('.str'):
                    str_paths.append(os.path.join(root, f))
        return str_paths

    def load_str(self, path):
        data = open(path, 'rb').read()
        try:
            assets, sgs, header = ng.extract_str_assets(data)
        except Exception as e:
            return None, str(e)
        rel_path = os.path.relpath(path, self.game_dir) if self.game_dir else os.path.basename(path)
        entry = {'path': path, 'rel_path': rel_path, 'size': len(data),
                 'assets': assets, 'simgroups': sgs, 'header': header}
        self.str_files[path] = entry
        for a in assets:
            self.resources[(a.get('filename',''), a.get('resource_type',''))] = a
        return entry, None

    def get_entity_list(self, str_path):
        if str_path in self.entity_cache:
            return self.entity_cache[str_path]
        entry = self.str_files.get(str_path)
        if not entry: return []
        all_ents = []
        for sg in entry['simgroups']:
            if sg.get('n_entities', 0) > 0:
                try: all_ents.extend(ng.extract_all_entities(sg['raw_data'], sg['n_entities']))
                except: pass
        self.entity_cache[str_path] = all_ents
        return all_ents


class NGLoadThread(QThread if 'QThread' in dir() else object):
    """Background loader for new-gen STR files."""
    progress = Signal(str, int, int)
    finished_loading = Signal(str, object, str)

    def __init__(self, res_mgr, paths):
        super().__init__()
        self.res_mgr = res_mgr
        self.paths = paths

    def run(self):
        for i, path in enumerate(self.paths):
            self.progress.emit(f"Loading {os.path.basename(path)}...", i, len(self.paths))
            entry, err = self.res_mgr.load_str(path)
            self.finished_loading.emit(path, entry, err or '')
        self.progress.emit("Done", len(self.paths), len(self.paths))


class AsuraExplorer(QMainWindow):
    def __init__(self):
        super().__init__(); self.setWindowTitle(f"{APP_NAME} v{VERSION}"); self.resize(1400,850); self.setMinimumSize(1024, 600)
        self._chunks=[]; self._files=[]; self._data=None; self._path=""; self._emap={}; self._amap={}; self._model_tex_map={}; self._bik_clips=[]
        self._nlld_chunk_indices=[]; self._txth_entries=[]; self._txth_chunk_idx=-1
        self._elf_data = {}  # 'proto_elf', 'final_elf', 'proto_map', 'final_map' etc
        # New-gen state
        self._ng_resmgr = NGResourceManager() if HAS_NEWGEN else None
        self._ng_mode = False  # True when viewing new-gen data
        self._setup_ui(); self._setup_menus(); self.statusBar().showMessage("Ready — Open a .wii or .str file to begin")

    def _setup_ui(self):
        self.sp=QSplitter(Qt.Horizontal); self.setCentralWidget(self.sp)
        left=QWidget(); ll=QVBoxLayout(left); ll.setContentsMargins(0,0,0,0); ll.setSpacing(0)
        self.filt=QLineEdit(); self.filt.setPlaceholderText("Filter..."); self.filt.textChanged.connect(self._filter_tree); ll.addWidget(self.filt)
        self.tree=QTreeWidget(); self.tree.setHeaderLabels(["Name","Size","Info"]); self.tree.setColumnWidth(0,260); self.tree.setColumnWidth(1,70)
        self.tree.setAlternatingRowColors(True); self.tree.itemClicked.connect(self._click)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu); self.tree.customContextMenuRequested.connect(self._ctx); ll.addWidget(self.tree)
        left.setMinimumWidth(200); self.sp.addWidget(left)
        self.vs=QStackedWidget()
        self.welcome=QLabel(f"<div style='text-align:center;padding:60px;'><h1 style='color:#e0a030;'>{APP_NAME}</h1><p style='color:#888;'>Version {VERSION}</p><p style='color:#666;'>Open a .wii file (Wii/Asura) or .str file (PS3/X360/EARS)</p><p style='color:#555;font-size:11px;margin-top:20px;'>The Simpsons Game (2007)</p></div>")
        self.welcome.setAlignment(Qt.AlignCenter)
        self.chunkv=ChunkPropsView(); self.texpanel=TexturePanel(); self.audv=AudioPlayer(); self.dlgv=DialogueView(); self.modv=ModelViewer(); self.scrv=ScriptView(); self.animv=AnimationView(); self.lvlv=LevelView(); self.lvlmap=LevelViewer()
        self.animv._owner_ref = self  # for texture loading
        self.elfv = ElfBrowser()  # ELF/DOL symbol browser
        if LevelViewport3D:
            self.lvl3d = LevelViewport3D()
        else:
            self.lvl3d = QLabel("<div style='text-align:center;padding:40px;color:#888;'><h2>3D Viewport requires PyOpenGL</h2><p><code>pip install PyOpenGL</code></p></div>")
            self.lvl3d.setAlignment(Qt.AlignCenter)
        for w in [self.welcome,self.chunkv,self.texpanel,self.audv,self.dlgv,self.modv,self.scrv,self.animv,self.lvlv,self.lvlmap,self.lvl3d,self.elfv]: self.vs.addWidget(w)
        # Connect DialogueView edit signal to undo stack
        self.dlgv.editApplied.connect(self._on_dlg_edit)
        self.scrv.editApplied.connect(self._on_dlg_edit)  # same handler works
        self.sp.addWidget(self.vs); self.sp.setSizes([320,1080]); self.sp.setStretchFactor(0,0); self.sp.setStretchFactor(1,1)

        # Property panel (editor mode) — right dock
        self._prop_dock = QDockWidget("Properties", self)
        self._prop_dock.setAllowedAreas(Qt.RightDockWidgetArea | Qt.BottomDockWidgetArea)
        prop_w = QWidget(); prop_lo = QVBoxLayout(prop_w); prop_lo.setContentsMargins(6,6,6,6); prop_lo.setSpacing(4)
        self._prop_title = QLabel("No Selection"); self._prop_title.setStyleSheet("font-weight:bold; font-size:13px;"); prop_lo.addWidget(self._prop_title)
        self._prop_table = QTableWidget(); self._prop_table.setColumnCount(2)
        self._prop_table.setHorizontalHeaderLabels(["Property", "Value"])
        self._prop_table.horizontalHeader().setStretchLastSection(True)
        self._prop_table.setAlternatingRowColors(True)
        self._prop_table.cellChanged.connect(self._on_prop_changed)
        prop_lo.addWidget(self._prop_table)
        # Entity action buttons
        btn_lo = QHBoxLayout()
        self._btn_delete = QPushButton("Delete"); self._btn_delete.clicked.connect(self._delete_selected)
        self._btn_duplicate = QPushButton("Duplicate"); self._btn_duplicate.clicked.connect(self._duplicate_selected)
        self._btn_import = QPushButton("Import Entity..."); self._btn_import.clicked.connect(self._import_entity)
        btn_lo.addWidget(self._btn_delete); btn_lo.addWidget(self._btn_duplicate); btn_lo.addWidget(self._btn_import)
        prop_lo.addLayout(btn_lo)
        self._prop_dock.setWidget(prop_w)
        self.addDockWidget(Qt.RightDockWidgetArea, self._prop_dock)
        self._prop_dock.hide()  # hidden until edit mode enabled
        self._selected_chunk_idx = -1  # index into self._chunks
        self._prop_updating = False  # prevent recursive cellChanged

    def _setup_menus(self):
        fm=self.menuBar().addMenu("&File")
        oa=QAction("&Open (Wii)...",self); oa.setShortcut(QKeySequence.Open); oa.triggered.connect(self._open); fm.addAction(oa)
        if HAS_NEWGEN:
            fm.addSeparator()
            sa=QAction("Open &STR File (New-Gen)...",self); sa.setShortcut("Ctrl+Shift+O"); sa.triggered.connect(self._open_str); fm.addAction(sa)
            da=QAction("Open Game &Directory (New-Gen)...",self); da.setShortcut("Ctrl+D"); da.triggered.connect(self._open_game_dir); fm.addAction(da)
            sfa=QAction("Open Sub&folder (New-Gen)...",self); sfa.setShortcut("Ctrl+Shift+D"); sfa.triggered.connect(self._open_subfolder); fm.addAction(sfa)
        fm.addSeparator()
        ea=QAction("Open &ELF/DOL...",self); ea.triggered.connect(self._open_elf); fm.addAction(ea)
        self._recent_menu = fm.addMenu("Recent Files"); self._recent_paths = []
        self._load_recent()
        fm.addSeparator()
        self._save_act = QAction("&Save Modified...", self); self._save_act.setShortcut("Ctrl+S")
        self._save_act.triggered.connect(self._save_modified); self._save_act.setEnabled(False); fm.addAction(self._save_act)
        fm.addSeparator()
        for label,fn in [("Export All &Textures...",self._exp_tex),("Export All &Models...",self._exp_mod),("Export All &Audio...",self._exp_aud),("Export &Level OBJ+MTL...",self._exp_level)]:
            a=QAction(label,self); a.triggered.connect(fn); fm.addAction(a)
        fm.addSeparator()
        ba=QAction("&Batch Export Multiple Files...",self); ba.triggered.connect(self._batch_export); fm.addAction(ba)
        fm.addSeparator(); qa=QAction("&Quit",self); qa.setShortcut("Ctrl+Q"); qa.triggered.connect(self.close); fm.addAction(qa)

        # Edit menu
        em = self.menuBar().addMenu("&Edit")
        self._edit_mode_act = QAction("Edit &Mode", self); self._edit_mode_act.setCheckable(True)
        self._edit_mode_act.setShortcut("Ctrl+E"); self._edit_mode_act.toggled.connect(self._toggle_edit_mode); em.addAction(self._edit_mode_act)
        em.addSeparator()
        self._undo_act = QAction("&Undo", self); self._undo_act.setShortcut("Ctrl+Z"); self._undo_act.triggered.connect(self._undo); self._undo_act.setEnabled(False); em.addAction(self._undo_act)
        self._redo_act = QAction("&Redo", self); self._redo_act.setShortcut("Ctrl+Shift+Z"); self._redo_act.triggered.connect(self._redo); self._redo_act.setEnabled(False); em.addAction(self._redo_act)

        # Tools menu
        tm = self.menuBar().addMenu("&Tools")
        ga = QAction("&Gecko Code Generator...", self); ga.triggered.connect(self._show_gecko_dialog); tm.addAction(ga)
        pa = QAction("&Patch DOL (Splitscreen)...", self); pa.triggered.connect(self._patch_dol); tm.addAction(pa)
        tm.addSeparator()
        xs = QAction("Export &Symbols CSV...", self); xs.triggered.connect(self._export_symbols_csv); tm.addAction(xs)

        # Editor state
        self._edit_mode = False
        self._undo_stack = []  # list of (description, undo_fn, redo_fn)
        self._redo_stack = []
        self._dirty = False

    def _load_recent(self):
        cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.asura_recent')
        if os.path.isfile(cfg):
            try: self._recent_paths = [l.strip() for l in open(cfg) if l.strip() and os.path.isfile(l.strip())][:10]
            except: pass
        self._update_recent_menu()

    def _add_recent(self, path):
        path = os.path.abspath(path)
        self._recent_paths = [path] + [p for p in self._recent_paths if p != path]
        self._recent_paths = self._recent_paths[:10]
        cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.asura_recent')
        try: open(cfg, 'w').write('\n'.join(self._recent_paths))
        except: pass
        self._update_recent_menu()

    def _update_recent_menu(self):
        self._recent_menu.clear()
        for p in self._recent_paths:
            a = self._recent_menu.addAction(os.path.basename(p))
            a.setToolTip(p)
            a.triggered.connect(lambda _=False, _p=p: self._load(_p))
        self._recent_menu.setEnabled(bool(self._recent_paths))

    def _open(self):
        p,_=QFileDialog.getOpenFileName(self,"Open","","Asura Files (*.wii *.enBE *.asrBE *.asr *.guiBE);;All (*)")
        if p: self._load(p)

    # ---- New-Gen (EARS Engine) Loading ----

    def _open_str(self):
        if not HAS_NEWGEN: return
        p,_=QFileDialog.getOpenFileName(self,"Open STR File","","STR Archives (*.str);;All (*)")
        if p:
            self._ng_resmgr.game_dir = os.path.dirname(p)
            self._load_single_str(p)

    def _open_game_dir(self):
        if not HAS_NEWGEN: return
        p=QFileDialog.getExistingDirectory(self,"Select Game Directory (contains .str files)")
        if p: self._load_game_dir(p)

    def _open_subfolder(self):
        if not HAS_NEWGEN: return
        p=QFileDialog.getExistingDirectory(self,"Select Subfolder (e.g., spr_hub, mob_rules)")
        if p: self._load_game_dir(p)

    def _load_game_dir(self, path):
        self.statusBar().showMessage(f"Scanning {path}..."); QApplication.processEvents()
        str_paths = self._ng_resmgr.set_game_directory(path)
        if not str_paths:
            QMessageBox.warning(self, "No STR Files", f"No .str files found in:\n{path}"); return
        self.tree.clear(); self._ng_mode = True
        self._ng_load_thread = NGLoadThread(self._ng_resmgr, str_paths)
        self._ng_load_thread.progress.connect(lambda m,c,t: self.statusBar().showMessage(f"{m} ({c}/{t})"))
        self._ng_load_thread.finished_loading.connect(self._on_ng_str_loaded)
        self._ng_load_thread.finished.connect(self._on_ng_load_complete)
        self._ng_load_thread.start()

    def _load_single_str(self, path):
        self.tree.clear(); self._ng_mode = True
        self.statusBar().showMessage(f"Loading {os.path.basename(path)}..."); QApplication.processEvents()
        entry, err = self._ng_resmgr.load_str(path)
        if err:
            QMessageBox.warning(self, "Error", f"Failed to load:\n{err}"); return
        self._add_ng_str_to_tree(path, entry)
        n_a = len(entry['assets']); n_e = sum(sg.get('n_entities',0) for sg in entry['simgroups'])
        self.statusBar().showMessage(f"Loaded {os.path.basename(path)}: {n_a} resources, {n_e} entities")

    def _on_ng_str_loaded(self, path, entry, error):
        if entry and not error:
            self._add_ng_str_to_tree(path, entry)

    def _on_ng_load_complete(self):
        total_res = sum(len(e['assets']) for e in self._ng_resmgr.str_files.values())
        n_files = len(self._ng_resmgr.str_files)
        self.statusBar().showMessage(f"Loaded {n_files} STR files: {total_res:,} resources")

    def _add_ng_str_to_tree(self, path, entry):
        rel = entry.get('rel_path', os.path.basename(path))
        n_assets = len(entry['assets']); n_ents = sum(sg.get('n_entities',0) for sg in entry['simgroups'])
        str_node = QTreeWidgetItem(self.tree)
        str_node.setText(0, f"📁 {rel}"); str_node.setText(1, f"{entry['size']//1024}KB"); str_node.setText(2, f"{n_assets} res")
        str_node.setData(0, Qt.UserRole, ('ng_str', path))
        str_node.setForeground(0, QColor("#e0a030"))
        by_type = defaultdict(list)
        for a in entry['assets']:
            by_type[a.get('resource_type','Unknown')].append(a)
        for rtype in sorted(by_type.keys()):
            assets = by_type[rtype]; icon = NG_RTYPE_ICONS.get(rtype, '📄')
            type_node = QTreeWidgetItem(str_node)
            type_node.setText(0, f"{icon} {rtype} ({len(assets)})"); type_node.setText(1, rtype)
            type_node.setData(0, Qt.UserRole, ('ng_type_group', rtype, path))
            for a in assets:
                leaf = QTreeWidgetItem(type_node)
                leaf.setText(0, a.get('filename','?')); leaf.setText(1, f"{len(a.get('data',b'')):,}"); leaf.setText(2, rtype)
                leaf.setData(0, Qt.UserRole, ('ng_resource', a, path))
        if n_ents > 0:
            ent_node = QTreeWidgetItem(str_node)
            ent_node.setText(0, f"🎮 Entities ({n_ents})"); ent_node.setText(1, "SimGroup")
            ent_node.setData(0, Qt.UserRole, ('ng_entities', path))

    def _ng_show_resource(self, asset, str_path):
        """Show a new-gen resource in the appropriate panel."""
        rtype = asset.get('resource_type', '')
        data = asset.get('data', b'')
        fn = asset.get('filename', '?')
        parsed = None
        try:
            if rtype == 'EARS_MESH':
                parsed = ng.parse_ears_mesh(data)
                if parsed and any(sm.get('positions') for sm in parsed.get('submeshes',[])):
                    # Show mesh info in chunk props view
                    total_v = sum(len(sm.get('positions',[])) for sm in parsed['submeshes'])
                    total_t = sum(len(sm.get('triangles',[])) for sm in parsed['submeshes'])
                    info = f"EARS_MESH: {fn}\n{total_v:,} vertices, {total_t:,} triangles\n{parsed['submesh_count']} submeshes\n"
                    for si, sm in enumerate(parsed['submeshes']):
                        nv = len(sm.get('positions',[])); nt = len(sm.get('triangles',[]))
                        info += f"\nSubmesh {si}: {nv} verts, {nt} tris"
                        if sm.get('blend_indices'): info += " (skinned)"
                        if sm.get('positions'):
                            xs=[p[0] for p in sm['positions']]; ys=[p[1] for p in sm['positions']]; zs=[p[2] for p in sm['positions']]
                            info += f"\n  bounds: ({min(xs):.2f}..{max(xs):.2f}, {min(ys):.2f}..{max(ys):.2f}, {min(zs):.2f}..{max(zs):.2f})"
                        for ve in sm.get('vertex_elements',[]):
                            info += f"\n  +{ve['offset']:2d}: {ve.get('type_name','?'):10s} → {ve.get('usage_name','?')}:{ve['usage_idx']}"
                    self.chunkv._hex.setPlainText(info)
                    self.chunkv._title.setText(f"🔷  {fn}")
                    self.vs.setCurrentWidget(self.chunkv); return
            elif rtype == 'LH2':
                parsed = ng.parse_lh2(data)
                if parsed and parsed.get('entries'):
                    lines = [f"LH2 Localized Text: {fn}", f"{len(parsed['entries'])} entries\n"]
                    for e in parsed['entries']:
                        lines.append(f"  [{e.get('hash',0):#010x}] {e.get('label','')}: {e.get('text','')}")
                    self.chunkv._hex.setPlainText('\n'.join(lines))
                    self.chunkv._title.setText(f"📝  {fn}"); self.vs.setCurrentWidget(self.chunkv); return
            elif rtype == 'BNK':
                parsed = ng.parse_bnk(data)
                if parsed:
                    lines = [f"BNK Sound Bank: {fn}", f"v{parsed['version']}  GUID: {parsed['guid']}", f"Groups: {parsed['n_groups']}, Sounds: {parsed['n_sounds']}\n"]
                    if parsed['emx_refs']:
                        lines.append(f"EMX References ({len(parsed['emx_refs'])}):")
                        for e in sorted(parsed['emx_refs']): lines.append(f"  {e}")
                    if parsed['anim_events']:
                        lines.append(f"\nAnimation Events ({len(parsed['anim_events'])}):")
                        for e in sorted(parsed['anim_events'])[:50]: lines.append(f"  {e}")
                    self.chunkv._hex.setPlainText('\n'.join(lines))
                    self.chunkv._title.setText(f"🔊  {fn}"); self.vs.setCurrentWidget(self.chunkv); return
            elif rtype == 'SMB':
                parsed = ng.parse_smb(data)
                if parsed:
                    lines = [f"Streaming Media Bank: {fn}", f"{parsed['n_entries']} dialogue clips, {len(parsed.get('voice_summary',{}))} characters\n"]
                    for char in sorted(parsed.get('voice_summary',{}).keys()):
                        lines.append(f"  {char:20s}: {parsed['voice_summary'][char]:3d} clips")
                    lines.append(f"\nAll clips:")
                    for e in parsed['entries']:
                        lines.append(f"  {e['character']:12s} {e['exa_name']:35s} → {e['snu_filename']}")
                    self.chunkv._hex.setPlainText('\n'.join(lines))
                    self.chunkv._title.setText(f"📻  {fn}"); self.vs.setCurrentWidget(self.chunkv); return
            elif rtype == 'CHA':
                parsed = ng.parse_cha(data)
                if parsed:
                    # Cross-reference with SMB
                    smb_lookup = {}
                    entry = self._ng_resmgr.str_files.get(str_path)
                    if entry:
                        for a in entry['assets']:
                            if a.get('resource_type') == 'SMB':
                                smb_p = ng.parse_smb(a['data'])
                                if smb_p:
                                    smb_lookup = {e['guid_suffix']: e for e in smb_p['entries']}
                                break
                    lines = [f"Chatter Alias Bank: {fn}", f"{parsed['n_entries']} entries\n"]
                    for i, e in enumerate(parsed['entries']):
                        smb_e = smb_lookup.get(e['guid_suffix'])
                        if smb_e:
                            lines.append(f"  [{i:2d}] 0x{e['bank_hash']:08X} → {smb_e['exa_name']} ({smb_e['character']})")
                        else:
                            lines.append(f"  [{i:2d}] 0x{e['bank_hash']:08X} → guid={e['guid']}")
                    self.chunkv._hex.setPlainText('\n'.join(lines))
                    self.chunkv._title.setText(f"🗣️  {fn}"); self.vs.setCurrentWidget(self.chunkv); return
            elif rtype == 'CHT':
                parsed = ng.parse_cht(data)
                if parsed:
                    events = parsed.get('events', [])
                    lines = [f"Chatter Template: {fn}", f"{len(events)} events\n"]
                    for e in events: lines.append(f"  {e}")
                    self.chunkv._hex.setPlainText('\n'.join(lines))
                    self.chunkv._title.setText(f"🗣️  {fn}"); self.vs.setCurrentWidget(self.chunkv); return
            else:
                # Generic parser
                parsers = {
                    'MetaModel': ng.parse_metamodel, 'EARS_ITXD': ng.parse_ears_itxd,
                    'HKO': ng.parse_havok, 'HKT': ng.parse_havok, 'BSP': ng.parse_bsp,
                    'GRAPH': ng.parse_graph, 'TOB': ng.parse_tob, 'StreamTOC': ng.parse_stream_toc,
                    'UIX': ng.parse_uix, 'FFN': ng.parse_ffn, 'SBK': ng.parse_sbk_header,
                    'VariableDictionary': ng.parse_variable_dict, 'TRINITY_SEQ_MASTER': ng.parse_trinity,
                    'AMX': ng.parse_amb,
                }
                parser = parsers.get(rtype)
                if parser:
                    parsed = parser(data)
        except Exception as e:
            parsed = {'parse_error': str(e)}

        # Fallback: show parsed dict or hex
        if parsed:
            lines = [f"{rtype}: {fn}\n"]
            for k, v in parsed.items():
                if isinstance(v, list) and len(v) > 10:
                    lines.append(f"  {k}: [{len(v)} items]")
                    for item in v[:5]: lines.append(f"    {item}")
                    lines.append(f"    ...")
                elif isinstance(v, dict) and len(str(v)) > 200:
                    lines.append(f"  {k}: {{...}}")
                else:
                    lines.append(f"  {k}: {v}")
            self.chunkv._hex.setPlainText('\n'.join(lines))
            self.chunkv._title.setText(f"📋  {fn}"); self.vs.setCurrentWidget(self.chunkv)
        else:
            # Raw hex
            lines = [f"Raw data: {len(data):,} bytes  {rtype}: {fn}\n"]
            for i in range(0, min(4096, len(data)), 16):
                hex_str = ' '.join(f'{b:02x}' for b in data[i:i+16])
                asc = ''.join(chr(b) if 32<=b<127 else '.' for b in data[i:i+16])
                lines.append(f"  {i:06x}: {hex_str:<48s}  {asc}")
            if len(data) > 4096: lines.append(f"\n  ... ({len(data)-4096:,} more bytes)")
            self.chunkv._hex.setPlainText('\n'.join(lines))
            self.chunkv._title.setText(f"🔢  {fn}"); self.vs.setCurrentWidget(self.chunkv)

    def _ng_show_entities(self, str_path):
        """Show new-gen entity list in a text view."""
        entities = self._ng_resmgr.get_entity_list(str_path)
        lines = [f"Entities: {len(entities)}\n"]
        classes = defaultdict(int)
        for e in entities:
            classes[e['class']] += 1
        lines.append("By class:")
        for cls in sorted(classes.keys()):
            lines.append(f"  {cls:30s}: {classes[cls]:4d}")
        lines.append(f"\nAll entities:")
        for e in entities[:200]:
            pos_str = ""
            for b in e.get('behaviors', []):
                for ai, (t, v) in b.get('attrs', {}).items():
                    if t == 'matrix':
                        p = v.get('pos', (0,0,0)); pos_str = f"({p[0]:.1f}, {p[1]:.1f}, {p[2]:.1f})"; break
                if pos_str: break
            beh_names = [b['name'] for b in e.get('behaviors',[]) if not b['name'].startswith('UNKNOWN')]
            lines.append(f"  [{e.get('index','?'):4}] {e['class']:25s} {pos_str:25s} {', '.join(beh_names[:3])}")
        if len(entities) > 200: lines.append(f"\n  ... ({len(entities)-200} more)")
        self.chunkv._hex.setPlainText('\n'.join(lines))
        self.chunkv._title.setText(f"🎮  Entities ({len(entities)})"); self.vs.setCurrentWidget(self.chunkv)

    def _ng_export_obj(self, asset):
        """Export new-gen EARS_MESH as OBJ."""
        parsed = ng.parse_ears_mesh(asset.get('data', b''))
        if not parsed: self.statusBar().showMessage("Failed to parse mesh"); return
        fn = asset.get('filename','mesh').replace('.dff','').replace('.rws','')
        path,_ = QFileDialog.getSaveFileName(self, "Export as OBJ", f"{fn}.obj", "OBJ (*.obj)")
        if not path: return
        obj_text, mtl_text = ng.export_ears_mesh_obj(parsed, fn)
        if obj_text:
            open(path,'w').write(obj_text)
            if mtl_text: open(path.rsplit('.',1)[0]+'.mtl','w').write(mtl_text)
            total_v = sum(len(sm.get('positions',[])) for sm in parsed['submeshes'])
            self.statusBar().showMessage(f"Exported {path} ({total_v:,} vertices)")

    def _ng_export_raw(self, asset):
        fn = asset.get('filename', 'resource.bin')
        path, _ = QFileDialog.getSaveFileName(self, "Export Resource", fn)
        if path:
            open(path, 'wb').write(asset.get('data', b''))
            self.statusBar().showMessage(f"Exported to {path}")

    def _ng_export_entities(self, str_path):
        import json
        entities = self._ng_resmgr.get_entity_list(str_path)
        path, _ = QFileDialog.getSaveFileName(self, "Export Entities", "entities.json", "JSON (*.json)")
        if not path: return
        out = []
        for e in entities:
            entry = {'index': e.get('index'), 'class': e['class'],
                     'class_hash': f"0x{e.get('class_hash',0):08X}", 'behaviors': []}
            for b in e.get('behaviors', []):
                beh = {'name': b['name'], 'hash': f"0x{b['hash']:08X}", 'attrs': {}}
                for ai, (t, v) in b.get('attrs', {}).items():
                    if t == 'matrix': v = {'pos': list(v.get('pos', []))}
                    beh['attrs'][str(ai)] = {'type': t, 'value': v}
                entry['behaviors'].append(beh)
            out.append(entry)
        open(path, 'w').write(json.dumps(out, indent=2, default=str))
        self.statusBar().showMessage(f"Exported {len(entities)} entities to {path}")

    def _open_elf(self):
        p,_=QFileDialog.getOpenFileName(self,"Open ELF/DOL/Ghidra Export","",
            "All Supported (*.elf *.dol *.map *.c *.xml);;Executables (*.elf *.dol);;Symbol Map (*.map);;Ghidra C (*.c);;Ghidra XML (*.xml);;All (*)")
        if p: self._load_elf(p)

    def _load_elf(self, path):
        self.statusBar().showMessage(f"Loading: {os.path.basename(path)}..."); QApplication.processEvents()
        ext = path.rsplit('.', 1)[-1].lower() if '.' in path else ''
        bn = os.path.basename(path).lower()
        build = 'proto' if 'proto' in bn else 'final' if 'final' in bn else None
        try:
            if ext == 'c':
                # Ghidra decompiled C file
                self.statusBar().showMessage(f"Parsing Ghidra decompilation ({os.path.getsize(path)//1024:,}KB)..."); QApplication.processEvents()
                text = open(path, 'r', errors='replace').read()
                self.elfv.load_ghidra_c(text, os.path.basename(path))
                self.elfv._build = build
                # Auto-load companion .xml if present
                xml_path = path.rsplit('.', 1)[0] + '.xml'
                if not os.path.isfile(xml_path):
                    d = os.path.dirname(path)
                    for candidate in os.listdir(d) if os.path.isdir(d) else []:
                        if candidate.endswith('.xml') and ('proto' in candidate.lower()) == ('proto' in bn):
                            xml_path = os.path.join(d, candidate); break
                if os.path.isfile(xml_path):
                    self.statusBar().showMessage(f"Loading companion XML..."); QApplication.processEvents()
                    self.elfv.load_ghidra_xml(xml_path, os.path.basename(xml_path))
                n = len(self.elfv._ghidra_funcs)
                self.statusBar().showMessage(f"Loaded {n:,} decompiled functions from {os.path.basename(path)}")
            elif ext == 'xml':
                # Ghidra XML export
                self.statusBar().showMessage(f"Parsing Ghidra XML..."); QApplication.processEvents()
                self.elfv.load_ghidra_xml(path, os.path.basename(path))
                self.elfv._build = build
                # Auto-load companion .c if present
                c_path = path.rsplit('.', 1)[0] + '.c'
                if not os.path.isfile(c_path):
                    d = os.path.dirname(path)
                    for candidate in os.listdir(d) if os.path.isdir(d) else []:
                        if candidate.endswith('.c') and ('proto' in candidate.lower()) == ('proto' in bn):
                            c_path = os.path.join(d, candidate); break
                if os.path.isfile(c_path):
                    self.statusBar().showMessage(f"Loading companion .c decompilation..."); QApplication.processEvents()
                    text = open(c_path, 'r', errors='replace').read()
                    self.elfv.load_ghidra_c(text, os.path.basename(c_path))
                n = len(self.elfv._symbols)
                self.statusBar().showMessage(f"Loaded {n:,} symbols from XML + decompilation")
            elif ext == 'map':
                text = open(path, 'r', errors='replace').read()
                syms = asura.parse_symbol_map(text)
                info = {'type': 'map', 'entry': 0, 'sections': [], 'symbols': syms,
                        'has_dwarf': False, 'text_size': 0, 'data_size': 0, 'bss_size': 0}
                self.elfv.load_elf(info, syms, os.path.basename(path), build)
                if build == 'proto': self._elf_data['proto_syms'] = syms
                elif build == 'final': self._elf_data['final_syms'] = syms
                else: self._elf_data['last_syms'] = syms
            elif ext == 'dol':
                data = open(path, 'rb').read()
                info = asura.parse_dol(data)
                if not info: QMessageBox.critical(self, "Error", "Not a valid DOL file"); return
                map_path = path.rsplit('.', 1)[0] + '.map'
                syms = []
                if not os.path.isfile(map_path):
                    d = os.path.dirname(path)
                    for candidate in os.listdir(d) if os.path.isdir(d) else []:
                        if candidate.endswith('.map') and ('proto' in candidate.lower()) == ('proto' in bn):
                            map_path = os.path.join(d, candidate); break
                if os.path.isfile(map_path):
                    syms = asura.parse_symbol_map(open(map_path, 'r', errors='replace').read())
                self.elfv.load_elf(info, syms if syms else None, os.path.basename(path), build)
                if build == 'proto': self._elf_data['proto_syms'] = syms
                elif build == 'final': self._elf_data['final_syms'] = syms
            else:  # .elf or unknown
                data = open(path, 'rb').read()
                info = asura.parse_elf(data)
                if not info: QMessageBox.critical(self, "Error", "Not a valid ELF file"); return
                syms = info['symbols']
                map_path = path.rsplit('.', 1)[0] + '.map'
                if not os.path.isfile(map_path):
                    d = os.path.dirname(path)
                    for candidate in os.listdir(d) if os.path.isdir(d) else []:
                        if candidate.endswith('.map') and ('proto' in candidate.lower()) == ('proto' in bn):
                            map_path = os.path.join(d, candidate); break
                if os.path.isfile(map_path):
                    map_syms = asura.parse_symbol_map(open(map_path, 'r', errors='replace').read())
                    if len(map_syms) > len(syms): syms = map_syms
                self.elfv.load_elf(info, syms if len(syms) > len(info['symbols']) else None,
                                   os.path.basename(path), build)
                if build == 'proto': self._elf_data['proto_syms'] = syms
                elif build == 'final': self._elf_data['final_syms'] = syms
        except Exception as e:
            import traceback; traceback.print_exc()
            QMessageBox.critical(self, "Error", f"Failed to load: {e}"); return
        self.vs.setCurrentWidget(self.elfv)
        self.setWindowTitle(f"{APP_NAME} v{VERSION} — {os.path.basename(path)}")
        n = len(self.elfv._symbols)
        self.statusBar().showMessage(f"{os.path.basename(path)}: {n:,} symbols loaded")

    def _show_gecko_dialog(self):
        dlg = GeckoCodeDialog(self)
        dlg.set_symbols(self._elf_data.get('proto_syms', []), self._elf_data.get('final_syms', []))
        dlg.exec()

    def _patch_dol(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select DOL to Patch", "", "DOL (*.dol);;All (*)")
        if not path: return
        bn = os.path.basename(path).lower()
        build = 'proto' if 'proto' in bn else 'final'
        patches = asura.get_dol_patches(build)
        if not patches:
            QMessageBox.warning(self, "No Patches", f"No patches available for '{build}' build."); return
        dol_data = open(path, 'rb').read()
        # Build patch selection dialog
        dlg = QDialog(self); dlg.setWindowTitle(f"Patch DOL — {build.title()} Build"); dlg.resize(550, 350)
        lo = QVBoxLayout(dlg)
        lo.addWidget(QLabel(f"<b>DOL Patcher</b> — {os.path.basename(path)} ({build.title()})"))
        lo.addWidget(QLabel(f"File size: {len(dol_data):,} bytes"))
        checks = {}
        for name, p in patches.items():
            can, msg = asura.verify_dol_patch(dol_data, p)
            safe_tag = "" if p.get('safe', True) else " ⚠️ UNSAFE"
            cb = QCheckBox(f"{p['desc']}{safe_tag} — {msg}")
            cb.setEnabled(can)
            cb.setChecked(can and p.get('safe', True))
            if not p.get('safe', True) and p.get('warning'):
                cb.setToolTip(p['warning'])
            lo.addWidget(cb)
            checks[name] = cb
        # Warning for unsafe patches
        warn = QLabel("⚠️ Unsafe patches may corrupt game data. Only enable if you understand the risks.")
        warn.setStyleSheet("color: #e08030; font-size: 11px;"); lo.addWidget(warn)
        lo.addStretch()
        # Buttons
        brow = QHBoxLayout()
        apply_btn = QPushButton("Apply Patches"); brow.addWidget(apply_btn)
        cancel_btn = QPushButton("Cancel"); brow.addWidget(cancel_btn)
        brow.addStretch()
        lo.addLayout(brow)
        result_label = QLabel(""); lo.addWidget(result_label)
        def do_apply():
            selected = [n for n, cb in checks.items() if cb.isChecked()]
            if not selected:
                result_label.setText("No patches selected."); return
            out_path, _ = QFileDialog.getSaveFileName(dlg, "Save Patched DOL", path, "DOL (*.dol)")
            if not out_path: return
            results = asura.patch_dol_file(path, out_path, build, selected, backup=(path==out_path))
            msgs = []
            for name, ok, msg in results:
                msgs.append(f"{'✓' if ok else '✗'} {msg}")
            result_label.setText('\n'.join(msgs))
            QMessageBox.information(dlg, "Done", '\n'.join(msgs))
        apply_btn.clicked.connect(do_apply)
        cancel_btn.clicked.connect(dlg.close)
        dlg.exec()

    def _export_symbols_csv(self):
        if not self.elfv._symbols:
            QMessageBox.information(self, "No Symbols", "Load an ELF/DOL/MAP file first."); return
        path, _ = QFileDialog.getSaveFileName(self, "Export Symbols", "", "CSV (*.csv)")
        if path:
            self.elfv.export_csv(path)
            self.statusBar().showMessage(f"Exported {len(self.elfv._symbols):,} symbols to {os.path.basename(path)}")

    def _load(self, path):
        self.statusBar().showMessage(f"Loading: {os.path.basename(path)}..."); QApplication.processEvents()
        try:
            # Detect format before decompression
            with open(path, 'rb') as f: self._raw_magic = f.read(8)
            self._data=asura.read_asura(path); self._chunks=asura.parse_chunks(self._data)
            self._files=asura.extract_fcsr_files(self._chunks); self._path=path
        except Exception as e: QMessageBox.critical(self,"Error",str(e)); return
        self._amap=asura._parse_alphamaps(self._files)
        # Expand Bink Audio bank (final .enBE) into individual clip entries
        self._bik_clips = []
        for c in self._chunks:
            if c['id'] == 'FCSR' and b'streamed sounds' in c['content'][:64]:
                try:
                    clips = asura.parse_bink_bank(c['content'])
                    nlld = asura.parse_nlld_chunks(self._chunks)
                    for clip in clips:
                        i = clip['index']
                        nl = nlld[i] if i < len(nlld) else None
                        sid = nl['sound_id'] if nl else f'clip_{i:03d}'
                        dur = nl['duration'] if nl else 0
                        txt = nl['text'] if nl else ''
                        safe_sid = sid.replace('/', '_').replace('\\', '_')
                        fname = f"Sounds\\streamed\\{i:03d}_{safe_sid}.bik"
                        entry = {'name': fname, 'data': clip['bik_data'],
                                 'bik_info': {'sr': clip['sample_rate'], 'ch': clip['channels'],
                                              'frames': clip['frames'], 'hash': clip['hash'],
                                              'sound_id': sid, 'duration': dur, 'subtitle': txt}}
                        self._files.append(entry)
                        self._bik_clips.append(entry)
                    # Remove the raw "streamed sounds" entry from the file list
                    self._files = [f for f in self._files if f['name'] != 'streamed sounds']
                except Exception as e:
                    print(f"BIK bank parse error: {e}")
                break
        self._model_tex_map={}  # invalidate texture cache
        self._emap={}
        for c in self._chunks:
            if c['id']=='ITNE' and len(c['content'])>=8:
                self._emap[struct.unpack_from('>I',c['content'],0)[0]]=struct.unpack_from('>H',c['content'],4)[0]
        # Reset editor state
        self._undo_stack = []; self._redo_stack = []; self._dirty = False
        self._undo_act.setEnabled(False); self._redo_act.setEnabled(False)
        self._selected_chunk_idx = -1
        self._pop_tree(); bn=os.path.basename(path)
        self.setWindowTitle(f"{APP_NAME} v{VERSION} — {bn}")
        self.statusBar().showMessage(f"{bn}: {len(self._chunks)} chunks, {len(self._files)} files, {len(self._data):,} bytes")
        self._add_recent(path)

    def _pop_tree(self):
        self.tree.clear()
        fr=QTreeWidgetItem(self.tree,[f"Files ({len(self._files)})","",""]); fr.setExpanded(True); fr.setData(0,Qt.UserRole,None)
        dns={}
        for f in self._files:
            path=f['name'].replace('\\','/').lstrip('/'); parts=path.split('/'); parent=fr
            for i,pt in enumerate(parts[:-1]):
                k='/'.join(parts[:i+1])
                if k not in dns: dns[k]=QTreeWidgetItem(parent,[pt,"",""]); dns[k].setData(0,Qt.UserRole,None)
                parent=dns[k]
            fn=parts[-1]; d=f['data']; ext=fn.rsplit('.',1)[-1].lower() if '.' in fn else ''
            tp=len(d)>=4 and struct.unpack_from('>I',d,0)[0]==asura.TPL_MAGIC
            ic="🖼" if tp and ext in ('tga','bmp','tpl') else "🔊" if d[:4]==b'DSP\x01' else "🔊" if d[:4]==b'BIKi' else "🔷" if f['name'].startswith('Stripped') and f['name']!='StrippedEnv' else "🌍" if f['name']=='StrippedEnv' else "📄"
            it=QTreeWidgetItem(parent,[f"{ic} {fn}",f"{len(d):,}",""]); it.setData(0,Qt.UserRole,('file',f))
        # Add level overview if StrippedEnv exists
        has_env = any(f['name']=='StrippedEnv' for f in self._files)
        if has_env:
            map_item = QTreeWidgetItem(fr, ["🌍 Level Map (top-down)", "", ""]); map_item.setData(0, Qt.UserRole, ('level', None))
            view3d_item = QTreeWidgetItem(fr, ["🎮 Level 3D View", "", ""]); view3d_item.setData(0, Qt.UserRole, ('level3d', None))
            tbl_item = QTreeWidgetItem(fr, ["📋 Level Entity Table", "", ""]); tbl_item.setData(0, Qt.UserRole, ('level_table', None))
            # Move to top (insert in reverse order)
            for _ in range(3):
                fr.insertChild(0, fr.takeChild(fr.childCount()-1))
        # Add animation viewer node if NACH chunks exist
        has_nach = any(c['id'] == 'NACH' for c in self._chunks)
        if has_nach:
            n_nach = sum(1 for c in self._chunks if c['id'] == 'NACH')
            anim_item = QTreeWidgetItem(fr, ["🎬 Animation Viewer ({})".format(n_nach), "", ""])
            anim_item.setData(0, Qt.UserRole, ('anim_view', None))
            fr.insertChild(3 if has_env else 0, fr.takeChild(fr.childCount()-1))
        bt=defaultdict(list)
        for c in self._chunks: bt[c['id']].append(c)
        nlld=asura.parse_nlld_chunks(self._chunks)
        # Build NLLD chunk index mapping (entry index → chunk index in self._chunks)
        self._nlld_chunk_indices = []
        for ci, c in enumerate(self._chunks):
            if c['id'] == 'NLLD': self._nlld_chunk_indices.append(ci)
        # Parse TXTH text entries if present, store chunk index for editing
        self._txth_entries = []
        self._txth_chunk_idx = -1
        for ci, c in enumerate(self._chunks):
            if c['id'] == 'TXTH':
                self._txth_entries = asura.parse_txth_chunk(c['content'])
                self._txth_chunk_idx = ci
                break
        _CHUNK_GROUP_NAMES = {
            'ITNE':'Entities','FCSR':'Files','NACH':'Animation Channels','MSDS':'Sound Events',
            'TPXF':'Particle FX Sources','TSXF':'Texture FX Sources','TEXF':'Emitter FX',
            'VEDS':'Debris/VFX','VELD':'Voice Events','TXET':'Texture Names','LFXT':'Texture Flags',
            'LRTM':'Materials','NKSH':'Skeletons/Nodes','BBSH':'Bounding Shapes','DNSH':'Hit/Dodge Shapes',
            'VETC':'Entity Controllers','RTTC':'Trigger Controllers','CATC':'Camera Triggers',
            'TATC':'Bone Attachments','STUC':'Cutscenes','AMDS':'Code Sounds','TPMH':'Morph Targets',
            'XETA':'Extra Animation','LBTA':'Blend Tables','GSMS':'Level Scripts','PMIU':'GUI Menus',
            'LFSR':'Resource Refs','NAIU':'UI Animations','NAXT':'UV Animations',
            'DOME':'Level Sections','NEHP':'Physics Collision','1VAN':'Navigation Mesh',
            'BABL':'AABB Spatial Trees','NILM':'Lightmaps','BYKS':'Skybox',' GOF':'Fog',
            'RHTW':'Weather','NSBS':'Streaming Audio','NSIG':'AI Signals','OFNF':'File Info',
            'DNER':'Render Env','BVRM':'Render Volumes','PAHS':'Physics Shapes','DPHS':'Shape Descriptors',
            'HPDS':'Shape Data','TRTA':'Triggers/Anim','ANRC':'Crowd Config','gulp':'Stream Markers',
            'SUMM':'Level Summary','NLLD':'Dialogue','TXTH':'Localized Text',
        }
        cr=QTreeWidgetItem(self.tree,[f"Chunks ({len(self._chunks)})","",""]); cr.setData(0,Qt.UserRole,None)
        for cid in sorted(bt):
            cl=bt[cid]; ts=sum(len(c['content']) for c in cl)
            gname = _CHUNK_GROUP_NAMES.get(cid, cid)
            tn=QTreeWidgetItem(cr,[f"{cid} — {gname} ({len(cl)})",f"{ts:,}",""]); tn.setData(0,Qt.UserRole,None)
            if cid=='NLLD' and nlld:
                a=QTreeWidgetItem(tn,["All Dialogue",str(len(nlld)),""]); a.setData(0,Qt.UserRole,('dlg',nlld))
            if cid=='TXTH' and self._txth_entries:
                a=QTreeWidgetItem(tn,[f"All Text ({len(self._txth_entries)})",str(len(self._txth_entries)),""]); a.setData(0,Qt.UserRole,('txth',self._txth_entries))
            if cid=='NACH' and len(cl) > 0:
                a=QTreeWidgetItem(tn,[f"Animation Overview ({len(cl)} channels)","",""]); a.setData(0,Qt.UserRole,('anims',None))
            for i,c in enumerate(cl):
                lb=f"#{i}"
                d2 = c['content']
                if cid in ('VELD','MSDS','BBSH','AMDS','NSBS','VEDS','RTTC','CATC','VETC','PMIU','LFSR','NLLD'):
                    n=d2.find(b'\x00')
                    if 0<n<60: lb=d2[:n].decode('ascii',errors='replace')[:40]
                elif cid == 'STUC' and len(d2) >= 24:
                    cs = asura.parse_stuc_chunk(d2, c['ver'])
                    if cs and cs['name']:
                        dur_s = f" ({cs['duration']:.1f}s)" if cs['duration'] > 0 else ""
                        n_act = len(cs['actors'])
                        lb = f"{cs['name']}{dur_s}" + (f" [{n_act} actors]" if n_act else " [camera]")
                elif cid in ('TPXF','TSXF','TEXF') and len(d2) > 4:
                    n=d2[4:].find(b'\x00')
                    if n>0: lb=d2[4:4+n].decode('ascii',errors='replace')[:40]
                elif cid in ('TATC','DNSH') and len(d2)>4:
                    n=d2[4:].find(b'\x00')
                    if n>0: lb=d2[4:4+n].decode('ascii',errors='replace')[:40]
                elif cid=='NKSH' and len(d2)>12:
                    n=d2[8:].find(b'\x00')
                    if n>0: lb=d2[8:8+n].decode('ascii',errors='replace')[:40]
                elif cid=='NACH':
                    aset=(c['unk']>>5)&0x7FF; ct2=c['unk']&0x1F
                    types={0:'rot',1:'pos',3:'scale',17:'vis'}
                    lb=f"set{aset}_{types.get(ct2,f't{ct2}')}"
                    if len(d2) > 28:
                        nn=d2[28:].find(b'\x00')
                        if nn>0: lb=d2[28:28+nn].decode('ascii',errors='replace')[:25]+f" [{types.get(ct2,f't{ct2}')}]"
                elif cid=='GSMS' and len(d2)>4:
                    ng=struct.unpack_from('>I',d2,0)[0]
                    if 0<ng<500:
                        cts=[d2[4+j] for j in range(min(ng,len(d2)-4))]
                        lb=f"Script: {sum(cts)} msg / {ng} events"
                    else: lb=f"Script #{i}"
                elif cid=='ITNE' and len(d2)>=6:
                    etype=struct.unpack_from('>H',d2,4)[0]
                    _EN={0x0001:'TimeTrig',0x0003:'CutsceneCtrl',0x0007:'PhysObj',
                         0x0009:'DestrLight',0x000B:'Splitter',0x000D:'CntTrig',
                         0x000E:'SoundCtrl',0x0011:'AdvLight',0x0014:'AdvVolTrig',
                         0x0015:'Lift',0x0016:'DmgVol',0x0018:'MusicTrig',
                         0x001C:'FMVTrig',0x001F:'MetaMusic',0x0021:'PFX',
                         0x0022:'Template',0x0023:'LookAtTrig',0x0024:'ClockTrig',
                         0x0026:'LogicTrig',0x0028:'ClientVol',0x0029:'StartPt',
                         0x002A:'Timeline',0x002B:'EnvTexAnim',0x002F:'SpawnPt',
                         0x0033:'CamVol',0x0035:'Node',0x0036:'OrientNode',
                         0x0037:'GameScene',0x0038:'CoverPt',0x0039:'GuardZone',
                         0x003A:'Attractor',0x003B:'Spline',0x003E:'DlgTrig',
                         0x003F:'LiftNode',0x0044:'Teleport',0x004A:'ConsoleVar',
                         0x8001:'Actor',0x8003:'NPC',0x8004:'Usable',
                         0x8005:'Pickup',0x8006:'DestrObj',0x8007:'StartPt',
                         0x800C:'Player',0x800D:'Player',0x800E:'Shover',
                         0x8010:'Updraft',0x8011:'Bunny',0x8012:'Trampoline',
                         0x8013:'Interactive',0x8014:'HoB',0x8015:'Respawn',
                         0x8016:'DeathVol',0x8017:'NPCSpawn',0x8018:'HoBPort',
                         0x801A:'IntTrig',0x801B:'SeeSaw',0x801D:'NavWP',
                         0x801E:'GuardZone',0x801F:'DmgObj',0x8020:'StubApe',
                         0x8021:'LardLad',0x8022:'Objective',0x8023:'Selmatty',
                         0x8024:'TransTrig',0x8025:'Groening',0x8026:'Shakes',
                         0x8027:'BartRing',0x8028:'LLFlap',0x8029:'StateTrig',
                         0x802A:'Parachute'}
                    eid=struct.unpack_from('>I',d2,0)[0]
                    lb=f"{_EN.get(etype,f'0x{etype:04x}')} #{eid:08X}"
                    if len(d2)>=84:
                        px=struct.unpack_from('>f',d2,72)[0]; pz=struct.unpack_from('>f',d2,80)[0]
                        lb+=f" ({px:.0f},{pz:.0f})"
                elif cid=='TXET' and len(d2)>=4:
                    ns=struct.unpack_from('>I',d2,0)[0]
                    if ns>0:
                        nn=d2[4:].find(b'\x00')
                        if nn>0:
                            path=d2[4:4+nn].decode('ascii',errors='replace')
                            lb=f"{path.split(chr(92))[-1][:30]} (+{ns-1})" if ns>1 else path.split(chr(92))[-1][:35]
                        else: lb=f"{ns} textures"
                    else: lb="(empty)"
                elif cid=='LRTM' and len(d2)>=4:
                    nm=struct.unpack_from('>I',d2,0)[0]
                    stride=(len(d2)-4)//nm if nm>0 else 0
                    lb=f"{nm} materials ({stride}B each)"
                elif cid=='DOME' and len(d2)>=4:
                    nm=struct.unpack_from('>I',d2,0)[0]
                    lb=f"{nm} sections"
                elif cid=='NEHP' and len(d2)>=8:
                    ns2=struct.unpack_from('>I',d2,0)[0]; nv2=struct.unpack_from('>I',d2,4)[0]
                    lb=f"{ns2} sec, {nv2:,} verts"
                elif cid=='1VAN' and len(d2)>=8:
                    nv2=struct.unpack_from('>I',d2,0)[0]; np2=struct.unpack_from('>I',d2,4)[0]
                    lb=f"{nv2} verts, {np2} polys"
                elif cid=='BYKS':
                    strs=_find_strings(d2,5,len(d2))
                    paths=[s for _,s in strs if '\\' in s]
                    lb=f"{len(paths)} faces" if paths else "Skybox"
                elif cid==' GOF':
                    if len(d2)>=12:
                        r2,g2,b2=[struct.unpack_from('>f',d2,j*4)[0] for j in range(3)]
                        lb=f"RGB({r2:.2f},{g2:.2f},{b2:.2f})"
                elif cid=='RHTW':
                    lb="Weather settings"
                elif cid=='FCSR' and len(d2)>=16:
                    nn=d2[12:].find(b'\x00')
                    if nn>0: lb=d2[12:12+nn].decode('ascii',errors='replace')[:40]
                elif cid=='LFXT' and len(d2)>=4:
                    nm=struct.unpack_from('>I',d2,0)[0]
                    lb=f"{nm} entries"
                elif cid=='NAXT' and len(d2)>4:
                    strs=_find_strings(d2,3,len(d2))
                    if strs: lb=strs[0][1][:35]
                elif cid=='TPMH':
                    strs=_find_strings(d2,3,len(d2))
                    if strs: lb=strs[0][1][:35]
                elif cid=='SUMM':
                    strs=_find_strings(d2,3,len(d2))
                    if strs: lb=strs[0][1][:35]
                elif cid=='NAIU':
                    strs=_find_strings(d2,3,len(d2))
                    if strs: lb=strs[0][1][:35]
                it=QTreeWidgetItem(tn,[lb,f"{len(d2):,}",f"v{c['ver']}"])
                it.setData(0,Qt.UserRole,('gsms',c) if cid=='GSMS' else ('chunk',c))

    def _filter_tree(self):
        f=self.filt.text().lower()
        for i in range(self.tree.topLevelItemCount()):
            self._fi(self.tree.topLevelItem(i),f) if f else self._sa(self.tree.topLevelItem(i))
    def _sa(self,it): it.setHidden(False); [self._sa(it.child(i)) for i in range(it.childCount())]
    def _fi(self,it,f):
        m=f in it.text(0).lower(); cm=any(self._fi(it.child(i),f) for i in range(it.childCount()))
        it.setHidden(not(m or cm));
        if cm: it.setExpanded(True)
        return m or cm

    def _click(self, item, col):
        d=item.data(0,Qt.UserRole)
        if not d: return
        k=d[0]
        if k=='chunk':
            self.chunkv.show_chunk(d[1], all_chunks=self._chunks); self.vs.setCurrentIndex(1)
            # In edit mode, select ITNE entities for property editing
            if self._edit_mode and d[1]['id'] == 'ITNE':
                for ci, c in enumerate(self._chunks):
                    if c is d[1]: self._select_entity(ci); break
        elif k=='file': self._show(d[1])
        elif k=='dlg':
            self.dlgv.load_dialogue(d[1], chunks=self._chunks, chunk_indices=self._nlld_chunk_indices)
            self.vs.setCurrentIndex(4)
        elif k=='txth':
            self.dlgv.load_text(d[1], chunks=self._chunks, chunk_index=self._txth_chunk_idx)
            self.vs.setCurrentIndex(4)
        elif k=='gsms':
            chunk_idx = -1
            for ci, c in enumerate(self._chunks):
                if c is d[1]: chunk_idx = ci; break
            self.scrv.load_gsms(d[1], self._emap, chunks=self._chunks, chunk_idx=chunk_idx)
            self.vs.setCurrentIndex(6)
        elif k=='level': self._show_level()
        elif k=='level3d': self._show_level_3d()
        elif k=='anim_view': self.animv.load_data(self._chunks); self.vs.setCurrentIndex(7)
        elif k=='level_table':
            try:
                level = parse_level_data(self._chunks, self._files)
                self.lvlv.load_level(level); self.vs.setCurrentIndex(8)
            except Exception as e: self.statusBar().showMessage(f"Level error: {e}")
        # ---- New-Gen item routing ----
        elif k=='ng_resource' and HAS_NEWGEN:
            self._ng_show_resource(d[1], d[2])
        elif k=='ng_entities' and HAS_NEWGEN:
            self._ng_show_entities(d[1])
        elif k=='ng_str' and HAS_NEWGEN:
            entry = self._ng_resmgr.str_files.get(d[1])
            if entry:
                n_a = len(entry['assets']); n_e = sum(sg.get('n_entities',0) for sg in entry['simgroups'])
                from collections import Counter
                types = Counter(a.get('resource_type','?') for a in entry['assets'])
                info = f"STR Archive: {entry['rel_path']}\n{n_a} resources, {n_e} entities\n\n"
                info += "Resource types:\n"
                for t, c in types.most_common(): info += f"  {t:25s}: {c}\n"
                self.chunkv._hex.setPlainText(info)
                self.chunkv._title.setText(f"📁  {entry['rel_path']}"); self.vs.setCurrentWidget(self.chunkv)

    def _ctx(self, pos):
        it=self.tree.itemAt(pos); 
        if not it: return
        d=it.data(0,Qt.UserRole)
        if not d: return
        menu=QMenu(self)
        if d[0]=='file':
            f=d[1]; n=f['name']; da=f['data']; ext=n.rsplit('.',1)[-1].lower() if '.' in n else ''
            tp=len(da)>=4 and struct.unpack_from('>I',da,0)[0]==asura.TPL_MAGIC
            if tp and ext in ('tga','bmp','tpl'): menu.addAction("Export as PNG...").triggered.connect(lambda _=False, _f=f: self._ex_png(_f))
            if da[:4]==b'DSP\x01': menu.addAction("Export as WAV...").triggered.connect(lambda _=False, _f=f: self._ex_wav(_f))
            if da[:4]==b'BIKi': menu.addAction("Export as WAV...").triggered.connect(lambda _=False, _f=f: self._ex_bik_wav(_f)); menu.addAction("Export as BIK...").triggered.connect(lambda _=False, _f=f: self._ex_raw(_f))
            if n.startswith('Stripped') and n!='StrippedEnv': menu.addAction("Export as OBJ...").triggered.connect(lambda _=False, _f=f: self._ex_obj(_f))
            if n.startswith('Stripped') and n!='StrippedEnv':
                parts = self._find_model_parts(n)
                if parts:
                    menu.addAction(f"View Without Parts").triggered.connect(lambda _=False, _f=f: self._show_model_alone(_f))
                    menu.addAction(f"View Assembled ({len(parts)} parts)").triggered.connect(lambda _=False, _f=f: self._show_model_assembled(_f))
                else:
                    # Check if this is a sub-part that has a body
                    bn = n[8:].lower()
                    body_file, costume_prefix = self._get_costume_body(bn)
                    if body_file:
                        body_label = body_file['name'][8:]
                        menu.addAction(f"View with Body ({body_label})").triggered.connect(lambda _=False, _f=f: self._show_model_assembled(_f))
            menu.addAction("Export Raw...").triggered.connect(lambda _=False, _f=f: self._ex_raw(_f))
            # Edit mode: replacement options
            if self._edit_mode:
                menu.addSeparator()
                if tp:
                    menu.addAction("Replace Texture (PNG→TPL)...").triggered.connect(lambda _=False, _f=f: self._replace_texture(_f))
                if da[:4]==b'DSP\x01':
                    menu.addAction("Replace Audio (WAV→DSP)...").triggered.connect(lambda _=False, _f=f: self._replace_audio(_f))
                menu.addAction("Replace Raw File Data...").triggered.connect(lambda _=False, _f=f: self._replace_raw(_f))
        elif d[0]=='chunk': menu.addAction("Export Raw Chunk...").triggered.connect(lambda _=False, _c=d[1]: self._ex_rchunk(_c))
        # ---- New-Gen context menu ----
        elif d[0]=='ng_resource' and HAS_NEWGEN:
            asset = d[1]
            menu.addAction("Export Raw Data...").triggered.connect(lambda _=False, _a=asset: self._ng_export_raw(_a))
            if asset.get('resource_type') == 'EARS_MESH':
                menu.addAction("Export as OBJ...").triggered.connect(lambda _=False, _a=asset: self._ng_export_obj(_a))
            menu.addAction("Copy Filename").triggered.connect(lambda: QApplication.clipboard().setText(asset.get('filename','')))
        elif d[0]=='ng_entities' and HAS_NEWGEN:
            menu.addAction("Export Entities as JSON...").triggered.connect(lambda _=False, _p=d[1]: self._ng_export_entities(_p))
        if menu.actions(): menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _show(self, f):
        n=f['name']; d=f['data']; ext=n.rsplit('.',1)[-1].lower() if '.' in n else ''
        if ext in ('tga','bmp','tpl') and len(d)>=4 and struct.unpack_from('>I',d,0)[0]==asura.TPL_MAGIC:
            self._show_tex(f); return
        if d[:4]==b'DSP\x01': self.audv.load_dsp(d,n.replace('\\','/').split('/')[-1]); self.vs.setCurrentIndex(3); return
        if d[:4]==b'BIKi':
            # Bink Audio clip — decode to WAV via ffmpeg, then play
            bik_info = f.get('bik_info', {})
            clip_name = bik_info.get('sound_id', n.replace('\\','/').split('/')[-1])
            subtitle = bik_info.get('subtitle', '')
            display_name = f"{clip_name}"
            if subtitle:
                display_name += f"  —  \"{subtitle}\""
            self.statusBar().showMessage(f"Decoding BIK audio: {clip_name}...")
            QApplication.processEvents()
            result = asura._decode_bik_to_wav(d)
            if result:
                self.audv.load_wav_bytes(result[0], display_name)
            else:
                self.audv.load_wav_bytes(None, f"{clip_name} (ffmpeg decode failed)")
            self.vs.setCurrentIndex(3); return
        if n.startswith('Stripped') and n!='StrippedEnv':
            # Look up texture for this model
            tex_img = self._get_model_texture(n)
            self.modv.set_texture(tex_img)
            self.modv.load_model(n,d,f.get('chunk_ver',2))
            parts = self._find_model_parts(n)
            if parts:
                self.statusBar().showMessage(f"Model: {n[8:]}  ·  {len(parts)} parts available (right-click for assembly)")
            self.vs.setCurrentIndex(5); return
        if n == 'StrippedEnv': self._show_level_3d(); return
        fc={'id':'FILE','size':len(d)+16,'ver':0,'unk':0,'content':d,'offset':0}; self.chunkv.show_chunk(fc); self.chunkv.title.setText(f"File: {n}"); self.vs.setCurrentIndex(1)

    def _get_model_texture(self, model_name):
        """Look up the texture for a model by finding its TXET in chunk ordering. Returns PIL Image or None."""
        if not self._model_tex_map and self._chunks:
            self._build_model_tex_map()
        tex_path = self._model_tex_map.get(model_name)
        if not tex_path: return None
        # Find matching FCSR file
        short = tex_path.replace('\\','/').split('/')[-1].lower()
        for f in self._files:
            fn = f['name'].replace('\\','/').split('/')[-1].lower()
            if fn == short:
                try: return self._full_tex_to_pil(f)
                except: return None
        return None

    def _build_model_tex_map(self):
        """Build model_name → texture_path mapping from TXET-before-FCSR chunk ordering."""
        self._model_tex_map = {}
        fcsr_idx = 0
        for ci, c in enumerate(self._chunks):
            if c['id'] == 'FCSR' and fcsr_idx < len(self._files):
                f = self._files[fcsr_idx]; fcsr_idx += 1
                if not f['name'].startswith('Stripped') or f['name'] == 'StrippedEnv': continue
                # Search backward for nearest TXET
                for j in range(ci-1, max(0, ci-10), -1):
                    if self._chunks[j]['id'] == 'TXET':
                        td = self._chunks[j]['content']
                        n_str = struct.unpack_from('>I', td, 0)[0]
                        if n_str > 0:
                            null = td[4:].find(b'\x00')
                            if null > 0:
                                self._model_tex_map[f['name']] = td[4:4+null].decode('ascii', errors='replace')
                        break

    # Costume → body mapping: which body mesh each costume variant uses at runtime.
    # Derived from NACH animation names (e.g. "homer_gummi_homerball_to_gummi" → gummi uses homerball body).
    _COSTUME_BODY_MAP = {
        'homer_gummi':  'homerball',   # Gummi Homer = homerball body + gummi head/arms
        'homer_helium': 'homer',       # Helium Homer = homer body + inflated head/arms
        'homerball':    'homerball',    # HomerBall = ball body + head/arms/shoes
    }

    def _find_model_parts(self, model_name):
        """Find extra parts for multi-part character models (e.g., HomerBall → HomerBall_Head).
        Returns list of dicts with 'name', 'data', 'chunk_ver' keys.
        Excludes parts belonging to costume variants (homer_gummi_*, homer_helium_*)
        since those are assembled separately with their own body model."""
        base = model_name[8:]  # strip 'Stripped' prefix
        base_lower = base.lower()
        # Known costume prefixes to exclude from base body assembly
        costume_prefixes = set(self._COSTUME_BODY_MAP.keys())
        parts = []
        for f in self._files:
            if not f['name'].startswith('Stripped') or f['name'] == model_name: continue
            fn = f['name'][8:]
            fn_lower = fn.lower()
            if fn_lower.startswith(base_lower + '_') and 'eyelid' not in fn_lower:
                # Check if this part belongs to a costume variant
                full_part = base_lower + '_' + fn_lower[len(base_lower)+1:]
                is_costume = False
                for cp in costume_prefixes:
                    if full_part.startswith(cp + '_') and cp != base_lower:
                        is_costume = True; break
                if not is_costume:
                    parts.append({'name': fn, 'data': f['data'], 'chunk_ver': f.get('chunk_ver', 2)})
        return parts

    def _get_costume_body(self, part_name_lower):
        """For a sub-part like 'homer_gummi_head', determine the correct body model.
        Returns (body_file_dict, costume_prefix) or (None, None)."""
        for prefix, body_name in self._COSTUME_BODY_MAP.items():
            if part_name_lower.startswith(prefix + '_') or part_name_lower == prefix:
                body_name_stripped = 'Stripped' + body_name
                for f2 in self._files:
                    if f2['name'].lower() == body_name_stripped.lower():
                        return f2, prefix
        # Fallback: find longest matching base model name
        best = None
        for f2 in self._files:
            fn2 = f2['name'][8:].lower() if f2['name'].startswith('Stripped') else ''
            if fn2 and part_name_lower.startswith(fn2 + '_') and fn2 != part_name_lower:
                if best is None or len(fn2) > len(best[1]):
                    best = (f2, fn2)
        if best:
            return best[0], part_name_lower.rsplit('_', 1)[0]
        return None, None

    def _show_model_alone(self, f):
        """Show model without any attached parts."""
        n = f['name']; d = f['data']
        tex_img = self._get_model_texture(n)
        self.modv.set_texture(tex_img)
        self.modv.load_model(n, d, f.get('chunk_ver', 2), extra_parts=None)
        self.vs.setCurrentIndex(5)

    def _show_model_assembled(self, f):
        """Show model with all attached parts, using correct body for each costume variant.
        homer_gummi → homerball body + gummi parts (from NACH: homer_gummi_homerball_to_gummi)
        homer_helium → homer body + helium parts (from NACH: homer_helium_to_homer)
        HomerBall → homerball body + HomerBall parts"""
        n = f['name']; d = f['data']
        tex_img = self._get_model_texture(n)
        self.modv.set_texture(tex_img)
        parts = self._find_model_parts(n)
        # If this model has parts directly (e.g., homerball → HomerBall_Head/ArmL/R/ShoeL/R)
        if parts:
            self.modv.load_model(n, d, f.get('chunk_ver', 2), extra_parts=parts)
            part_names = ', '.join(p['name'] for p in parts)
            self.statusBar().showMessage(f"Assembled: {n[8:]} + {len(parts)} parts")
            self.vs.setCurrentIndex(5); return
        # This is a sub-part (e.g., homer_gummi_head) — find correct body + siblings
        base_name = n[8:].lower()
        body_file, costume_prefix = self._get_costume_body(base_name)
        if body_file:
            # Gather all sibling parts with same costume prefix
            sibling_parts = []
            for f3 in self._files:
                if not f3['name'].startswith('Stripped') or f3['name'] == body_file['name']: continue
                fn3 = f3['name'][8:].lower()
                if fn3.startswith(costume_prefix + '_') and 'eyelid' not in fn3:
                    sibling_parts.append({'name': f3['name'][8:], 'data': f3['data'], 'chunk_ver': f3.get('chunk_ver', 2)})
            # Use body's texture if available
            tex_img2 = self._get_model_texture(body_file['name'])
            if tex_img2: self.modv.set_texture(tex_img2)
            self.modv.load_model(body_file['name'], body_file['data'], body_file.get('chunk_ver', 2), extra_parts=sibling_parts)
            part_names = ', '.join(p['name'] for p in sibling_parts)
            self.statusBar().showMessage(f"Assembled: {body_file['name'][8:]} + {part_names}")
            self.vs.setCurrentIndex(5); return
        # No parts, no parent — just show alone
        self.modv.load_model(n, d, f.get('chunk_ver', 2), extra_parts=None)
        self.statusBar().showMessage(f"Viewing: {n[8:]}")
        self.vs.setCurrentIndex(5)

    def _show_tex(self, f):
        self.texpanel.load_texture(f, self)
        try: self.texpanel.export_btn.clicked.disconnect()
        except (RuntimeError, TypeError): pass
        self.texpanel.export_btn.clicked.connect(lambda _=False, _f=f: self._ex_png(_f))
        self.vs.setCurrentIndex(2)

    def _full_tex_to_pil(self, f, use_palette=True, use_alpha=True):
        """Full texture decode pipeline: palette + second-image alpha + GC_Alpha + chroma key. Returns PIL Image or None."""
        from PIL import Image; import numpy as np
        td=f['data']; imgs=asura.parse_tpl(td)
        if not imgs: return None
        i0=imgs[0]; dec=asura._DECODERS.get(i0['fmt'])
        if not dec: return None
        px,mode=dec(td[i0['doff']:],i0['w'],i0['h']); img=Image.frombytes(mode,(i0['w'],i0['h']),bytes(px))
        if use_palette and i0['fmt']==1 and img.mode=='L': img=Image.fromarray(asura._get_palette_lut()[np.array(img)],'RGBA')
        if use_alpha:
            if len(imgs)>=2:
                i1=imgs[1]; d1=asura._DECODERS.get(i1['fmt'])
                if d1:
                    px1,m1=d1(td[i1['doff']:],i1['w'],i1['h']); alpha=Image.frombytes(m1,(i1['w'],i1['h']),bytes(px1))
                    if alpha.size!=img.size: alpha=alpha.resize(img.size,Image.NEAREST)
                    if alpha.mode!='L': alpha=alpha.convert('L')
                    if img.mode in ('L','RGB','LA'): img=img.convert('RGBA')
                    r,g,b,ea=img.split()
                    img=Image.merge('RGBA',(r,g,b,Image.fromarray((np.array(ea).astype(np.uint16)*np.array(alpha).astype(np.uint16)//255).astype(np.uint8))))
            rel=f['name'].replace('\\','/').lstrip('/'); tp=rel[9:].lower() if rel.lower().startswith('graphics/') else rel.lower()
            ae=None
            for ak,av in self._amap.items():
                if ak.lower()==tp: ae=av; break
            if ae:
                ad,ci=ae; ai2=asura.parse_tpl(ad)
                if ai2:
                    a0=ai2[0]; ad2=asura._DECODERS.get(a0['fmt'])
                    if ad2:
                        ap,am2=ad2(ad[a0['doff']:],a0['w'],a0['h']); aimg=Image.frombytes(am2,(a0['w'],a0['h']),bytes(ap)).convert('RGB')
                        rc,gc,bc=aimg.split(); ach={0:bc,1:gc,2:rc}.get(ci,bc)
                        if img.mode!='RGBA': img=img.convert('RGBA')
                        if ach.size!=img.size: ach=ach.resize(img.size,Image.NEAREST)
                        r,g,b,ea=img.split()
                        img=Image.merge('RGBA',(r,g,b,Image.fromarray((np.array(ea).astype(np.uint16)*np.array(ach).astype(np.uint16)//255).astype(np.uint8))))
            if i0['fmt']==14:
                if img.mode!='RGBA': img=img.convert('RGBA')
                arr=np.array(img); arr[(arr[:,:,0]>240)&(arr[:,:,1]<16)&(arr[:,:,2]>240),3]=0; img=Image.fromarray(arr,'RGBA')
        if img.mode!='RGBA': img=img.convert('RGBA')
        return img

    def _ex_png(self,f):
        n=f['name'].replace('\\','/').split('/')[-1]; p,_=QFileDialog.getSaveFileName(self,"Export PNG",os.path.splitext(n)[0]+".png","PNG (*.png)")
        if not p: return
        try:
            img=self._full_tex_to_pil(f)
            if img:
                import numpy as np
                arr = np.array(img)
                t = np.sum(arr[:,:,3] < 128) if arr.shape[2] == 4 else 0
                img.save(p)
                self.statusBar().showMessage(f"v{VERSION} Exported: {p} ({img.size[0]}x{img.size[1]} {t} transparent pixels)")
            else: QMessageBox.warning(self,"Error","Failed to decode texture")
        except Exception as e: QMessageBox.warning(self,"Error",str(e))
    def _ex_wav(self,f):
        n=f['name'].replace('\\','/').split('/')[-1]; p,_=QFileDialog.getSaveFileName(self,"Export WAV",os.path.splitext(n)[0]+".wav","WAV (*.wav)")
        if p:
            r=asura._decode_dsp_adpcm(f['data'])
            if r: open(p,'wb').write(r[0]); self.statusBar().showMessage(f"Exported: {p}")
    def _ex_bik_wav(self,f):
        n=f['name'].replace('\\','/').split('/')[-1]; p,_=QFileDialog.getSaveFileName(self,"Export WAV",os.path.splitext(n)[0]+".wav","WAV (*.wav)")
        if p:
            self.statusBar().showMessage(f"Decoding BIK audio...")
            QApplication.processEvents()
            r=asura._decode_bik_to_wav(f['data'])
            if r: open(p,'wb').write(r[0]); self.statusBar().showMessage(f"Exported: {p}")
            else: self.statusBar().showMessage("BIK decode failed (is ffmpeg installed?)")
    def _ex_obj(self,f):
        bn=f['name'][8:] if f['name'].startswith('Stripped') else f['name']; p,_=QFileDialog.getSaveFileName(self,"Export OBJ",bn+".obj","OBJ (*.obj)")
        if p: ok,info=asura.convert_model_to_obj(f['name'],f['data'],p,f.get('chunk_ver',2)); self.statusBar().showMessage(info if ok else f"Error: {info}")
    def _ex_raw(self,f):
        n=f['name'].replace('\\','/').split('/')[-1]; p,_=QFileDialog.getSaveFileName(self,"Export",n,"All (*)")
        if p: open(p,'wb').write(f['data'])
    def _ex_rchunk(self,c):
        p,_=QFileDialog.getSaveFileName(self,"Export",f"{c['id']}.bin","All (*)")
        if p: open(p,'wb').write(c['content'])

    def _show_level(self):
        try:
            level = parse_level_data(self._chunks, self._files)
            self.lvlv.load_level(level)
            self.lvlmap.load_level(level)
            self.vs.setCurrentIndex(9)  # Show the top-down map by default
        except Exception as e:
            self.statusBar().showMessage(f"Level error: {e}")
            import traceback; traceback.print_exc()

    def _show_level_3d(self):
        if not LevelViewport3D or not isinstance(self.lvl3d, LevelViewport3D):
            self.vs.setCurrentIndex(10); return
        self.statusBar().showMessage("Loading 3D level..."); QApplication.processEvents()
        try:
            env_data = None
            for f in self._files:
                if f['name'] == 'StrippedEnv': env_data = f['data']; break
            if not env_data:
                self.statusBar().showMessage("No StrippedEnv found"); return

            env = asura.parse_env_mesh_full(env_data)
            env_mat_info = asura.parse_env_materials(self._chunks)
            mats = env_mat_info['mat_table']
            mat_details = env_mat_info.get('materials', [])
            ents = asura.parse_entity_placements(self._chunks)

            # Build texture lookup
            tex_lookup = {}
            for f in self._files:
                rel = f['name'].replace('\\','/').lstrip('/')
                key = rel[9:].lower() if rel.lower().startswith('graphics/') else rel.lower()
                tex_lookup[key] = f

            def tex_pil_func(mat_path, **kwargs):
                key = mat_path.lower()
                if key in tex_lookup:
                    try: return self._full_tex_to_pil(tex_lookup[key], **kwargs)
                    except: return None
                return None

            # ALL env textures are I8 grayscale — the I8 value indexes into the
            # simpsons_palette LUT to produce the cartoon-colored pixel.
            # This is the same system used by the texture viewer.
            # Skybox textures are CMPR (already colored), handled separately.
            def env_tex_func(mat_path, **kw):
                kw['use_palette'] = True
                return tex_pil_func(mat_path, **kw)

            # Build prop meshes from level data
            level = parse_level_data(self._chunks, self._files)
            prop_meshes = []
            model_cache = {}

            # Build model→texture mapping from TXET chunks (same method as model viewer)
            _model_tex_map = {}
            fcsr_idx = 0
            for ci, c in enumerate(self._chunks):
                if c['id'] == 'FCSR' and fcsr_idx < len(self._files):
                    f2 = self._files[fcsr_idx]; fcsr_idx += 1
                    if not f2['name'].startswith('Stripped') or f2['name'] == 'StrippedEnv': continue
                    for j in range(ci-1, max(0, ci-10), -1):
                        if self._chunks[j]['id'] == 'TXET':
                            td = self._chunks[j]['content']
                            n_str = struct.unpack_from('>I', td, 0)[0]
                            if n_str > 0:
                                null = td[4:].find(b'\x00')
                                if null > 0:
                                    tp = td[4:4+null].decode('ascii', errors='replace')
                                    tp = tp.replace('\\', '/').lstrip('/')
                                    if tp.lower().startswith('graphics/'):
                                        tp = tp[9:]
                                    _model_tex_map[f2['name'][8:]] = tp
                            break

            def _find_prop_tex(model_name):
                return _model_tex_map.get(model_name)

            gqr = 1.0/1024.0
            for e in level['entities']:
                if not e.get('model') or 'pos' not in e: continue
                mname = e['model']
                if mname not in model_cache:
                    mfile = level['model_data'].get(mname)
                    verts = []; uvs = []; tris = []
                    if mfile:
                        d = mfile['data']; cv = mfile.get('chunk_ver', 0)
                        fv = struct.unpack_from('>I', d, 4)[0] if len(d) >= 8 else 0
                        try:
                            if fv in (6, 14) and cv < 3:
                                vc = struct.unpack_from('>I', d, 16)[0]
                                sm = struct.unpack_from('>I', d, 8)[0]
                                vo = (28+(sm-1)*8) if fv==6 else (32+(sm-1)*12)
                                for i in range(min(vc, 8000)):
                                    o = vo+i*16
                                    if o+16 > len(d): break
                                    verts.append((struct.unpack_from('>h',d,o)[0]*gqr,
                                                  -struct.unpack_from('>h',d,o+2)[0]*gqr,
                                                  -struct.unpack_from('>h',d,o+4)[0]*gqr))
                                    uvs.append((struct.unpack_from('>h',d,o+12)[0]/1024.0,
                                                struct.unpack_from('>h',d,o+14)[0]/1024.0))
                                if fv == 6:
                                    ic = struct.unpack_from('>I',d,12)[0]
                                    io2 = vo+vc*16
                                    idx = [struct.unpack_from('>H',d,io2+i*2)[0] for i in range(ic)]
                                    tris = asura._tristrip_to_tris(idx, vc)
                                elif fv == 14:
                                    dl_sz = struct.unpack_from('>I', d, 12)[0]
                                    dl = d[vo+vc*16:vo+vc*16+dl_sz]; doff = 0
                                    while doff < len(dl) - 3:
                                        cmd = dl[doff]
                                        if 0x90 <= cmd <= 0x9f:
                                            cnt = struct.unpack_from('>H', dl, doff+1)[0]
                                            if 3 <= cnt <= 65535:
                                                ve2 = doff+3+cnt*8
                                                if ve2 <= len(dl):
                                                    pis = []; ok2 = True
                                                    for vi in range(cnt):
                                                        pi = struct.unpack_from('>H', dl, doff+3+vi*8)[0]
                                                        if pi >= vc: ok2=False; break
                                                        pis.append(pi)
                                                    if ok2:
                                                        for i in range(len(pis)-2):
                                                            a,b,c2 = pis[i],pis[i+1],pis[i+2]
                                                            if a==b or b==c2 or a==c2: continue
                                                            if i%2==0: tris.append((a,b,c2))
                                                            else: tris.append((a,c2,b))
                                                        doff = ve2; continue
                                            doff += 1
                                        elif cmd == 0: doff += 1
                                        else: doff += 1
                            elif cv >= 3 and fv == 14:
                                nVerts = struct.unpack_from('>I', d, 16)[0]
                                nSub = struct.unpack_from('>I', d, 8)[0]
                                dlSize = struct.unpack_from('>I', d, 12)[0]
                                vo = 32 + (nSub - 1) * 12
                                for i in range(min(nVerts, 8000)):
                                    o = vo + i * 16
                                    if o + 16 > len(d): break
                                    verts.append((struct.unpack_from('>h',d,o)[0]*gqr,
                                                  -struct.unpack_from('>h',d,o+2)[0]*gqr,
                                                  -struct.unpack_from('>h',d,o+4)[0]*gqr))
                                    uvs.append((struct.unpack_from('>h',d,o+12)[0]/1024.0,
                                                struct.unpack_from('>h',d,o+14)[0]/1024.0))
                                dl_off = vo + nVerts * 16
                                dl = d[dl_off:dl_off+dlSize]; doff = 0
                                while doff < len(dl) - 3:
                                    cmd = dl[doff]
                                    if 0x90 <= cmd <= 0x9f:
                                        cnt = struct.unpack_from('>H', dl, doff+1)[0]
                                        if 3 <= cnt <= 65535:
                                            ve2 = doff+3+cnt*8
                                            if ve2 <= len(dl):
                                                pis = []
                                                for vi in range(cnt):
                                                    pi = struct.unpack_from('>H', dl, doff+3+vi*8)[0]
                                                    if pi >= nVerts: break
                                                    pis.append(pi)
                                                else:
                                                    for i in range(len(pis)-2):
                                                        a,b,c2 = pis[i],pis[i+1],pis[i+2]
                                                        if a==b or b==c2 or a==c2: continue
                                                        if i%2==0: tris.append((a,b,c2))
                                                        else: tris.append((a,c2,b))
                                                    doff = ve2; continue
                                        doff += 1
                                    elif cmd == 0: doff += 1
                                    else: doff += 1
                            elif cv >= 3 and fv == 0:
                                off2=8
                                ec2=struct.unpack_from('>I',d,off2)[0]; off2+=4
                                dbs=struct.unpack_from('>I',d,off2)[0]; off2+=4
                                ni=struct.unpack_from('>I',d,off2)[0]+2; off2+=4
                                nv=struct.unpack_from('>I',d,off2)[0]; off2+=4
                                nbi=struct.unpack_from('>I',d,off2)[0]; off2+=4
                                ne=struct.unpack_from('>I',d,off2)[0]; off2+=4
                                cd=struct.unpack_from('>I',d,off2)[0]; off2+=4
                                vs2=10 if ec2==1 else 6
                                vo2=36+cd*nv+ne*8
                                for i in range(min(nv, 8000)):
                                    o=vo2+i*vs2
                                    if o+6 > len(d): break
                                    verts.append((struct.unpack_from('>h',d,o)[0]*gqr,
                                                  -struct.unpack_from('>h',d,o+2)[0]*gqr,
                                                  -struct.unpack_from('>h',d,o+4)[0]*gqr))
                                uv_off = vo2 + nv * vs2
                                for i in range(min(nv, 8000)):
                                    o = uv_off + i * 4
                                    if o + 4 > len(d): break
                                    uvs.append((struct.unpack_from('>h',d,o)[0]/1024.0,
                                                struct.unpack_from('>h',d,o+2)[0]/1024.0))
                                do2=uv_off+nv*4+nbi*2
                                dl=d[do2:do2+dbs]; doff=0
                                while doff<len(dl)-3:
                                    cmd=dl[doff]
                                    if 0x90<=cmd<=0x9f:
                                        cnt=struct.unpack_from('>H',dl,doff+1)[0]
                                        if 3<=cnt<=65535:
                                            vd=doff+3; ve2=vd+cnt*6
                                            if ve2<=len(dl):
                                                pis=[]
                                                for vi in range(cnt):
                                                    pi=struct.unpack_from('>H',dl,vd+vi*6)[0]
                                                    if pi>=nv: break
                                                    pis.append(pi)
                                                else:
                                                    for i in range(len(pis)-2):
                                                        a,b,c2=pis[i],pis[i+1],pis[i+2]
                                                        if a==b or b==c2 or a==c2: continue
                                                        if i%2==0: tris.append((a,b,c2))
                                                        else: tris.append((a,c2,b))
                                                    doff=ve2; continue
                                        doff+=1
                                    elif cmd==0: doff+=1
                                    else: doff+=1
                            else:
                                mesh = asura._parse_smoothskin(d, cv)
                                if mesh:
                                    for x,y,z in mesh['positions']:
                                        verts.append((x*gqr,-y*gqr,-z*gqr))
                                    if mesh.get('uvs'):
                                        uvs = list(mesh['uvs'])
                                    tris = asura._tristrip_to_tris(mesh['indices'], mesh['nVtx'])
                        except: pass
                    model_cache[mname] = (verts, uvs, tris) if verts and tris else ([], [], [])

                verts, uvs, tris = model_cache[mname]
                if verts and tris:
                    tex_path = _find_prop_tex(mname)
                    ec = ETYPE_COLORS.get(e.get('type',0), (0xCC,0x99,0x55))
                    prop_meshes.append({
                        'verts': verts, 'uvs': uvs, 'tris': tris,
                        'pos': (e['pos'][0], e['pos'][1], e['pos'][2]),
                        'quat': e.get('quat'),
                        'color': (ec[0]/255*0.7, ec[1]/255*0.7, ec[2]/255*0.7),
                        'tex_path': tex_path,
                        'name': mname,
                    })

            fog = None
            for c in self._chunks:
                if c['id'] == ' GOF':
                    d = c['content']
                    fog = [struct.unpack_from('>f', d, j*4)[0] for j in range(min(10, len(d)//4))]
                    break

            navmesh = asura.parse_navmesh(self._chunks)
            dome_sections = asura.parse_dome_sections(self._chunks)
            collision = asura.parse_collision_mesh(self._chunks)
            prop_bbs = asura.parse_prop_bounding_boxes(self._chunks)
            splines = asura.parse_splines(self._chunks)
            cliches = asura.parse_cliche_locations(self._chunks)

            # Parse cutscene definitions
            cutscenes = []
            for c in self._chunks:
                if c['id'] == 'STUC':
                    cs = asura.parse_stuc_chunk(c['content'], c['ver'])
                    if cs and cs['name']:
                        cutscenes.append(cs)

            # Find player start position from entities
            start_pos = None
            for e in ents:
                if e.get('type') in (0x8007, 0x0029) and 'pos' in e:
                    p = e['pos']
                    if all(abs(v) < 5000 for v in p):
                        start_pos = p; break

            # Parse BYKS skybox face texture paths
            skybox_faces = None
            for c in self._chunks:
                if c['id'] == 'BYKS':
                    d = c['content']
                    if len(d) < 16: break
                    off = 12  # skip float32×3 ambient color
                    faces = []
                    for fi in range(6):
                        null = d[off:].find(b'\x00')
                        if null < 0: break
                        path = d[off:off+null].decode('ascii', errors='replace') if null > 0 else ''
                        # Normalize path for tex_lookup
                        path = path.replace('\\', '/').lstrip('/')
                        if path.lower().startswith('graphics/'):
                            path = path[9:]
                        faces.append(path)
                        off += null + 1
                        off = (off + 3) & ~3  # 4-byte align
                    if faces:
                        skybox_faces = faces
                    break

            self.lvl3d.load_level(env, mats, env_tex_func, ents, fog, prop_meshes,
                                  palette_lut=asura._get_palette_lut(), mat_details=mat_details,
                                  navmesh=navmesh, dome_sections=dome_sections, start_pos=start_pos,
                                  collision=collision, splines=splines, cliches=cliches, cutscenes=cutscenes,
                                  skybox_faces=skybox_faces, prop_bbs=prop_bbs)
            extra = []
            if navmesh and navmesh.get('vertices'): extra.append("nav:{}wp".format(len(navmesh['vertices'])))
            if dome_sections: extra.append("{}sec".format(len(dome_sections)))
            if collision and collision.get('faces'): extra.append("col:{}f".format(len(collision['faces'])))
            if splines: extra.append("{}spl".format(len(splines)))
            if cliches: extra.append("{}cli".format(len(cliches)))
            if cutscenes: extra.append("{}cuts".format(len(cutscenes)))
            self.vs.setCurrentIndex(10)
            self.statusBar().showMessage(
                "3D level: {:,}v, {:,}t, {} props, {}".format(
                    len(env['positions']), sum(len(s['tris']) for s in env['strips']),
                    len(prop_meshes), ', '.join(extra)))
        except Exception as e:
            self.statusBar().showMessage(f"3D level error: {e}")
            import traceback; traceback.print_exc()

    def _exp_level(self):
        """Export level env mesh as OBJ using existing tsg_oldgen.cmd_env logic."""
        env_data = None
        for f in self._files:
            if f['name'] == 'StrippedEnv': env_data = f['data']; break
        if not env_data:
            QMessageBox.information(self, "Info", "No StrippedEnv in this file"); return
        out = QFileDialog.getExistingDirectory(self, "Export Level To")
        if not out: return
        bn = os.path.splitext(os.path.basename(self._path))[0]
        obj_path = os.path.join(out, bn + '_Env_mesh.obj')
        # Use the existing env export in tsg_oldgen
        import argparse
        args = argparse.Namespace(input=[self._path], output=out)
        try:
            asura.cmd_env(args)
            self.statusBar().showMessage(f"Level exported → {obj_path}")
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Level export failed: {e}")

    def _exp_tex(self):
        if not self._files: return
        out=QFileDialog.getExistingDirectory(self,"Export Textures To")
        if not out: return
        ct=0
        for f in self._files:
            ext=f['name'].rsplit('.',1)[-1].lower() if '.' in f['name'] else ''
            if ext not in ('tga','bmp','tpl') or 'GC_Alpha_Textures' in f['name']: continue
            if len(f['data'])<4 or struct.unpack_from('>I',f['data'],0)[0]!=asura.TPL_MAGIC: continue
            rel=f['name'].replace('\\','/').lstrip('/'); png=os.path.join(out,os.path.splitext(rel)[0]+'.png')
            try:
                img=self._full_tex_to_pil(f)
                if img: os.makedirs(os.path.dirname(png) or '.',exist_ok=True); img.save(png); ct+=1
            except: pass
        self.statusBar().showMessage(f"Exported {ct} textures to {out}")
    def _exp_mod(self):
        if not self._files: return
        out=QFileDialog.getExistingDirectory(self,"Export Models To")
        if not out: return
        ct=0
        for f in self._files:
            if not f['name'].startswith('Stripped') or f['name']=='StrippedEnv': continue
            ok,_=asura.convert_model_to_obj(f['name'],f['data'],os.path.join(out,f['name'][8:]+'.obj'),f.get('chunk_ver',2))
            if ok: ct+=1
        self.statusBar().showMessage(f"Exported {ct} models")
    def _exp_aud(self):
        if not self._files: return
        out=QFileDialog.getExistingDirectory(self,"Export Audio To")
        if not out: return
        ct=0
        for f in self._files:
            if f['data'][:4]==b'DSP\x01':
                rel=f['name'].replace('\\','/').lstrip('/'); wp=os.path.join(out,os.path.splitext(rel)[0]+'.wav')
                r=asura._decode_dsp_adpcm(f['data'])
                if r: os.makedirs(os.path.dirname(wp) or '.',exist_ok=True); open(wp,'wb').write(r[0]); ct+=1
            elif f['data'][:4]==b'BIKi':
                rel=f['name'].replace('\\','/').lstrip('/'); wp=os.path.join(out,os.path.splitext(rel)[0]+'.wav')
                r=asura._decode_bik_to_wav(f['data'])
                if r: os.makedirs(os.path.dirname(wp) or '.',exist_ok=True); open(wp,'wb').write(r[0]); ct+=1
        self.statusBar().showMessage(f"Exported {ct} audio files")

    def _batch_export(self):
        """Export all assets from multiple .wii files."""
        files, _ = QFileDialog.getOpenFileNames(self, "Select .wii Files for Batch Export", "",
            "Asura Files (*.wii *.enBE *.asrBE *.asr *.guiBE);;All (*)")
        if not files: return
        out = QFileDialog.getExistingDirectory(self, "Batch Export Output Directory")
        if not out: return

        from PySide6.QtWidgets import QProgressDialog
        prog = QProgressDialog(f"Exporting {len(files)} files...", "Cancel", 0, len(files), self)
        prog.setWindowTitle("Batch Export"); prog.setMinimumWidth(400); prog.show()

        total_tex = total_mod = total_aud = total_dlg = 0
        for fi, fp in enumerate(files):
            if prog.wasCanceled(): break
            bn = os.path.splitext(os.path.basename(fp))[0]
            prog.setLabelText(f"[{fi+1}/{len(files)}] {bn}...")
            prog.setValue(fi); QApplication.processEvents()

            try:
                data = asura.read_asura(fp)
                chunks = asura.parse_chunks(data)
                flist = asura.extract_fcsr_files(chunks)
                amap = asura._parse_alphamaps(flist)
                file_out = os.path.join(out, bn)
                os.makedirs(file_out, exist_ok=True)

                # Textures
                tex_dir = os.path.join(file_out, 'textures')
                os.makedirs(tex_dir, exist_ok=True)
                # Build temp explorer state for _full_tex_to_pil
                old_amap, old_files = self._amap, self._files
                self._amap, self._files = amap, flist
                for f in flist:
                    ext = f['name'].rsplit('.',1)[-1].lower() if '.' in f['name'] else ''
                    if ext not in ('tga','bmp','tpl') or 'GC_Alpha_Textures' in f['name']: continue
                    if len(f['data']) < 4 or struct.unpack_from('>I', f['data'], 0)[0] != asura.TPL_MAGIC: continue
                    rel = f['name'].replace('\\','/').lstrip('/')
                    png = os.path.join(tex_dir, os.path.splitext(rel)[0].replace('/','_') + '.png')
                    try:
                        img = self._full_tex_to_pil(f)
                        if img: img.save(png); total_tex += 1
                    except: pass
                self._amap, self._files = old_amap, old_files

                # Models
                mod_dir = os.path.join(file_out, 'models')
                os.makedirs(mod_dir, exist_ok=True)
                for f in flist:
                    if not f['name'].startswith('Stripped') or f['name'] == 'StrippedEnv': continue
                    bname = f['name'][8:]
                    ok, _ = asura.convert_model_to_obj(f['name'], f['data'],
                        os.path.join(mod_dir, bname + '.obj'), f.get('chunk_ver', 0))
                    if ok: total_mod += 1

                # Audio
                aud_dir = os.path.join(file_out, 'audio')
                for f in flist:
                    if f['data'][:4] != b'DSP\x01': continue
                    rel = f['name'].replace('\\','/').lstrip('/')
                    wp = os.path.join(aud_dir, os.path.splitext(rel)[0].replace('/','_') + '.wav')
                    os.makedirs(os.path.dirname(wp) or aud_dir, exist_ok=True)
                    r = asura._decode_dsp_adpcm(f['data'])
                    if r: open(wp, 'wb').write(r[0]); total_aud += 1

                # Dialogue
                nlld = asura.parse_nlld_chunks(chunks)
                if nlld:
                    import csv
                    csv_path = os.path.join(file_out, f'{bn}_dialogue.csv')
                    with open(csv_path, 'w', newline='', encoding='utf-8-sig') as cf:
                        w = csv.DictWriter(cf, ['sound_id','duration','subtitle'])
                        w.writeheader()
                        for e in nlld:
                            w.writerow({'sound_id':e['sound_id'],'duration':f"{e['duration']:.2f}",'subtitle':e['text']})
                    total_dlg += len(nlld)

            except Exception as e:
                print(f"Batch error on {bn}: {e}")

        prog.setValue(len(files))
        QMessageBox.information(self, "Batch Export Complete",
            f"Processed {len(files)} files:\n"
            f"  Textures: {total_tex}\n  Models: {total_mod}\n"
            f"  Audio: {total_aud}\n  Dialogue: {total_dlg} lines\n"
            f"\nOutput: {out}")

    # ============================================================
    # Editor Mode (Phase E1 + E2 + E6)
    # ============================================================

    def _toggle_edit_mode(self, enabled):
        self._edit_mode = enabled
        self._prop_dock.setVisible(enabled)
        self.dlgv.set_edit_mode(enabled)
        self.scrv.set_edit_mode(enabled)
        if enabled:
            self.statusBar().showMessage("EDIT MODE — Click entities in tree to edit, Ctrl+S to save")
            self._save_act.setEnabled(bool(self._chunks))
        else:
            self.statusBar().showMessage("View mode")
            self._save_act.setEnabled(False)

    def _on_dlg_edit(self, desc, chunk_idx, old_content, new_content):
        """Handle edit from DialogueView — push to undo stack."""
        self._undo_stack.append((desc, chunk_idx, old_content, new_content))
        self._redo_stack.clear()
        self._undo_act.setEnabled(True)
        self._redo_act.setEnabled(False)
        self._dirty = True
        self._save_act.setEnabled(True)
        self.statusBar().showMessage(f"Applied: {desc}")

    def _push_undo(self, desc, old_content, chunk_idx):
        """Push an edit action onto the undo stack."""
        new_content = self._chunks[chunk_idx]['content']
        self._undo_stack.append((desc, chunk_idx, old_content, new_content))
        self._redo_stack.clear()
        self._undo_act.setEnabled(True)
        self._redo_act.setEnabled(False)
        self._dirty = True
        self._save_act.setEnabled(True)

    def _undo(self):
        if not self._undo_stack: return
        desc, idx, old_content, new_content = self._undo_stack.pop()
        self._redo_stack.append((desc, idx, old_content, new_content))
        self._chunks[idx] = {**self._chunks[idx], 'content': old_content}
        self._undo_act.setEnabled(bool(self._undo_stack))
        self._redo_act.setEnabled(True)
        self._select_entity(idx)
        self.statusBar().showMessage("Undo: {}".format(desc))

    def _redo(self):
        if not self._redo_stack: return
        desc, idx, old_content, new_content = self._redo_stack.pop()
        self._undo_stack.append((desc, idx, old_content, new_content))
        self._chunks[idx] = {**self._chunks[idx], 'content': new_content}
        self._undo_act.setEnabled(True)
        self._redo_act.setEnabled(bool(self._redo_stack))
        self._select_entity(idx)
        self.statusBar().showMessage("Redo: {}".format(desc))

    def _select_entity(self, chunk_idx):
        """Select an entity for editing and populate the property panel."""
        self._selected_chunk_idx = chunk_idx
        if chunk_idx < 0 or chunk_idx >= len(self._chunks):
            self._prop_title.setText("No Selection")
            self._prop_table.setRowCount(0)
            return
        c = self._chunks[chunk_idx]
        d = c['content']
        if c['id'] != 'ITNE' or len(d) < 8:
            self._prop_title.setText("{} chunk [{}]".format(c['id'], chunk_idx))
            self._prop_table.setRowCount(0)
            return

        self._prop_updating = True
        guid = struct.unpack_from('>I', d, 0)[0]
        etype = struct.unpack_from('>H', d, 4)[0]
        etype_name = asura.ENTITY_TYPES.get(etype, 'Unknown_0x{:04X}'.format(etype))
        self._prop_title.setText("{} — 0x{:08X}".format(etype_name, guid))

        rows = [("Type", etype_name), ("GUID", "0x{:08X}".format(guid)),
                ("Chunk Index", str(chunk_idx))]

        if len(d) >= 84:
            px, py, pz = struct.unpack_from('>fff', d, 72)
            rows += [("Position X", "{:.4f}".format(px)),
                     ("Position Y", "{:.4f}".format(py)),
                     ("Position Z", "{:.4f}".format(pz))]
        if len(d) >= 100:
            qx, qy, qz, qw = struct.unpack_from('>ffff', d, 84)
            import math
            qmag = math.sqrt(qx*qx+qy*qy+qz*qz+qw*qw)
            rows += [("Quat X", "{:.6f}".format(qx)),
                     ("Quat Y", "{:.6f}".format(qy)),
                     ("Quat Z", "{:.6f}".format(qz)),
                     ("Quat W", "{:.6f}".format(qw)),
                     ("Quat |q|", "{:.4f}".format(qmag))]
        if len(d) >= 104:
            radius = struct.unpack_from('>f', d, 100)[0]
            rows.append(("Radius", "{:.4f}".format(radius)))

        # Volume bounding box fields
        bb_offset = -1
        if etype == 0x0014 and len(d) >= 84: bb_offset = 60  # AdvVolumeTrigger
        elif etype == 0x0033 and len(d) >= 56: bb_offset = 32  # CameraVolume
        elif etype == 0x8016 and len(d) >= 56: bb_offset = 32  # DeathVolume
        if bb_offset >= 0 and bb_offset + 24 <= len(d):
            bb = struct.unpack_from('>ffffff', d, bb_offset)
            rows += [("BB Min X", "{:.4f}".format(bb[0])),
                     ("BB Max X", "{:.4f}".format(bb[1])),
                     ("BB Min Y", "{:.4f}".format(bb[2])),
                     ("BB Max Y", "{:.4f}".format(bb[3])),
                     ("BB Min Z", "{:.4f}".format(bb[4])),
                     ("BB Max Z", "{:.4f}".format(bb[5]))]

        self._prop_table.setRowCount(len(rows))
        editable_rows = {'Position X', 'Position Y', 'Position Z',
                         'Quat X', 'Quat Y', 'Quat Z', 'Quat W', 'Radius',
                         'BB Min X', 'BB Max X', 'BB Min Y', 'BB Max Y', 'BB Min Z', 'BB Max Z'}
        for i, (label, val) in enumerate(rows):
            li = QTableWidgetItem(label); li.setFlags(li.flags() & ~Qt.ItemIsEditable)
            self._prop_table.setItem(i, 0, li)
            vi = QTableWidgetItem(val)
            if label not in editable_rows:
                vi.setFlags(vi.flags() & ~Qt.ItemIsEditable)
            self._prop_table.setItem(i, 1, vi)
        self._prop_table.resizeColumnsToContents()
        self._prop_updating = False

    def _on_prop_changed(self, row, col):
        """Handle property value edit from table."""
        if self._prop_updating or col != 1: return
        if self._selected_chunk_idx < 0: return
        c = self._chunks[self._selected_chunk_idx]
        if c['id'] != 'ITNE': return
        d = c['content']

        label_item = self._prop_table.item(row, 0)
        val_item = self._prop_table.item(row, 1)
        if not label_item or not val_item: return
        label = label_item.text()
        try: val = float(val_item.text())
        except ValueError: return

        old_content = d
        field_map = {
            'Position X': (72, '>f'), 'Position Y': (76, '>f'), 'Position Z': (80, '>f'),
            'Quat X': (84, '>f'), 'Quat Y': (88, '>f'), 'Quat Z': (92, '>f'), 'Quat W': (96, '>f'),
            'Radius': (100, '>f'),
        }
        # BB fields: type-dependent offset
        etype = struct.unpack_from('>H', d, 4)[0] if len(d) >= 6 else 0
        bb_base = -1
        if etype == 0x0014 and len(d) >= 84: bb_base = 60
        elif etype == 0x0033 and len(d) >= 56: bb_base = 32
        elif etype == 0x8016 and len(d) >= 56: bb_base = 32
        if bb_base >= 0:
            field_map.update({
                'BB Min X': (bb_base, '>f'), 'BB Max X': (bb_base+4, '>f'),
                'BB Min Y': (bb_base+8, '>f'), 'BB Max Y': (bb_base+12, '>f'),
                'BB Min Z': (bb_base+16, '>f'), 'BB Max Z': (bb_base+20, '>f'),
            })

        if label in field_map:
            offset, fmt = field_map[label]
            if offset + 4 <= len(d):
                ba = bytearray(d)
                struct.pack_into(fmt, ba, offset, val)
                self._chunks[self._selected_chunk_idx] = {**c, 'content': bytes(ba)}
                self._push_undo("Edit {} → {:.4f}".format(label, val), old_content, self._selected_chunk_idx)
                self.statusBar().showMessage("Modified: {} = {:.4f}".format(label, val))

    def _delete_selected(self):
        """Delete the selected entity."""
        if self._selected_chunk_idx < 0: return
        c = self._chunks[self._selected_chunk_idx]
        reply = QMessageBox.question(self, "Delete Entity",
            "Delete {} entity 0x{:08X}?".format(c['id'], struct.unpack_from('>I', c['content'], 0)[0] if len(c['content'])>=4 else 0),
            QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes: return
        old_content = c['content']
        self._chunks.pop(self._selected_chunk_idx)
        self._undo_stack.append(("Delete entity", self._selected_chunk_idx, old_content, None))
        self._dirty = True
        self._selected_chunk_idx = -1
        self._select_entity(-1)
        self.statusBar().showMessage("Entity deleted (undo with Ctrl+Z)")

    def _duplicate_selected(self):
        """Duplicate the selected entity with a new GUID and offset position."""
        if self._selected_chunk_idx < 0: return
        c = self._chunks[self._selected_chunk_idx]
        if c['id'] != 'ITNE' or len(c['content']) < 84: return
        new_guid = asura.find_next_guid(self._chunks)
        ba = bytearray(c['content'])
        struct.pack_into('>I', ba, 0, new_guid)
        # Offset position slightly so it's visible
        if len(ba) >= 84:
            px, py, pz = struct.unpack_from('>fff', ba, 72)
            struct.pack_into('>fff', ba, 72, px + 2.0, py, pz + 2.0)
        new_chunk = {'id': 'ITNE', 'ver': c['ver'], 'unk': c['unk'], 'content': bytes(ba)}
        self._chunks.append(new_chunk)
        new_idx = len(self._chunks) - 1
        self._dirty = True
        self._select_entity(new_idx)
        self.statusBar().showMessage("Duplicated → 0x{:08X} (offset +2,+2)".format(new_guid))

    def _import_entity(self):
        """Import entities from another .wii file."""
        path, _ = QFileDialog.getOpenFileName(self, "Import Entities From...", "",
            "Asura Files (*.wii);;All (*.*)")
        if not path: return
        try:
            imp_data = asura.read_asura(path)
            imp_chunks = asura.parse_chunks(imp_data)
            imp_ents = [c for c in imp_chunks if c['id'] == 'ITNE']
            if not imp_ents:
                QMessageBox.information(self, "Import", "No entities found in file.")
                return

            # Build entity description list
            ent_descs = []
            for c in imp_ents:
                d = c['content']
                if len(d) < 8: continue
                guid = struct.unpack_from('>I', d, 0)[0]
                etype = struct.unpack_from('>H', d, 4)[0]
                name = asura.ENTITY_TYPES.get(etype, '0x{:04X}'.format(etype))
                pos = struct.unpack_from('>fff', d, 72) if len(d) >= 84 else (0,0,0)
                ent_descs.append((c, name, guid, pos))

            # Simple selection dialog
            items = ["{} 0x{:08X} ({:.0f},{:.0f},{:.0f})".format(name, guid, *pos)
                     for _, name, guid, pos in ent_descs[:200]]
            from PySide6.QtWidgets import QInputDialog
            chosen, ok = QInputDialog.getItem(self, "Import Entity",
                "Select entity to import ({} available):".format(len(ent_descs)),
                items, 0, False)
            if not ok: return
            idx = items.index(chosen)
            src_chunk = ent_descs[idx][0]

            # Reassign GUID
            new_guid = asura.find_next_guid(self._chunks)
            ba = bytearray(src_chunk['content'])
            struct.pack_into('>I', ba, 0, new_guid)
            new_chunk = {'id': 'ITNE', 'ver': src_chunk['ver'], 'unk': src_chunk['unk'], 'content': bytes(ba)}
            self._chunks.append(new_chunk)
            self._dirty = True
            self._select_entity(len(self._chunks) - 1)
            self.statusBar().showMessage("Imported {} → GUID 0x{:08X}".format(ent_descs[idx][1], new_guid))
        except Exception as e:
            QMessageBox.warning(self, "Import Error", str(e))

    def _replace_texture(self, f):
        """Replace a texture file with a PNG, converting to TPL."""
        path, _ = QFileDialog.getOpenFileName(self, "Replace Texture",
            "", "PNG Images (*.png);;All (*.*)")
        if not path: return
        try:
            tpl_data = asura.png_to_tpl(path, fmt=1)  # I8 format
            self._replace_fcsr_data(f, tpl_data)
            self.statusBar().showMessage("Replaced texture '{}' ({} bytes)".format(f['name'], len(tpl_data)))
        except Exception as e:
            QMessageBox.warning(self, "Replace Error", str(e))

    def _replace_audio(self, f):
        """Replace audio with a raw file (WAV→DSP encoding is complex, accept raw DSP for now)."""
        path, _ = QFileDialog.getOpenFileName(self, "Replace Audio",
            "", "DSP Audio (*.dsp);;WAV Audio (*.wav);;All (*.*)")
        if not path: return
        try:
            with open(path, 'rb') as fp: new_data = fp.read()
            self._replace_fcsr_data(f, new_data)
            self.statusBar().showMessage("Replaced audio '{}' ({} bytes)".format(f['name'], len(new_data)))
        except Exception as e:
            QMessageBox.warning(self, "Replace Error", str(e))

    def _replace_raw(self, f):
        """Replace a file's raw data."""
        path, _ = QFileDialog.getOpenFileName(self, "Replace File Data",
            "", "All Files (*.*)")
        if not path: return
        try:
            with open(path, 'rb') as fp: new_data = fp.read()
            self._replace_fcsr_data(f, new_data)
            self.statusBar().showMessage("Replaced '{}' ({} bytes)".format(f['name'], len(new_data)))
        except Exception as e:
            QMessageBox.warning(self, "Replace Error", str(e))

    def _replace_fcsr_data(self, f, new_data):
        """Find and replace the FCSR chunk for a given file entry."""
        target_name = f['name']
        for ci, c in enumerate(self._chunks):
            if c['id'] != 'FCSR': continue
            d = c['content']
            if len(d) < 12: continue
            null = d[12:].find(b'\x00')
            name = d[12:12+null].decode('ascii', errors='replace') if null > 0 else ''
            if name == target_name:
                old_content = c['content']
                self._chunks[ci] = asura.replace_fcsr_file_data(c, new_data)
                self._push_undo("Replace '{}'".format(target_name), old_content, ci)
                # Update cached files
                self._files = asura.extract_fcsr_files(self._chunks)
                self._dirty = True
                return
        QMessageBox.warning(self, "Not Found", "FCSR chunk for '{}' not found".format(target_name))

    def _save_modified(self):
        """Save modified container to file."""
        if not self._chunks: return
        issues = asura.validate_container(self._chunks)
        if issues:
            reply = QMessageBox.warning(self, "Validation Issues",
                "{} issues found:\n{}".format(len(issues), '\n'.join(issues[:5])),
                QMessageBox.Ok | QMessageBox.Cancel)
            if reply != QMessageBox.Ok: return

        # Detect original format
        is_proto = getattr(self, '_raw_magic', b'')[:8] == b'Asura   '
        default_compressed = not is_proto

        path, _ = QFileDialog.getSaveFileName(self, "Save Modified Level",
            "", "Asura Files (*.wii);;All (*.*)")
        if not path: return

        try:
            repacked = asura.repack_chunks(self._chunks)
            stats = asura.write_container(repacked, path, compressed=default_compressed)
            self._dirty = False
            self.statusBar().showMessage("Saved: {} ({} format, {:,} bytes)".format(
                os.path.basename(path), stats.get('format','?'),
                stats.get('compressed_size', stats.get('size', 0))))
        except Exception as e:
            QMessageBox.critical(self, "Save Error", str(e))

    def dragEnterEvent(self,e):
        if e.mimeData().hasUrls(): e.acceptProposedAction()
    def dropEvent(self,e):
        for u in e.mimeData().urls():
            p=u.toLocalFile()
            if p: self._load(p); break

def main():
    # Clear stale bytecode cache to ensure latest code runs
    import shutil
    cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '__pycache__')
    if os.path.isdir(cache_dir):
        shutil.rmtree(cache_dir, ignore_errors=True)
    
    app=QApplication(sys.argv); app.setStyle("Fusion"); app.setStyleSheet(DARK_STYLE)
    pal=QPalette()
    pal.setColor(QPalette.Window,QColor(0x1a,0x1a,0x1e)); pal.setColor(QPalette.WindowText,QColor(0xcc,0xcc,0xcc))
    pal.setColor(QPalette.Base,QColor(0x16,0x16,0x1a)); pal.setColor(QPalette.AlternateBase,QColor(0x1e,0x1e,0x24))
    pal.setColor(QPalette.Text,QColor(0xcc,0xcc,0xcc)); pal.setColor(QPalette.Highlight,QColor(0xe0,0xa0,0x30))
    pal.setColor(QPalette.HighlightedText,QColor(0x11,0x11,0x11)); app.setPalette(pal)
    w=AsuraExplorer(); w.setAcceptDrops(True); w.show()
    if len(sys.argv)>1 and os.path.isfile(sys.argv[1]): w._load(sys.argv[1])
    sys.exit(app.exec())



if __name__=='__main__': main()
