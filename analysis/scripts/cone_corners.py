"""Per-corner geometry from the cone map, best lap: track width and how far the car's path (camera axis) ran
from the inner-side cone line at the apex."""
import numpy as np, pandas as pd, json
C=pd.read_csv('out/cone_map_0813.csv'); T=pd.read_csv('out/cone_traj_0813.csv'); M=json.load(open('out/cone_slam_meta.json'))
K=pd.read_csv('out/corners_courses.csv'); K=K[K.course=='B']
G=T[T.lap==M['ref']].reset_index(drop=True); x,y=G.xc.values,G.yc.values; s=np.r_[0,np.cumsum(np.hypot(np.diff(x),np.diff(y)))]
hx,hy=np.gradient(x),np.gradient(y); n=np.hypot(hx,hy); hx/=n; hy/=n
def lateral(col):
    q=C[C.color==col][['x','y']].values; dx=q[None,:,0]-x[:,None]; dy=q[None,:,1]-y[:,None]
    lat=-hy[:,None]*dx+hx[:,None]*dy; lon=hx[:,None]*dx+hy[:,None]*dy
    cand=np.where(np.abs(lon)<3.0,np.abs(lat),np.inf); j=cand.argmin(1); return lat[np.arange(len(j)),j],cand[np.arange(len(j)),j]
Ly,dy_=lateral('yellow'); Lb,db=lateral('blue')
rows=[]
for _,k in K.iterrows():
    j0=int(np.searchsorted(s,k.apex_m*s[-1]/898.0)); w=slice(max(0,j0-8),min(len(G),j0+9))   # +-~8 m around the apex
    inner='yellow' if k.direction=='left' else 'blue'   # yellow cones line the left side on this course
    di=(dy_ if inner=='yellow' else db)[w]; do=(db if inner=='yellow' else dy_)[w]
    ok=np.isfinite(di)&(di<8); ok2=ok&np.isfinite(do)&(do<8)
    rows.append(dict(corner=int(k.corner),direction=k.direction,apex_m=int(k.apex_m),v_min_best=k.v_min_best_lap,
        to_inner_cones_m=round(float(np.min(di[ok])),2) if ok.any() else np.nan,
        track_width_m=round(float(np.median(di[ok2]+do[ok2])),2) if ok2.any() else np.nan))
R=pd.DataFrame(rows); R.to_csv('out/cone_corners_0813.csv',index=False); print(R.to_string(index=False))
both=np.isfinite(dy_)&np.isfinite(db)&(dy_<7)&(db<7)&(np.sign(Ly)!=np.sign(Lb)); print('whole lap: width median %.2f m (p10 %.2f, p90 %.2f) over %.0f%% of the lap; yellow on the left %.0f%%, blue on the right %.0f%%'%(np.median((dy_+db)[both]),np.quantile((dy_+db)[both],.1),np.quantile((dy_+db)[both],.9),100*both.mean(),100*(Ly[dy_<7]>0).mean(),100*(Lb[db<7]<0).mean()))
