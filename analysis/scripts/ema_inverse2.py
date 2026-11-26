"""Second pass: (a) how much of the recovered raw signal is motion vs electrical noise, from its spectrum when standing
versus driving; (b) coherence with the video, only while moving, with short gaps bridged."""
import numpy as np, pandas as pd, json
from common import load_sensor, load_pots, video_t0
from ema_inverse import invert, ema_gain, spectra
def uniform(t, y, fs=100.0, max_gap=0.06):
    g = np.arange(t.min(), t.max(), 1 / fs); m = np.isfinite(y); tt, yy = t[m], y[m]
    v = np.interp(g, tt, yy); j = np.clip(np.searchsorted(tt, g), 1, len(tt) - 1)
    v[np.minimum(g - tt[j - 1], tt[j] - g) > max_gap] = np.nan; return g, v
def psd_runs(v, mask, fs=100.0, n=256):
    good = np.isfinite(v) & mask; runs = np.flatnonzero(np.diff(np.r_[0, good.astype(int), 0])); acc = 0; k = 0; w = np.hanning(n)
    for a, b in zip(runs[::2], runs[1::2]):
        for i in range(a, b - n + 1, n // 2):
            seg = v[i:i + n] - v[i:i + n].mean(); acc = acc + np.abs(np.fft.rfft(w * seg)) ** 2; k += 1
    return np.fft.rfftfreq(n, 1 / fs), (acc / max(k, 1)) / (fs * (w ** 2).sum()), k
H = {h['session']: h['video_to_log_lag_s'] for h in json.load(open('out/offset_hints.json')) if h['method'].startswith('pot')}
rows = []; P = {}
for sid, lag in H.items():
    d, _ = load_sensor(sid); d = d.sort_values('stm').reset_index(drop=True); t = d.t_wall.values
    spd = pd.Series(d.vehicle_speed_kmh.values).rolling(50, center=True, min_periods=1).median().values
    for ch in ('FR', 'FL'):
        x = d[ch].values.astype(float)
        if np.nanstd(x) < 0.3: continue
        r, share = invert(x, d.stm.values.astype(float), ch)
        bad = np.abs(r - pd.Series(x).rolling(21, center=True, min_periods=5).median().values) > 8   # bit-flip outliers (8 mm ~ 180 counts)
        r[bad] = np.nan
        g, R = uniform(t, r); _, X = uniform(t, x); sp = np.interp(g, t, spd)
        for state, m in (('standing', sp < 0.5), ('driving >15 km/h', sp > 15)):
            f, pr, k1 = psd_runs(R, m); _, px, _ = psd_runs(X, m)
            if k1 < 3: continue
            P[(sid[:15], ch, state)] = (f, pr, px)
            for lo, hi in ((0.5, 2), (2, 7), (7, 20)):
                b = (f >= lo) & (f < hi)
                rows.append(dict(session=sid[:15], ch=ch, state=state, band=f'{lo}-{hi} Hz', segs=k1, rms_recon_mm=round(float(np.sqrt(pr[b].sum() * (f[1] - f[0]))), 3), rms_logged_mm=round(float(np.sqrt(px[b].sum() * (f[1] - f[0]))), 3)))
        # coherence with video while moving
        gp = load_pots(sid); tv = video_t0(sid) + gp.t_ms.values / 1000 + lag
        fi = np.round(gp.t_ms.values / (1000 / 15)).astype(int); N = fi.max() + 1; tf = video_t0(sid) + np.arange(N) / 15 + lag
        V = np.full(N, np.nan); V[fi] = gp['R_px' if ch == 'FR' else 'L_px'].values
        V = pd.Series(V).interpolate(limit=2, limit_area='inside').values
        box = lambda Y: pd.Series(Y).rolling(7, center=True, min_periods=7).mean().values   # ~1/15 s
        Lf = np.interp(tf, g, box(X), left=np.nan, right=np.nan); Rf = np.interp(tf, g, box(R), left=np.nan, right=np.nan)
        mv = np.interp(tf, g, sp) > 15; V2 = np.where(mv, V, np.nan)
        f, out, nseg = spectra(V2, [Lf, Rf], n=48)
        if out is None: continue
        (Hl, Hr), (Cl, Cr) = out
        for lo, hi in ((0.5, 2), (2, 4), (4, 7)):
            b = (f >= lo) & (f < hi)
            rows.append(dict(session=sid[:15], ch=ch, state='video coherence (moving)', band=f'{lo}-{hi} Hz', segs=nseg, coh_logged=round(float(Cl[b].mean()), 2), coh_recon=round(float(Cr[b].mean()), 2), gain_ratio_recon_over_logged=round(float(np.abs(Hr[b]).mean() / np.abs(Hl[b]).mean()), 2), ema_inverse_theory=round(float((1 / ema_gain(f[b])).mean()), 2)))
T = pd.DataFrame(rows); T.to_csv('out/ema_inverse2.csv', index=False); pd.to_pickle(P, 'out/ema_inverse_psd.pkl')
pd.set_option('display.width', 250); print(T.to_string(index=False))
