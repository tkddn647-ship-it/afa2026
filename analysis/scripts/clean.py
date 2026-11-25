"""Build cleaned per-session datasets: corrupt rows removed, STM-based time axis, corrected suspension
labels, firmware EMA (alpha=32/256 at 100 Hz) inverted on the pot channels, video offset attached."""
import numpy as np, pandas as pd, json, os, sys
from common import *
BETA = 1 - 32/256
STD = dict(FL='FL', FR='FR', RL='RL', RR='RR'); SWAP = dict(FL='RR', FR='FR', RL='RL', RR='FL')   # physical <- logged label
SUS_OK = {s: STD for s in ['20260811_022758_150378','20260811_025402_438882','20260811_071828_103551','20260811_073314_333336']}
SUS_OK.update({s: SWAP for s in ['20260811_082807_765988','20260811_084517_257245','20260813_001309_427966']})
DRIVE_731 = ['20260731_031858_070588','20260731_034542_245924','20260731_091829_496066','20260731_094323_267640','20260731_095728_607711','20260731_101840_574049','20260731_103420_339055','20260731_104300_866808']
KEEP = ['x_g','y_g','z_g','steering_angle','steering_speed','vehicle_speed_kmh','inv_motor_speed','inv_dc_current','inv_voltage','INV_torque_commanded','INV_torque_feedback','INV_iq_feedback','INV_iq_command','bms_voltage','bms_current','bms_charge','inv_temp_igt','inv_temp_motor','bms_temp_maxvalue']
def clean(sid):
    d, info = load_sensor(sid)
    d = d[np.r_[True, np.diff(d.t_wall.values) > 0]].reset_index(drop=True)
    k = np.r_[1, np.rint(np.diff(d.stm.values) / 10)].astype(int)
    out = pd.DataFrame({'t': d.t_wall - d.t_wall.iloc[0], 'stm_ms': d.stm.astype('int64'), 'gap': k})
    mp = SUS_OK.get(sid)
    if mp:
        for phys, lab in mp.items():
            y = d[lab].values; yp = np.r_[y[0], y[:-1]]; b = BETA ** np.clip(k, 1, 2)
            x = (y - b * yp) / (1 - b); x[k > 2] = np.nan; x[0] = np.nan
            out['sus_' + phys] = y; out['sus_' + phys + '_inv'] = np.round(x, 3)
    for c in KEEP:
        if c in d: out[c] = d[c].values
    hints = {h['session']: h for h in json.load(open(f'{ROOT}/_analysis/out/offset_hints.json'))}
    out.attrs['video_lag_s'] = hints.get(sid, {}).get('video_to_log_lag_s')
    return out, info
if __name__ == '__main__':
    summ = []
    for sid in list(SUS_OK) + DRIVE_731:
        o, info = clean(sid); o.to_csv(f'{ROOT}/_analysis/clean/{sid[:15]}.csv.gz', index=False, float_format='%.4f')
        summ.append(dict(session=sid[:15], rows=len(o), duration_s=round(float(o.t.iloc[-1]), 1), suspension_valid=sid in SUS_OK, label_fix='FL<->RR' if SUS_OK.get(sid) is SWAP else '', rows_removed=info['rows_raw'] - len(o), stm_clock_scale=round(float(info['stm_scale']), 5), video_lag_s=o.attrs['video_lag_s']))
    pd.DataFrame(summ).to_csv(f'{ROOT}/_analysis/clean/README_sessions.csv', index=False); print(pd.DataFrame(summ).to_string())
