"""Steering response. (1) steering -> lateral g delay from the sensor log alone (same STM sample, so no video timing involved).
(2) yaw-rate gain from the camera (calibrated on 8/13 lap closure, 0.001858 rad/px), fit r/delta_sw = v / (Leff (1 + K v^2))."""
import numpy as np, pandas as pd, json, glob, os
from common import *
sm=lambda x,n: pd.Series(x).rolling(n,center=True,min_periods=1).mean().values
md=lambda x,n=5: pd.Series(x).rolling(n,center=True,min_periods=1).median().values
H={h['session']:h['video_to_log_lag_s'] for h in json.load(open('out/offset_hints.json'))}
K_RAD_PX=0.001858
lag_rows=[]
for f in sorted(glob.glob('clean/2026*_??????.csv.gz')):
    s=os.path.basename(f)[:15]; o=pd.read_csv(f)
    if 'steering_angle' not in o: continue
    tg=np.arange(0,o.t.iloc[-1],0.01); ok=np.interp(tg,o.t.values[1:],np.diff(o.t.values))<0.045
    st=np.interp(tg,o.t,md(o.steering_angle.values)); ay=np.interp(tg,o.t,md(o.y_g.values)); v=np.interp(tg,o.t,md(o.vehicle_speed_kmh.values))
    bp=lambda x: sm(x,8)-sm(x,300); S=bp(st); A=bp(ay); m=(v>20)&ok
    if m.sum()<3000 or np.std(st[m])<5: continue
    best=max(((np.corrcoef(S[m][:len(S[m])-L] if L else S[m],A[m][L:] if L else A[m])[0,1],L) for L in range(0,41)),key=lambda z:abs(z[0]))
    # windowed for spread
    wl=[]
    idx=np.where(m)[0]
    for a in range(0,len(idx)-3000,1500):
        ii=idx[a:a+3000]; 
        if np.std(S[ii])<3: continue
        c=[abs(np.corrcoef(S[ii[:len(ii)-L]],A[ii[L:]])[0,1]) for L in range(0,41)]; 
        if max(c)>0.7: wl.append(int(np.argmax(c))*10)
    lag_rows.append(dict(session=s,moving_s=int(m.sum()/100),lag_ms=best[1]*10,r=round(best[0],2),window_lags_ms=(int(np.median(wl)),int(np.quantile(wl,.25)),int(np.quantile(wl,.75)),len(wl)) if len(wl)>=3 else None,loss_pct=round(100*(1-ok.mean()),0)))
L=pd.DataFrame(lag_rows); pd.set_option('display.width',220); print(L.to_string(index=False)); L.to_csv('out/steer_lag.csv',index=False)
# ---- yaw gain from camera
pts=[]
for sid in ['20260811_022758_150378','20260811_073314_333336','20260811_084517_257245','20260813_001309_427966']:
    d,_=load_sensor(sid); d=d[np.r_[True,np.diff(d.t_wall.values)>0]]
    y=pd.read_csv(f'out/yaw_{sid[4:15]}.csv'); tv=video_t0(sid)+y.t_ms.values/1000+H[sid]
    dx=pd.Series(np.where((np.abs(y.dx)>80)|(y.resp<0.03),np.nan,y.dx)).interpolate(limit=8).fillna(0).values
    r=sm(dx*K_RAD_PX*15.0,8)
    f=lambda c,n=8: np.interp(tv,d.t_wall,sm(md(d[c].values),n*7))
    st=np.radians(f('steering_angle')); v=f('vehicle_speed_kmh')/3.6; ay=f('y_g')*9.81
    sgn=np.sign(np.corrcoef(r[v>5],st[v>5])[0,1]); r=r*sgn
    stdy=(v>6)&(np.abs(np.gradient(np.degrees(st),1/15))<25)&(np.abs(np.degrees(st))>8)&(np.abs(np.gradient(sm(v,15),1/15))<1.5)
    pts.append(pd.DataFrame({'s':sid[:15],'v':v[stdy],'G':r[stdy]/st[stdy],'ayv_over_r':(ay/np.maximum(v,1))[stdy]/np.where(np.abs(r[stdy])>0.05,r[stdy],np.nan)}))
P=pd.concat(pts); P=P[(P.G>0)&np.isfinite(P.G)]
print('steady points',len(P),'by session',P.s.value_counts().to_dict())
b=pd.cut(P.v,[6,8,10,12,14,17,21,26]); T=P.groupby(b,observed=True).agg(v=('v','median'),G=('G','median'),n=('G','size'),ratio=('ayv_over_r','median')); T['Leff_m']=T.v/T.G; print(T.round(3).to_string())
X=np.c_[np.ones(len(T)),T.v**2]; co=np.linalg.lstsq(X,T.Leff_m.values,rcond=None)[0]; print('Leff(v) = %.2f + %.4f v^2  -> Leff0 %.2f m, K = %.4f s^2/m^2, characteristic speed %s'%(co[0],co[1],co[0],co[1]/co[0],('%.1f m/s'%(1/np.sqrt(co[1]/co[0]))) if co[1]>0 else 'n/a (oversteer)'))
# per-session robustness
for s,q in P.groupby('s'):
    q=q.assign(L=q.v/q.G); bb=pd.cut(q.v,[6,10,14,26]); print(' ',s,q.groupby(bb,observed=True).L.median().round(2).to_dict(),'n',len(q))
# bootstrap on sessions
rng=np.random.default_rng(1); ss=P.s.unique(); ks=[]
for _ in range(300):
    Q=pd.concat([P[P.s==x] for x in rng.choice(ss,len(ss))]); TT=Q.groupby(pd.cut(Q.v,[6,8,10,12,14,17,21,26]),observed=True).agg(v=('v','median'),G=('G','median')); TT['L']=TT.v/TT.G
    c=np.linalg.lstsq(np.c_[np.ones(len(TT)),TT.v**2],TT.L.values,rcond=None)[0]; ks.append((c[0],c[1]/c[0]))
ks=np.array(ks); print('bootstrap Leff0 %.2f..%.2f, K %.4f..%.4f'%(*np.quantile(ks[:,0],[.025,.975]),*np.quantile(ks[:,1],[.025,.975])))
T.to_csv('out/yaw_gain.csv')
