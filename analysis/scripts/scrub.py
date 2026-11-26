"""Find single-sample values hit by a UART bit flip. A flipped digit makes an isolated spike: the samples before and
after agree with each other, the one in between does not. If it is a bit flip, the jump is d x 10^p with d in
{1, 2, 4, 8} (one of the low four bits of an ASCII digit), so the share of spikes with that size tells whether the
detector finds bit errors or real motion."""
import numpy as np, pandas as pd, glob, sys
DEC = dict(FR=2, FL=2, RR=2, RL=2, x_g=3, y_g=3, z_g=3, ecu_temp=1, inv_temp_igt=1, inv_temp_motor=1, inv_motor_speed=0,
           inv_dc_current=1, inv_voltage=1, INV_iq_feedback=1, INV_torque_feedback=1, INV_torque_commanded=1, bms_voltage=1, bms_current=1)
FLOOR = dict(FR=0.5, FL=0.5, RR=0.5, RL=0.5, x_g=0.15, y_g=0.15, z_g=0.15, ecu_temp=1, inv_temp_igt=1, inv_temp_motor=1, inv_motor_speed=40,
             inv_dc_current=8, inv_voltage=2, INV_iq_feedback=8, INV_torque_feedback=4, INV_torque_commanded=4, bms_voltage=2, bms_current=8)
def flip_like(jump, dec):
    """|jump| = d * 10^p, d in {1,2,4,8}, p from -dec up to 4, within half a printed step"""
    a = np.abs(jump); step = 10.0 ** -dec; out = np.zeros(len(a), bool)
    for p in range(-dec, 5):
        for d in (1, 2, 4, 8): out |= np.abs(a - d * 10.0 ** p) <= 0.5 * step + 1e-9
    return out
def spikes(x, ch):
    prv, nxt = np.r_[np.nan, x[:-1]], np.r_[x[1:], np.nan]
    ref = (prv + nxt) / 2; agree = np.abs(prv - nxt); jump = x - ref
    local = pd.Series(np.abs(np.diff(x, prepend=x[0]))).rolling(101, center=True, min_periods=20).median().values
    thr = np.maximum(FLOOR[ch], 8 * local)
    return np.isfinite(jump) & (np.abs(jump) > thr) & (agree < np.abs(jump) / 4), jump
def run(path):
    d = pd.read_csv(path, low_memory=False); s = pd.to_numeric(d.stm_ms, errors='coerce').values; o = np.argsort(s, kind='stable'); d = d.iloc[o]
    ds = np.diff(s[o]); cont = np.r_[False, (ds >= 8) & (ds <= 12)] & np.r_[(ds >= 8) & (ds <= 12), False]   # both neighbours one step away
    res = {}; tot = tot_flip = 0; flagged = np.zeros(len(d), bool)
    for ch, dec in DEC.items():
        if ch not in d: continue
        x = pd.to_numeric(d[ch], errors='coerce').values.astype(float)
        if np.nanstd(x) == 0: continue
        m, j = spikes(x, ch); m &= cont; fl = flip_like(j, dec) & m
        res[ch] = (int(m.sum()), int(fl.sum())); tot += m.sum(); tot_flip += fl.sum(); flagged |= m
    return res, int(tot), int(tot_flip), len(d), flagged
if __name__ == '__main__':
    rows = []
    for f in sorted(glob.glob('../udp_logger/logs/sensor_log_2026*.csv*')):
        if f.endswith(('bak_steer', 'bak_steer_est')): continue
        sid = f.split('sensor_log_')[1][:15]
        try: res, tot, fl, n, _ = run(f)
        except Exception as e: continue
        if n < 3000: continue
        rows.append(dict(session=sid, rows=n, spikes=tot, spikes_per_1000=round(1000 * tot / n, 2), flip_sized_share=round(fl / tot, 2) if tot else np.nan,
                         by_channel=';'.join(f'{k}:{a}/{b}' for k, (a, b) in res.items() if a)))
        print(rows[-1]['session'], rows[-1]['spikes_per_1000'], rows[-1]['flip_sized_share'])
    T = pd.DataFrame(rows); T.to_csv('out/scrub_sessions.csv', index=False)
