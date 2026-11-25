import numpy as np, pandas as pd, sys, json
from common import *
out=[]
for sid in ['20260731_034542_245924','20260731_091829_496066','20260731_092911_551450','20260731_101840_574049','20260731_103420_339055','20260731_104300_866808']:
    try:
        d,info=load_sensor(sid); g=load_pots(sid); t0=video_t0(sid)
    except Exception as e: print(sid,'ERR',e); continue
    tv=t0+g.t_ms.values/1000
    tg=np.arange(d.t_wall.iloc[0],d.t_wall.iloc[-1],0.01)
    S={c:np.convolve(np.interp(tg,d.t_wall,d[c]),np.ones(7)/7,'same') for c in SUS+['y_g']}
    V={k:hp(g[k].values,75) for k in ('L_px','R_px')}
    lags=np.arange(-8,8.001,0.01)
    r=dict(sid=sid[4:15],frames_ok=len(g),sensor_rows=info['rows_clean'],stm_scale=round(info['stm_scale'],5))
    for k in V:
        for c in SUS:
            cc=np.array([np.corrcoef(hp(np.interp(tv+l,tg,S[c]),75),V[k])[0,1] for l in lags[::5]])
            i=np.argmax(np.abs(cc)); l0=lags[::5][i]
            fine=np.arange(l0-0.06,l0+0.06,0.005); cf=np.array([np.corrcoef(hp(np.interp(tv+l,tg,S[c]),75),V[k])[0,1] for l in fine]); j=np.argmax(np.abs(cf))
            r[f'{k[0]}~{c}']=round(cf[j],2); r[f'{k[0]}~{c}_lag']=round(fine[j],3)
    out.append(r)
df=pd.DataFrame(out); pd.set_option('display.width',250); pd.set_option('display.max_columns',40)
print(df[['sid','frames_ok','sensor_rows','stm_scale']+[f'{k}~{c}' for k in 'LR' for c in SUS]].to_string())
print(df[['sid']+[f'{k}~{c}_lag' for k in 'LR' for c in SUS]].to_string())
df.to_csv('out/sync_summary_0731.csv',index=False)
