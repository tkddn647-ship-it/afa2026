"""Loss and garbled rows per 2 s window (STM time), split by inverter state: contactor open, enabled at standstill,
rolling below 5 A, 5-40 A, above 40 A. Writes out/uart_loss_by_state.csv."""
import pandas as pd, numpy as np, glob, sys
from uart_integrity import K, LIM
LOGS = sys.argv[1] if len(sys.argv) > 1 else '../udp_logger/logs'
SESSIONS = ('20260728_025451','20260728_103916','20260731_031858','20260731_034542','20260731_095728','20260811_084517','20260813_001309')
rows = []
for sid in SESSIONS:
    f = [x for x in glob.glob(f'{LOGS}/sensor_log_{sid}*.csv*') if not x.endswith(('bak_steer','bak_steer_est'))][0]
    d = pd.read_csv(f, low_memory=False)
    s = pd.to_numeric(d.stm_ms, errors='coerce').values; ok = np.isfinite(s); d = d[ok]; s = s[ok]
    med = pd.Series(s).rolling(41, center=True, min_periods=5).median().values; g = np.abs(s - med) < 3000; d = d[g]; s = s[g]
    o = np.argsort(s, kind='stable'); d = d.iloc[o]; s = s[o]
    X = d[[k for k in K if k in d]].apply(pd.to_numeric, errors='coerce')
    garb = np.zeros(len(d), bool)
    for k, lim in LIM.items(): garb |= (np.abs(X[k].values) > lim)
    w = (s // 2000).astype(int)
    cur = np.abs(pd.to_numeric(d.inv_dc_current, errors='coerce').values); cur = np.where(cur > 450, np.nan, cur)
    cont = pd.to_numeric(d.motor_main_contactor, errors='coerce').values
    rpm = np.abs(pd.to_numeric(d.inv_motor_speed, errors='coerce').values); rpm = np.where(rpm > 7000, np.nan, rpm)
    W = pd.DataFrame(dict(w=w, cur=cur, cont=cont, rpm=rpm, garb=garb)).groupby('w').agg(n=('cur','size'), cur=('cur','median'), cont=('cont','median'), rpm=('rpm','median'), garb=('garb','sum'))
    W['loss'] = 1 - W.n / 200
    W['state'] = np.select([W.cont < 0.5, (W.cur < 5) & (W.rpm < 100), (W.cur < 5), (W.cur < 40), W.cur >= 40], ['contactor open','enabled, standstill','rolling, <5 A','5-40 A','>40 A'], '?')
    for st, G in W.groupby('state'):
        if len(G) < 5: continue
        rows.append(dict(session=sid, state=st, windows=len(G), loss_pct=round(100 * G.loss.clip(0, 1).mean(), 1), garbled_per_1000=round(1000 * G.garb.sum() / G.n.sum(), 2)))
T = pd.DataFrame(rows); T.to_csv('out/uart_loss_by_state.csv', index=False); print(T.to_string(index=False))
