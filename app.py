#!/usr/bin/env python3
"""
BL-FMO-TEF — FM broadcast monitoring
TEF6686 hardware tuner + HifiBerry DAC+ADC
"""

import os, json, time, threading, subprocess, logging, sqlite3
import numpy as np
from functools import wraps
from datetime import datetime, timedelta
from collections import deque
from flask import Flask, render_template, jsonify, Response, request, redirect, url_for, session
from flask_httpauth import HTTPBasicAuth
from flask_bcrypt import Bcrypt
from flask_wtf.csrf import CSRFProtect
from rds_lookup import get_lookup

# ─── Config ───────────────────────────────────────────────────────────────────

from dotenv import load_dotenv
load_dotenv()

CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.json')
DB_PATH     = os.path.join(os.path.dirname(__file__), 'bl_fmo_tef.db')

def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)

def save_config(cfg):
    with open(CONFIG_PATH, 'w') as f:
        json.dump(cfg, f, indent=2)

# ─── Flask app ────────────────────────────────────────────────────────────────

app    = Flask(__name__)
bcrypt = Bcrypt(app)
csrf   = CSRFProtect(app)

app.secret_key = os.environ.get('SECRET_KEY', 'change-me-in-dotenv')
app.config['WTF_CSRF_TIME_LIMIT'] = None
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# ─── Database ─────────────────────────────────────────────────────────────────

def db_init():
    con = sqlite3.connect(DB_PATH)
    con.execute('''CREATE TABLE IF NOT EXISTS alerts (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        ts        TEXT    NOT NULL,
        kind      TEXT    NOT NULL,
        message   TEXT    NOT NULL
    )''')
    con.execute('''CREATE TABLE IF NOT EXISTS signal_history (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        ts        TEXT    NOT NULL,
        signal    REAL,
        quality   INTEGER
    )''')
    con.commit()
    con.close()

def db_insert_alert(kind, message):
    con = sqlite3.connect(DB_PATH)
    con.execute('INSERT INTO alerts (ts, kind, message) VALUES (?,?,?)',
                (datetime.now().isoformat(), kind, message))
    con.commit()
    con.close()

def db_insert_signal(signal, quality):
    con = sqlite3.connect(DB_PATH)
    con.execute('INSERT INTO signal_history (ts, signal, quality) VALUES (?,?,?)',
                (datetime.now().isoformat(), signal, quality))
    # Purge > 7 jours
    con.execute("DELETE FROM signal_history WHERE ts < ?",
                ((datetime.now() - timedelta(days=7)).isoformat(),))
    con.commit()
    con.close()

# ─── Shared state ─────────────────────────────────────────────────────────────

state = {
    'signal_dbuv':  None,   # float dBµV depuis TEF6686
    'quality':      None,   # int 0-9
    'pi':           None,   # str hex ex "FA41"
    'ps':           None,   # str 8 chars
    'rt':           None,   # str RadioText complet
    'stereo':       None,   # bool
    'signal_ok':    False,
    'rds_ok':       False,
    'uptime_start': time.time(),
    'alerts_sent':  0,
    'stream_ok':    False,
    'station_logo': None,   # URL logo depuis rds-station-db
    # MPX spectrum
    'mpx_spectrum':    [],
    'mpx_sample_rate': 192000,
    'mpx_hz_per_bin':  0.0,
    # MPX analysis
    'mpx_pilot_db':    None,
    'mpx_rds_db':      None,
    'mpx_power_db':    None,
}

state_lock   = threading.Lock()
signal_hist  = deque(maxlen=300)  # 5 min à 1 Hz

