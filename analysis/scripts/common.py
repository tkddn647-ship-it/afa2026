import pandas as pd, numpy as np, json, os
ROOT = os.environ.get('AFA_DATA', os.path.expanduser('~/mnt/afa_data'))
SUS = ['FR', 'FL', 'RR', 'RL']
def load_sensor(sid):
    """Load a sensor log, drop corrupted rows, return df with t_wall (unix s) from a robust linear STM->server clock fit."""
    p = f'{ROOT}/udp_logger/logs/sensor_log_{sid}.csv.gz'
    if not os.path.exists(p): p = p[:-3]
    d = pd.read_csv(p, low_memory=False)
    n0 = len(d)
    # AFA_SCRUB=1: remove bit-flip spikes and over-limit values first (scrub.py / scrub_apply.py); off by default
    rowbad = np.zeros(len(d), bool)
    if os.environ.get('AFA_SCRUB') == '1':
        from scrub import spikes
        from scrub_apply import SCRUB, LIMIT
        o = np.argsort(pd.to_numeric(d.stm_ms, errors='coerce').values, kind='stable'); nflag = np.zeros(len(d), int)
        for ch in SCRUB:
            if ch not in d: continue
            x = pd.to_numeric(d[ch], errors='coerce').values.astype(float)
            if np.nanstd(x) == 0: continue
            xs = x[o]; m, _ = spikes(xs, ch)
            if ch in LIMIT: m |= np.abs(xs) > LIMIT[ch]
            bad = np.zeros(len(x), bool); bad[o[m]] = True; x[bad] = np.nan; d[ch] = x; nflag += bad
        # two or more flagged channels in one row = a shifted or garbled line: drop the whole row (unflagged fields are suspect too)
        rowbad = nflag >= 2
    wall = pd.to_datetime(d.timestamp, format='%Y/%m/%d %H:%M:%S.%f', utc=True).astype('int64').values / 1e9
    s = d.stm_ms.astype(float).values
    med = pd.Series(s).rolling(41, center=True, min_periods=5).median().values
    ok = ~rowbad & (np.abs(s - med) < 3000) & (d[SUS].abs().max(axis=1).fillna(0 if os.environ.get('AFA_SCRUB') == '1' else np.inf).values < 150) & (d.x_g.abs().values < 6) & (d.y_g.abs().values < 6)
    d = d[ok].copy(); s = s[ok]; wall = wall[ok]
    # server receive time = STM time + latency(>=0): fit slope robustly, then anchor on the low-latency envelope
    # split at STM time resets (large persistent backward jumps), fit one slope, one intercept per segment
    seg = np.r_[0, np.cumsum(np.diff(s) < -5000)]
    keep = np.r_[True, (np.diff(s) > 0) | (np.diff(seg) > 0)]
    d = d[keep]; s = s[keep]; wall = wall[keep]; seg = seg[keep]
    big = np.bincount(seg).argmax(); sb, wb = s[seg == big], wall[seg == big]
    a = np.polyfit(sb, wb, 1)[0]
    for _ in range(3):
        r = wb - a * sb; b = np.quantile(r, 0.02); sel = np.abs(r - b) < 0.05
        if sel.sum() > 100: a = np.polyfit(sb[sel], wb[sel], 1)[0]
    tw = np.empty_like(s)
    for k in np.unique(seg):
        m = seg == k; tw[m] = a * s[m] + np.quantile(wall[m] - a * s[m], 0.02)
    order = np.argsort(tw, kind='stable'); d = d.iloc[order]; s = s[order]; wall = wall[order]; tw = tw[order]
    d['t_wall'] = tw; d['stm'] = s; d['seg'] = seg[order]
    info = dict(n_seg=int(seg.max()+1), rows_raw=n0, rows_clean=len(d), stm_scale=a * 1000, latency_p50_ms=float(np.median(wall - d.t_wall) * 1000), latency_p95_ms=float(np.quantile(wall - d.t_wall, .95) * 1000))
    return d.reset_index(drop=True), info
def video_t0(sid):
    j = json.load(open(f'{ROOT}/udp_realtime/camera_recordings/camera-{sid}.mp4.sync.json'))
    return j['rtcp_mapping']['recording_first_rtp_ntp_ns'] / 1e9
def load_pots(sid):
    g = pd.read_csv(f'{ROOT}/_analysis/out/pots_{sid[4:15]}.csv').dropna()
    C = np.hypot(g.Ri_x - g.Li_x, g.Ri_y - g.Li_y)
    ok = (np.abs(C - C.median()) < 2.0) & g.Lo_x.between(338, 385) & g.Ro_x.between(555, 605) & (g[['Lo_area','Li_area','Ri_area','Ro_area']].min(axis=1) > 25)
    g = g[ok].copy()
    g['L_px'] = np.hypot(g.Lo_x - g.Li_x, g.Lo_y - g.Li_y); g['R_px'] = np.hypot(g.Ro_x - g.Ri_x, g.Ro_y - g.Ri_y)
    return g
def hp(x, n):  # high-pass by removing rolling median
    return x - pd.Series(x).rolling(n, center=True, min_periods=n // 4).median().values
