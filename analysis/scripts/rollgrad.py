import numpy as np, pandas as pd
from common import *
MAP_STD=dict(fL='FL',fR='FR',rL='RL',rR='RR'); MAP_SWAP=dict(fL='RR',fR='FR',rL='RL',rR='FL')
SESS=[('20260731_031858_070588',MAP_STD,'7/31 03:18')]+[(s,MAP_STD,'8/11 AM') for s in ['20260811_022758_150378','20260811_025402_438882','20260811_071828_103551','20260811_073314_333336']]+[(s,MAP_SWAP,'8/11 PM+ (FL<->RR)') for s in ['20260811_082807_765988','20260811_084517_257245','20260813_001309_427966']]
rows=[]
for sid,mp,tag in SESS:
    d,_=load_sensor(sid)
    d=d[np.r_[True,np.diff(d.t_wall.values)>0]]
    tg=np.arange(d.t_wall.iloc[0],d.t_wall.iloc[-1],0.01)
    U=pd.DataFrame({c:np.interp(tg,d.t_wall,d[c]) for c in SUS+['x_g','y_g','vehicle_speed_kmh']})
    gapok=np.interp(tg,d.t_wall.values[1:],np.diff(d.t_wall.values))<0.1
    U=U.rolling(100,center=True,min_periods=50).mean()       # 1 s quasi-steady
    m=gapok&(U.vehicle_speed_kmh>15).values&U.y_g.notna().values
    U=U[m].iloc[::25]                                          # decimate to 4 Hz
    def fit(y,x):
        A=np.c_[x,np.ones(len(x))]; k,b=np.linalg.lstsq(A,y,rcond=None)[0]; r=np.corrcoef(x,y)[0,1]; return k,r
    kf,rf=fit((U[mp['fL']]-U[mp['fR']]).values,U.y_g.values); kr,rr=fit((U[mp['rL']]-U[mp['rR']]).values,U.y_g.values)
    kp,rp=fit(((U[mp['fL']]+U[mp['fR']])/2-(U[mp['rL']]+U[mp['rR']])/2).values,U.x_g.values)
    # same thing with un-swapped labels for comparison
    kf0,rf0=fit((U.FL-U.FR).values,U.y_g.values); kr0,rr0=fit((U.RL-U.RR).values,U.y_g.values)
    rows.append(dict(sid=sid[4:15],tag=tag,n=len(U),ay_p95=round(U.y_g.abs().quantile(.95),2),front_mm_per_g=round(kf,1),r_f=round(rf,2),rear_mm_per_g=round(kr,1),r_r=round(rr,2),front_share=round(kf/(kf+kr),2),pitch_mm_per_g=round(kp,1),r_p=round(rp,2),asLabeled_front=round(kf0,1),asLabeled_r_f=round(rf0,2),asLabeled_rear=round(kr0,1),asLabeled_r_r=round(rr0,2)))
df=pd.DataFrame(rows); pd.set_option('display.width',260); pd.set_option('display.max_columns',30); print(df.to_string(index=False)); df.to_csv('out/roll_gradient.csv',index=False)
