"""Undo the firmware's integer EMA on the potentiometers and check the result against the onboard video.
Firmware (adc.c): ema += ((raw - ema) * 32) >> 8, in ADC counts, once per 10 ms sample. So the step is floor((raw-ema)/8):
raw lies in [ema_prev + 8*step, ema_prev + 8*step + 7] -> raw ~ ema_prev + 8*step + 3.5 counts (+-3.5 counts, ~0.15 mm).
Needs two consecutive samples; a lost sample breaks the chain for one step."""
import numpy as np, pandas as pd, json, sys
from common import load_sensor, load_pots, video_t0
VMAX = dict(FR=1825, RR=1835, RL=1827, FL=1818)
mm_per_count = lambda ch: 3300 / 4095 / VMAX[ch] * 100
def invert(x, s, ch):
    k = mm_per_count(ch); dx = np.diff(x); ds = np.diff(s); ok = (ds >= 8) & (ds <= 12)
    step = np.round(dx / k); r = np.full(len(x), np.nan)
    r[1:] = np.where(ok, x[:-1] + (8 * step + 3.5) * k, np.nan)
    return r, ok.mean()
def ema_gain(f, fs=100, a=32 / 256):
    z = np.exp(-2j * np.pi * f / fs); return np.abs(a / (1 - (1 - a) * z))
def to_frames(t_wall, y, tv, max_gap=0.03):
    """100 Hz grid -> fill gaps <= max_gap -> 1/15 s boxcar -> value at each video frame time"""
    g = np.arange(tv.min() - 1, tv.max() + 1, 0.01); m = np.isfinite(y)
    tt, yy = t_wall[m], y[m]; v = np.interp(g, tt, yy)
    j = np.searchsorted(tt, g); near = np.minimum(np.abs(g - tt[np.clip(j, 0, len(tt) - 1)]), np.abs(g - tt[np.clip(j - 1, 0, len(tt) - 1)]))
    v[near > max_gap] = np.nan
    v = pd.Series(v).rolling(7, center=True, min_periods=7).mean().values
    return np.interp(tv, g, v, left=np.nan, right=np.nan) if np.isfinite(v).any() else np.full(len(tv), np.nan), v, g
def spectra(ref, sigs, fs=15.0, n=64):
    """Welch-style cross spectra on contiguous runs where everything is finite; returns freqs and per-signal H = Sxv/Svv, coherence"""
    good = np.isfinite(ref)
    for s in sigs: good &= np.isfinite(s)
    runs = np.flatnonzero(np.diff(np.r_[0, good.astype(int), 0]))
    acc = None; w = np.hanning(n); f = np.fft.rfftfreq(n, 1 / fs); nseg = 0
    for a, b in zip(runs[::2], runs[1::2]):
        for i in range(a, b - n + 1, n // 2):
            V = np.fft.rfft(w * (ref[i:i + n] - ref[i:i + n].mean()))
            X = [np.fft.rfft(w * (s[i:i + n] - s[i:i + n].mean())) for s in sigs]
            cur = [np.abs(V) ** 2] + [x * np.conj(V) for x in X] + [np.abs(x) ** 2 for x in X]
            acc = cur if acc is None else [p + q for p, q in zip(acc, cur)]; nseg += 1
    if acc is None: return f, None, 0
    Svv = acc[0]; k = len(sigs); H = [acc[1 + i] / Svv for i in range(k)]; C = [np.abs(acc[1 + i]) ** 2 / (Svv * acc[1 + k + i]) for i in range(k)]
    return f, (H, C), nseg
if __name__ == '__main__':
    rows = []; keep = {}
    for sid, lag in json.load(open('out/offset_hints.json')) and [(h['session'], h['video_to_log_lag_s']) for h in json.load(open('out/offset_hints.json')) if h['method'].startswith('pot')]:
        d, _ = load_sensor(sid); d = d.sort_values('stm').reset_index(drop=True)
        g = load_pots(sid); tv = video_t0(sid) + g.t_ms.values / 1000 + lag
        for ch, px in (('FR', 'R_px'), ('FL', 'L_px')):
            x = d[ch].values.astype(float)
            if np.nanstd(x) < 0.3: continue           # dead channel
            r, share = invert(x, d.stm.values.astype(float), ch)
            # stationary noise of the reconstruction: car standing still
            still = (d.vehicle_speed_kmh.values < 0.5)
            res = (r - pd.Series(x).rolling(41, center=True, min_periods=10).median().values)[still]
            lg, _, _ = to_frames(d.t_wall.values, x, tv); rc, _, _ = to_frames(d.t_wall.values, r, tv)
            frame_idx = np.round(g.t_ms.values / (1000 / 15)).astype(int); N = frame_idx.max() + 1
            V = np.full(N, np.nan); L = np.full(N, np.nan); R = np.full(N, np.nan)
            V[frame_idx] = g[px].values; L[frame_idx] = lg; R[frame_idx] = rc
            f, out, nseg = spectra(V, [L, R])
            if out is None: continue
            (Hl, Hr), (Cl, Cr) = out
            keep[(sid[:15], ch)] = dict(f=f, Hl=Hl, Hr=Hr, Cl=Cl, Cr=Cr, nseg=nseg)
            for lo, hi in ((0.3, 1), (1, 2), (2, 3), (3, 4.5), (4.5, 7)):
                b = (f >= lo) & (f < hi)
                rows.append(dict(session=sid[:15], ch=ch, band=f'{lo}-{hi} Hz', segs=nseg, pairs_share=round(share, 2), still_noise_mm=round(float(np.nanstd(res)), 3),
                    gain_log=float(np.abs(Hl[b]).mean()), gain_recon=float(np.abs(Hr[b]).mean()), ema_theory=float(ema_gain(f[b]).mean()),
                    coh_log=round(float(Cl[b].mean()), 2), coh_recon=round(float(Cr[b].mean()), 2),
                    phase_log_deg=round(float(np.degrees(np.angle(Hl[b].mean()))), 0), phase_recon_deg=round(float(np.degrees(np.angle(Hr[b].mean()))), 0)))
            print(sid[:15], ch, 'segments', nseg, 'consecutive pairs %.0f%%' % (100 * share), 'still noise %.3f mm' % np.nanstd(res))
    T = pd.DataFrame(rows)
    # gains are relative to video px; normalise both by the log's 0.3-1 Hz gain, where the EMA passes ~everything
    for (s, c), G in T.groupby(['session', 'ch']):
        ref = G[G.band == '0.3-1 Hz'].gain_log.values[0]; T.loc[G.index, 'gain_log_n'] = (G.gain_log / ref).round(2); T.loc[G.index, 'gain_recon_n'] = (G.gain_recon / ref).round(2)
    T['ema_theory'] = T.ema_theory.round(2); T.to_csv('out/ema_inverse.csv', index=False); pd.to_pickle(keep, 'out/ema_inverse_spectra.pkl')
    print(T[['session','ch','band','segs','gain_log_n','gain_recon_n','ema_theory','coh_log','coh_recon','phase_log_deg','phase_recon_deg']].to_string(index=False))
