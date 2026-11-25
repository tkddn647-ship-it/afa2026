"""7/31: per-session video<->log offset (pot method and yaw method), then front roll from video-left pot + logged FR."""
import numpy as np, pandas as pd, json
from common import *
from estimate_offset import estimate
sm=lambda x,n: pd.Series(x).rolling(n,center=True,min_periods=1).mean().values
SES=['20260731_031858_070588','20260731_034542_245924','20260731_091829_496066','20260731_092911_551450','20260731_101840_574049','20260731_103420_339055','20260731_104300_866808']
KL_REF=None; rows=[]
def pot_offset(d,g,tv,tt):
    tg=np.arange(d.t_wall.iloc[0],d.t_wall.iloc[-1],0.01); S=np.convolve(np.interp(tg,d.t_wall,d.FR),np.ones(7)/7,'same'); VR=hp(g.R_px.values,75)
    lags=np.arange(-4,6,0.02); X=np.array([hp(np.interp(tv+l,tg,S),75) for l in lags]); wl=[]
    for a in np.arange(0,tt[-1]-60,30):
        m=(tt>=a)&(tt<a+60)
        if m.sum()<400 or np.std(VR[m])<0.8: continue
        c=np.array([np.corrcoef(x[m],VR[m])[0,1] for x in X]); i=np.argmax(c)
        if c[i]>0.6: wl.append(lags[i])
    return (float(np.median(wl)),len(wl),float(np.subtract(*np.quantile(wl,[.75,.25])))) if len(wl)>=3 else (None,len(wl),None)
for sid in SES:
    d,_=load_sensor(sid); d=d[np.r_[True,np.diff(d.t_wall.values)>0]]; g=load_pots(sid); t0=video_t0(sid); tv=t0+g.t_ms.values/1000; tt=g.t_ms.values/1000
    po,pn,ps=pot_offset(d,g,tv,tt); ye=estimate(sid)
    lag=po if po is not None else (ye['offset_s'] if ye['n_windows']>=3 else None); src='pot' if po is not None else ('yaw' if lag is not None else 'none')
    r=dict(session=sid[:15],frames_ok=len(g),pot_offset=None if po is None else round(po,2),pot_windows=pn,yaw_offset=ye['offset_s'],yaw_windows=ye['n_windows'],used=src,lag=None if lag is None else round(lag,2))
    if lag is not None:
        f=lambda c,n=101: np.interp(tv+lag,d.t_wall,sm(d[c].values,n))
        ay=f('y_g'); spd=f('vehicle_speed_kmh'); FR=f('FR'); FLl=f('FL'); mv=spd>15
        hpf=lambda x:x-sm(x,151)
        kR=np.polyfit(hpf(g.R_px.values)[mv],hpf(f('FR',15))[mv],1)[0]; rR=np.corrcoef(hpf(g.R_px.values)[mv],hpf(f('FR',15))[mv])[0,1]
        if sid.startswith('20260731_031858'): KL_REF=np.polyfit(hpf(g.L_px.values)[mv],hpf(f('FL',15))[mv],1)[0]
        kL=KL_REF if KL_REF is not None else 1.261
        Lv=kL*sm(g.L_px.values,15); Rv=kR*sm(g.R_px.values,15)
        def sl(y,m):
            if m.sum()<300: return (None,None)
            k=np.polyfit(ay[m],y[m],1)[0]; return round(float(k),1),round(float(np.corrcoef(ay[m],y[m])[0,1]),2)
        r.update(kR_mm_px=round(float(kR),3),rR=round(float(rR),2),kL_mm_px=round(float(kL),3),roll_logged=sl(FLl-FR,mv),roll_videoL_logFR=sl(Lv-FR,mv),roll_video_both=sl(Lv-Rv,mv),moving_frames=int(mv.sum()))
        pd.DataFrame({'video_t':np.round(tt,3),'FL_video_mm':np.round(Lv,2),'FR_video_mm':np.round(Rv,2),'FR_log_mm':np.round(FR,2),'FL_log_mm':np.round(FLl,2),'y_g':np.round(ay,3),'speed_kmh':np.round(spd,1)}).to_csv(f'clean/{sid[:15]}_front_video.csv.gz',index=False)
    rows.append(r); print(r)
pd.DataFrame(rows).to_csv('out/fl731.csv',index=False)
