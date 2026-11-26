"""Fit camera height, horizon row, static tilt and pitch/roll-per-g from cone tracks: every frame of one track must
project to the same ground point."""
import numpy as np, pandas as pd, json
F=538.0; CX=427.0
L=pd.read_pickle('out/cone_tracks_0813.pkl')
T=pd.concat([pd.DataFrame(t).assign(tr=k) for k,t in enumerate(L) if len(t)>=4],ignore_index=True)
def project(D,p):
    yh=p['yh']+(D.u.values-CX)*p['tilt']/CX+p['kp']*D.ax.values/9.81+(D.u.values-CX)*np.tan(np.radians(p['kr']*D.ay.values/9.81))
    dv=D.vb.values-yh; d=np.where(dv>3,F*p['h']/np.maximum(dv,3),np.nan); xl=-(D.u.values-CX)*d/F
    c,s=np.cos(D.psi.values),np.sin(D.psi.values)
    return D.x0.values+(d+p['a'])*c-xl*s, D.y0.values+(d+p['a'])*s+xl*c, d, xl
def score(p,D=T,q=0.5):
    X,Y,d,xl=project(D,p); ok=np.isfinite(d)&(d<20)
    G=pd.DataFrame({'tr':D.tr.values[ok],'X':X[ok],'Y':Y[ok]}); c=G.groupby('tr')[['X','Y']].transform('mean')
    e=np.hypot(G.X-c.X,G.Y-c.Y); return float(G.assign(e=e).groupby('tr').e.mean().quantile(q))
if __name__=='__main__':
    print('tracks',T.tr.nunique(),'detections',len(T))
    p=dict(h=1.2,yh=252.0,tilt=0.0,kp=0.0,kr=0.0,a=0.0)
    grid=dict(h=np.arange(0.6,2.01,0.02),yh=np.arange(236,270.1,0.5),tilt=np.arange(-30,30.1,1.0),kp=np.arange(-40,40.1,2.0),kr=np.arange(-4,4.01,0.25),a=np.arange(-2,3.01,0.25))
    for it in range(4):
        for k,vals in grid.items():
            sc=[score({**p,k:x}) for x in vals]; p[k]=float(vals[int(np.argmin(sc))])
        print('iter',it,{k:round(v,3) for k,v in p.items()},'median within-track spread %.3f m'%score(p))
    # sensitivity: how sharp is the optimum in h
    for hh in (p['h']*0.9,p['h'],p['h']*1.1): print('  h=%.2f -> %.3f m'%(hh,score({**p,'h':hh})))
    json.dump(p,open('out/cone_cam_params.json','w'))
