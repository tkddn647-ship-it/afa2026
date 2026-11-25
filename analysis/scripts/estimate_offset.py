"""Estimate the video<->sensor-log time offset of one session from the horizon optical flow (yaw-rate proxy)
cross-correlated with lateral acceleration / speed.  Usage: python3 estimate_offset.py <session_id> [yaw_csv]
(yaw_csv comes from track_yaw.py; offset_s = seconds to add to video time to get sensor-log time)"""
import numpy as np, pandas as pd, sys, json
from common import *
def estimate(sid, yaw_csv=None, span=6.0):
    d,info=load_sensor(sid); d=d[np.r_[True,np.diff(d.t_wall.values)>0]]
    y=pd.read_csv(yaw_csv or f'{ROOT}/_analysis/out/yaw_{sid[4:15]}.csv'); tv=video_t0(sid)+y.t_ms.values/1000; tt=y.t_ms.values/1000
    tg=np.arange(d.t_wall.iloc[0],d.t_wall.iloc[-1],0.01)
    v=np.interp(tg,d.t_wall,d.vehicle_speed_kmh)/3.6; ay=np.interp(tg,d.t_wall,d.y_g)*9.81
    r=np.convolve(np.where(v>3,ay/np.maximum(v,3),0.0),np.ones(15)/15,'same')
    dx=pd.Series(np.clip(y.dx.values,-60,60)).rolling(3,center=True,min_periods=1).median().values
    lags=np.arange(-span,span+1e-9,0.02); X=np.array([np.interp(tv+l,tg,r) for l in lags])
    cc=np.array([np.corrcoef(x,dx)[0,1] for x in X]); i=np.argmax(np.abs(cc)); wl=[]
    for a in np.arange(0,tt[-1]-60,30):
        m=(tt>=a)&(tt<a+60)
        if m.sum()<500 or np.std(X[len(lags)//2][m])<0.05: continue
        c=np.array([np.corrcoef(x[m],dx[m])[0,1] for x in X]); j=np.argmax(np.abs(c))
        if abs(c[j])>0.6: wl.append(lags[j])
    wl=np.array(wl); ok=len(wl)>=3
    return dict(session=sid,offset_s=round(float(np.median(wl)),2) if ok else round(float(lags[i]),2),basis='60 s windows' if ok else 'whole-session peak',n_windows=int(len(wl)),spread_s=round(float(np.quantile(wl,.75)-np.quantile(wl,.25)),2) if ok else None,peak_r=round(float(cc[i]),2),r_at_zero=round(float(cc[len(lags)//2]),2))
if __name__=='__main__': print(json.dumps(estimate(*sys.argv[1:3]),indent=1))