# ─── Auth (session) ───────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            if request.path.startswith('/api/'):
                return jsonify({'error': 'unauthorized'}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        if session.get('logged_in'):
            return redirect(url_for('index'))
        return render_template('login.html')
    # POST : vérification des identifiants
    try:
        data = request.get_json(force=True)
        cfg  = load_config()
        user = cfg.get('auth', {}).get('username', 'admin')
        pw   = cfg.get('auth', {}).get('password_hash', '')
        if data.get('username') == user and pw and \
           bcrypt.check_password_hash(pw, data.get('password', '')):
            session['logged_in'] = True
            session['username']  = user
            if data.get('remember'):
                session.permanent = True
            return jsonify({'status': 'success', 'redirect': '/'})
        return jsonify({'status': 'error',
                        'message': "Nom d'utilisateur ou mot de passe incorrect"}), 401
    except Exception as e:
        log.error(f'Login error: {e}')
        return jsonify({'status': 'error', 'message': 'Erreur serveur'}), 500


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ─── Station logo (rds-station-db) ────────────────────────────────────────────

_logo_state = {'searched': False, 'last_attempt': 0, 'current_pi': None}
_logo_lock  = threading.Lock()

def _fetch_station_logo():
    """Cherche le logo via rds-station-db à partir du PI + PS courants."""
    with _logo_lock:
        if time.time() - _logo_state['last_attempt'] < 60:
            return
        _logo_state['last_attempt'] = time.time()
    try:
        # Attendre PI + PS dispo
        for _ in range(10):
            with state_lock:
                pi = (state.get('pi') or '').strip().upper()
                ps = (state.get('ps') or '').strip()
            if pi and pi != '-' and ps:
                break
            time.sleep(0.5)
        with state_lock:
            pi = (state.get('pi') or '').strip().upper()
            ps = (state.get('ps') or '').strip()
        if not pi or pi == '-' or not ps:
            return
        cfg     = load_config()
        country = cfg.get('station', {}).get('country', 'FR')
        lookup  = get_lookup(country)
        station = lookup.get(pi=pi, ps=ps)
        if station and station.get('logo_url'):
            with state_lock:
                state['station_logo'] = station['logo_url']
            log.info(f"Logo trouvé [{pi}/{ps}]: {station['logo_url']}")
        else:
            log.info(f"Aucun logo [{pi}/{ps}]")
        with _logo_lock:
            _logo_state['searched'] = True
    except Exception as e:
        log.warning(f"Erreur logo: {e}")


# ─── TEF6686 serial reader ────────────────────────────────────────────────────

def tef_reader():
    """Lit le port série TEF6686, met à jour state."""
    cfg         = load_config()
    port        = cfg.get('tef', {}).get('port', '/dev/ttyUSB0')
    baud        = cfg.get('tef', {}).get('baudrate', 115200)
    threshold   = cfg.get('monitoring', {}).get('signal_threshold_dbuv', 35.0)
    alert_delay = cfg.get('monitoring', {}).get('alert_delay_seconds', 30)

    ps_segs  = {}
    rt_segs  = {}
    lost_since = None
    alert_sent = False
    last_db_ts = 0

    log.info(f'TEF reader starting on {port} @ {baud}')

    while True:
        try:
            subprocess.run(['stty', '-F', port, str(baud), 'raw', '-echo'],
                           check=True, capture_output=True)
            with open(port, 'r', errors='ignore') as ser:
                log.info('TEF6686 port opened')
                for line in ser:
                    line = line.strip()
                    if not line:
                        continue

                    # Signal
                    if line.startswith('Ss'):
                        parts = line[2:].split(',')
                        if len(parts) >= 2:
                            try:
                                sig  = float(parts[0])
                                qual = int(parts[1])
                                ok   = sig >= threshold
                                with state_lock:
                                    state['signal_dbuv'] = sig
                                    state['quality']     = qual
                                    state['signal_ok']   = ok

                                # Historique en mémoire
                                signal_hist.append({'ts': time.time(), 'v': sig})

                                # DB toutes les 60s
                                if time.time() - last_db_ts > 60:
                                    db_insert_signal(sig, qual)
                                    last_db_ts = time.time()

                                # Alertes perte / rétablissement du signal
                                if not ok:
                                    if lost_since is None:
                                        lost_since = time.time()
                                    elif not alert_sent and (time.time() - lost_since) > alert_delay:
                                        _send_alert('signal_lost',
                                            f'Signal perdu — {sig:.1f} dBµV (seuil {threshold})')
                                        alert_sent = True
                                else:
                                    # Signal au-dessus du seuil
                                    if alert_sent and lost_since is not None:
                                        duration = int(time.time() - lost_since)
                                        _send_alert('signal_restored',
                                            f'Signal rétabli — {sig:.1f} dBµV après {duration}s de coupure')
                                    lost_since = None
                                    alert_sent = False

                            except ValueError:
                                pass

                    # PI
                    elif line.startswith('P'):
                        pi = line[1:].strip().upper()
                        with state_lock:
                            old = state.get('pi')
                            state['pi']     = pi
                            state['rds_ok'] = True
                        # Changement de PI → réinitialiser le logo
                        if pi and pi != old:
                            with _logo_lock:
                                _logo_state['searched']     = False
                                _logo_state['last_attempt'] = 0
                                _logo_state['current_pi']   = pi
                            with state_lock:
                                state['station_logo'] = None
                        # Lancer la recherche logo si pas encore faite
                        with _logo_lock:
                            need_logo = not _logo_state['searched']
                        if need_logo:
                            threading.Thread(target=_fetch_station_logo, daemon=True).start()

                    # RDS frames
                    elif line.startswith('R'):
                        data = line[1:]
                        if len(data) < 4:
                            continue
                        frame_type = data[0:2]

                        if frame_type == '04':
                            try:
                                seg      = int(data[2:4], 16) & 0x03
                                ps_bytes = bytes.fromhex(data[8:12])
                                ps_segs[seg] = ps_bytes.decode('latin-1', errors='replace').replace('\x00', ' ')
                                if len(ps_segs) == 4:
                                    ps = ''.join(ps_segs[i] for i in range(4)).strip()
                                    with state_lock:
                                        state['ps'] = ps
                                    ps_segs = {}
                            except Exception:
                                pass

                        elif frame_type == '24':
                            try:
                                seg   = int(data[2:4], 16) & 0x0F
                                chars = bytes.fromhex(data[4:12]).decode('latin-1', errors='replace')
                                # Fin de RT (carriage return)
                                if '\x0d' in chars:
                                    rt_segs[seg] = chars[:chars.index('\x0d')]
                                    rt = ''.join(rt_segs.get(i,'') for i in range(max(rt_segs)+1))
                                    with state_lock:
                                        state['rt'] = rt.strip()
                                    rt_segs = {}
                                else:
                                    # Nouveau cycle détecté
                                    if seg == 0 and rt_segs:
                                        rt = ''.join(rt_segs.get(i,'') for i in range(max(rt_segs)+1))
                                        with state_lock:
                                            state['rt'] = rt.strip()
                                        rt_segs = {}
                                    rt_segs[seg] = chars.replace('\x00','')
                                    # Affichage progressif
                                    rt = ''.join(rt_segs.get(i,'') for i in range(max(rt_segs)+1))
                                    with state_lock:
                                        state['rt'] = rt.strip()
                            except Exception:
                                pass

        except Exception as e:
            log.error(f'TEF reader error: {e}')
            with state_lock:
                state['signal_ok'] = False
            time.sleep(5)

# ─── Capture engine (single reader → FFT + Icecast) ───────────────────────────

def capture_engine():
    """Un seul arecord 192kHz alimente le FFT MPX ET le streaming Icecast.
    Evite toute concurrence sur le device ALSA."""
    cfg       = load_config()
    card      = cfg.get('hifiberry', {}).get('alsa_device', 'hw:2,0')
    gain      = float(cfg.get('hifiberry', {}).get('software_gain', 20.0))
    rate      = 192000
    channels  = 2
    n_fft     = 1024
    chunk     = 4096

    icecast   = cfg.get('icecast', {})
    host      = icecast.get('host', 'localhost')
    port      = icecast.get('port', 8000)
    mount     = icecast.get('mount', '/stream.mp3')
    password  = icecast.get('source_password', 'hackme')
    bitrate   = icecast.get('bitrate', '128k')

    bytes_per_frame = 2 * channels
    target_bytes    = chunk * bytes_per_frame
    hz_per_bin      = rate / n_fft

    def fbin(f): return int(f / hz_per_bin)
    b_pilot = slice(fbin(18500), fbin(19500))
    b_rds   = slice(fbin(55000), fbin(59000))
    window  = np.hanning(chunk)

    log.info(f'Capture engine starting on {card} (gain x{gain})')

    fft_counter = 0

    while True:
        p_rec = p_ff = None
        try:
            cmd_rec = ['arecord', '-D', card, '-r', str(rate),
                       '-c', str(channels), '-f', 'S16_LE', '-t', 'raw', '-']
            cmd_ff  = [
                'ffmpeg', '-loglevel', 'error',
                '-f', 's16le', '-ar', str(rate), '-ac', str(channels), '-i', 'pipe:0',
                '-af', 'pan=mono|c0=c0,lowpass=f=15000',
                '-c:a', 'libmp3lame', '-b:a', bitrate, '-ar', '44100',
                '-f', 'mp3',
                f'icecast://source:{password}@{host}:{port}{mount}'
            ]
            p_rec = subprocess.Popen(cmd_rec, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL)
            p_ff  = subprocess.Popen(cmd_ff, stdin=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL)

            with state_lock:
                state['stream_ok'] = True
            log.info('Capture engine active (stream + FFT)')

            while True:
                data = p_rec.stdout.read(target_bytes)
                if not data or len(data) < target_bytes:
                    break

                # 1) PRIORITE : alimenter ffmpeg (audio fluide)
                try:
                    p_ff.stdin.write(data)
                except (BrokenPipeError, OSError):
                    break

                # 2) FFT une fois sur deux (allege le CPU)
                fft_counter += 1
                if fft_counter % 2 != 0:
                    continue

                samples = np.frombuffer(data, dtype=np.int16)
                left    = samples[0::2].astype(np.float32) / 32768.0
                left    = left * gain

                fft = np.fft.rfft(left * window, n=n_fft)
                mag = np.abs(fft) / (n_fft / 2)
                mag = np.maximum(mag, 1e-10)
                db  = 20 * np.log10(mag)

                pilot_db = float(np.max(db[b_pilot]))
                rds_db   = float(np.max(db[b_rds]))
                power_db = float(np.mean(db))

                with state_lock:
                    state['mpx_spectrum']    = db.tolist()
                    state['mpx_sample_rate'] = rate
                    state['mpx_hz_per_bin']  = hz_per_bin
                    state['mpx_pilot_db']    = pilot_db
                    state['mpx_rds_db']      = rds_db
                    state['mpx_power_db']    = power_db

        except Exception as e:
            log.error(f'Capture engine error: {e}')
        finally:
            with state_lock:
                state['stream_ok'] = False
            for p in (p_ff, p_rec):
                if p:
                    try: p.kill()
                    except Exception: pass
        time.sleep(3)

# ─── Email alert ──────────────────────────────────────────────────────────────

def _send_alert(kind, message):
    import smtplib
    from email.mime.text import MIMEText
    try:
        cfg   = load_config()
        ecfg  = cfg.get('email', {})
        if not ecfg.get('enabled', False):
            return
        recipients = ecfg.get('recipient_emails', [])
        if isinstance(recipients, str):
            recipients = [r.strip() for r in recipients.split(',')]
        msg = MIMEText(message)
        msg['Subject'] = f'[BL-FMO-TEF] {kind}'
        msg['From']    = ecfg.get('sender_email', '')
        msg['To']      = ', '.join(recipients)
        with smtplib.SMTP(ecfg.get('smtp_host', 'smtp.gmail.com'),
                          ecfg.get('smtp_port', 587)) as s:
            s.starttls()
            s.login(ecfg.get('sender_email', ''),
                    ecfg.get('sender_password', '').replace(' ', ''))
            s.sendmail(msg['From'], recipients, msg.as_string())
        db_insert_alert(kind, message)
        with state_lock:
            state['alerts_sent'] += 1
        log.info(f'Alert sent: {kind}')
    except Exception as e:
        log.error(f'Alert error: {e}')

# ─── Flask routes ─────────────────────────────────────────────────────────────

@app.route('/')
@login_required
def index():
    return render_template('index.html')

@app.route('/config')
@login_required
def config_page():
    return render_template('config.html')

@app.route('/stats')
@login_required
def stats_page():
    return render_template('stats.html')

@app.route('/about')
@login_required
def about_page():
    return render_template('about.html')

# ── API publiques (dashboard JS) ──────────────────────────────────────────────

def _stats_payload():
    with state_lock:
        s = dict(state)
    return {
        'signal_dbuv':  s['signal_dbuv'],
        'quality':      s['quality'],
        'signal_ok':    s['signal_ok'],
        'rds_ok':       s['rds_ok'],
        'pi':           s['pi'],
        'ps':           s['ps'],
        'rt':           s['rt'],
        'stream_ok':    s['stream_ok'],
        'uptime':       int(time.time() - s['uptime_start']),
        'alerts_sent':  s['alerts_sent'],
        'mpx_pilot_db': s['mpx_pilot_db'],
        'mpx_rds_db':   s['mpx_rds_db'],
        'mpx_power_db': s['mpx_power_db'],
        'station_logo': s['station_logo'],
        'start_time':   datetime.fromtimestamp(s['uptime_start']).strftime('%d/%m/%Y %H:%M'),
    }

@app.route('/api/stats')
def api_stats():
    return jsonify(_stats_payload())

@app.route('/api/stream/stats')
def api_stream_stats():
    """Server-Sent Events : pousse les stats ~2 fois/seconde."""
    def gen():
        while True:
            try:
                yield f"data: {json.dumps(_stats_payload())}\n\n"
            except Exception:
                break
            time.sleep(0.5)
    return Response(gen(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache',
                             'X-Accel-Buffering': 'no'})

@app.route('/api/mpx/spectrum')
def api_mpx_spectrum():
    with state_lock:
        return jsonify({
            'spectrum':    state['mpx_spectrum'],
            'sample_rate': state['mpx_sample_rate'],
            'hz_per_bin':  state['mpx_hz_per_bin'],
        })

@app.route('/api/config/full')
@login_required
def api_config_full():
    return jsonify(load_config())

@app.route('/api/config/save', methods=['POST'])
@login_required
def api_config_save():
    try:
        incoming = request.get_json()
        current  = load_config()

        # Fusion section par section
        for section, values in incoming.items():
            if section == 'auth':
                # Gestion spéciale : hash du mot de passe
                current.setdefault('auth', {})
                if values.get('username'):
                    current['auth']['username'] = values['username']
                if values.get('password'):
                    current['auth']['password_hash'] = bcrypt.generate_password_hash(
                        values['password']).decode()
                continue

            current.setdefault(section, {})
            if isinstance(values, dict):
                for k, v in values.items():
                    # Mots de passe vides = ne pas écraser
                    if k in ('source_password', 'sender_password') and v == '':
                        continue
                    current[section][k] = v
            else:
                current[section] = values

        save_config(current)
        return jsonify({'status': 'success', 'ok': True})
    except Exception as e:
        return jsonify({'status': 'error', 'ok': False, 'error': str(e)}), 400


@app.route('/api/test-email', methods=['POST'])
@login_required
def api_test_email():
    import smtplib
    from email.mime.text import MIMEText
    try:
        cfg  = load_config()
        ecfg = cfg.get('email', {})
        sender = ecfg.get('sender_email', '')
        pw     = ecfg.get('sender_password', '').replace(' ', '')
        recips = ecfg.get('recipient_emails', [])
        if isinstance(recips, str):
            recips = [r.strip() for r in recips.split(',') if r.strip()]
        if not sender or not pw or not recips:
            return jsonify({'status': 'error', 'message': 'Config email incomplète'}), 400

        msg = MIMEText('Email de test BL-FMO-TEF — la configuration fonctionne.')
        msg['Subject'] = '[BL-FMO-TEF] Email de test'
        msg['From']    = sender
        msg['To']      = ', '.join(recips)
        with smtplib.SMTP(ecfg.get('smtp_host', 'smtp.gmail.com'),
                          ecfg.get('smtp_port', 587), timeout=15) as s:
            s.starttls()
            s.login(sender, pw)
            s.sendmail(sender, recips, msg.as_string())
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/restart', methods=['POST'])
@login_required
def api_restart():
    import shutil
    try:
        if not shutil.which('systemctl'):
            return jsonify({'status': 'error', 'message': 'systemctl indisponible'}), 500
        # Restart DÉTACHÉ : ce processus est lui-même le service à redémarrer.
        # On lance la commande dans une nouvelle session pour qu'elle survive
        # à l'arrêt du worker, et on répond AVANT que systemd ne nous arrête.
        subprocess.Popen(
            ['sudo', 'systemctl', 'restart', 'bl-fmo-tef'],
            start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/config/export')
@login_required
def api_config_export():
    import io, zipfile
    from flask import send_file
    buf = io.BytesIO()
    base = os.path.dirname(__file__)
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for fname in ('config.json', '.env'):
            fpath = os.path.join(base, fname)
            if os.path.exists(fpath):
                z.write(fpath, fname)
    buf.seek(0)
    return send_file(buf, mimetype='application/zip',
                     as_attachment=True, download_name='bl-fmo-tef-backup.zip')


@app.route('/api/config/import', methods=['POST'])
@login_required
def api_config_import():
    import zipfile
    try:
        f = request.files.get('file')
        if not f:
            return jsonify({'success': False, 'error': 'Aucun fichier'}), 400
        base = os.path.dirname(__file__)
        with zipfile.ZipFile(f) as z:
            for name in z.namelist():
                if name in ('config.json', '.env'):
                    with z.open(name) as src_f, open(os.path.join(base, name), 'wb') as dst_f:
                        dst_f.write(src_f.read())
        return jsonify({'success': True, 'ok': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/csrf-token')
def api_csrf_token():
    from flask_wtf.csrf import generate_csrf
    return jsonify({'csrf_token': generate_csrf()})

@app.route('/api/signal/history')
@login_required
def api_signal_history():
    data = list(signal_hist)
    return jsonify(data)

@app.route('/api/logs')
@login_required
def api_logs():
    """100 dernières lignes du journal systemd du service."""
    try:
        r = subprocess.run(
            ['journalctl', '-u', 'bl-fmo-tef', '-n', '100', '--no-pager', '--output', 'short'],
            capture_output=True, text=True, timeout=10
        )
        return jsonify({'logs': r.stdout or '(journal vide)'})
    except Exception as e:
        return jsonify({'logs': f'Erreur lecture logs : {e}'}), 500


@app.route('/api/signal/history/db')
@login_required
def api_signal_history_db():
    """Historique signal depuis la base (graphe stats)."""
    try:
        hours = int(request.args.get('hours', 24))
    except ValueError:
        hours = 24
    since = (datetime.now() - timedelta(hours=hours)).isoformat()
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        'SELECT ts, signal, quality FROM signal_history WHERE ts >= ? ORDER BY id ASC',
        (since,)
    ).fetchall()
    con.close()
    return jsonify([{'ts': r[0], 'signal': r[1], 'quality': r[2]} for r in rows])


@app.route('/api/alerts/history')
@login_required
def api_alerts_history():
    con  = sqlite3.connect(DB_PATH)
    rows = con.execute('SELECT ts, kind, message FROM alerts ORDER BY id DESC LIMIT 100').fetchall()
    con.close()
    return jsonify([{'ts': r[0], 'kind': r[1], 'message': r[2]} for r in rows])

# ─── Background threads (idempotent, dev + Gunicorn) ──────────────────────────

_bg_started = False
_bg_lock    = threading.Lock()

def start_background():
    """Démarre les threads TEF + capture une seule fois.
    Appelé en dev (__main__) et sous Gunicorn (hook post_fork)."""
    global _bg_started
    with _bg_lock:
        if _bg_started:
            return
        _bg_started = True
    db_init()
    threading.Thread(target=tef_reader,     daemon=True, name='tef').start()
    threading.Thread(target=capture_engine, daemon=True, name='capture').start()
    log.info('BL-FMO-TEF background threads started')

# ─── Main (dev only — Gunicorn n'exécute pas ce bloc) ─────────────────────────

if __name__ == '__main__':
    start_background()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)),
            debug=False, threaded=True)
