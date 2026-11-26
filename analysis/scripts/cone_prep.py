"""Step 1 of the cone map (course B, 8/13): per-lap closed poses from logged speed + camera yaw rate, cone detections
put on those poses, and frame-to-frame cone tracks used to calibrate the camera (cone_calib.py).
Original description:
Flat-ground model: a cone base seen at row vb lies d = f*h/(vb - y_h) ahead and -(u-cx)*d/f to the left.
Camera height h, horizon row y_h, pitch-per-g and roll-per-g are unknown; they are fitted so that the same cone,
seen from two places 1/3 s apart, lands on the same ground point (no cone survey or GPS involved)."""
import numpy as np, pandas as pd, json, os, sys
from common import load_sensor, video_t0
SID='20260813_001309_427966'; S=SID[:15]; LAG=1.67; F=538.0; CX=427.0; FPS=15.0; DAY=-1
sm=lambda x,n: pd.Series(x).rolling(n,center=True,min_periods=1).mean().values
md=lambda x,n=5: pd.Series(x).rolling(n,center=True,min_periods=1).median().values
def prep():
    if not os.path.exists('out/tw0_0813.json'):
        d,_=load_sensor(SID); d=d[np.r_[True,np.diff(d.t_wall.values)>0]]; json.dump(dict(tw0=float(d.t_wall.iloc[0])),open('out/tw0_0813.json','w'))
    tw0=json.load(open('out/tw0_0813.json'))['tw0']
    o=pd.read_csv(f'clean/{S}.csv.gz'); tg=np.arange(0,o.t.iloc[-1],0.01); g=lambda c,n=20: sm(np.interp(tg,o.t,md(o[c].values)),n)
    v=g('vehicle_speed_kmh').clip(min=0)/3.6
    ay=g('y_g')*9.81; ay-=np.median(ay[v<0.5]); ay*=DAY
    ax=g('x_g',30)*9.81; ax*=np.sign(np.corrcoef(ax[v>3],np.gradient(sm(v,100),0.01)[v>3])[0,1])
    cy=pd.read_csv(f'clean/{S}_camyaw.csv.gz'); r=np.radians(np.interp(tg,cy.t,cy.yaw_rate_cam_dps))
    r*=np.sign(np.corrcoef(r[v>5],(ay/np.maximum(v,2.5))[v>5])[0,1])      # +left, same convention as ay
    J=json.load(open(f'out/laps_{S}.json')); sd=np.cumsum(v)*0.01; sg=np.arange(0,sd[-1],J['DS']); ti=np.interp(sg,sd,tg)
    laps=[(float(ti[a]),float(ti[b])) for a,b in J['laps']]
    # per-lap pose, closed: heading scaled to exactly one turn, position error spread linearly
    pose=np.full((len(tg),4),np.nan)   # x, y, psi, lap
    for L,(t0,t1) in enumerate(laps):
        i0,i1=np.searchsorted(tg,[t0,t1]); rr=r[i0:i1]; psi=np.cumsum(rr)*0.01; psi*=2*np.pi*np.sign(psi[-1])/psi[-1]
        x=np.cumsum(v[i0:i1]*np.cos(psi))*0.01; y=np.cumsum(v[i0:i1]*np.sin(psi))*0.01; w=np.linspace(0,1,len(x)); x-=w*x[-1]; y-=w*y[-1]
        pose[i0:i1]=np.c_[x,y,psi,np.full(len(x),L)]
    D=pd.read_csv('out/cones_0813.csv'); D=D[(D.vb>=262)&(D.area>=5)].copy()
    # drop the upper half of a cone split by its white band
    D=D.sort_values(['frame','color','vb']).reset_index(drop=True); drop=np.zeros(len(D),bool)
    for (fr,col),G in D.groupby(['frame','color']):
        if len(G)<2: continue
        idx=G.index.values; u=G.u.values; vb=G.vb.values; h=G.h.values; w=G.w.values
        for j in range(len(G)):          # j lower (larger vb)
            for k in range(len(G)):
                if vb[k]<vb[j] and abs(u[k]-u[j])<w[j]/2+3 and vb[k]>=vb[j]-1.7*h[j]-3: drop[idx[k]]=True
    D=D[~drop].copy()
    tc=video_t0(SID)+D.frame.values/FPS+LAG-tw0; D['t']=tc; i=np.clip(np.searchsorted(tg,tc),0,len(tg)-1)
    D['x0'],D['y0'],D['psi'],D['lap']=pose[i].T; D['ax']=ax[i]; D['ay']=ay[i]; D['v']=v[i]
    D=D[np.isfinite(D.lap)].reset_index(drop=True)
    np.savez('out/cone_prep_0813.npz',tg=tg,pose=pose,v=v,ax=ax,ay=ay,laps=np.array(laps))
    D.to_csv('out/cone_dets_0813.csv',index=False); print('detections in timed laps',len(D),'| dropped split halves',int(drop.sum()),'| laps',len(laps))
    return D

def tracks(D):
    """greedy frame-to-frame tracks: same colour, small horizontal step, base row moving down the image"""
    D=D.sort_values(['frame']).reset_index(drop=True); byf={f:g for f,g in D.groupby('frame')}; done=[]; active=[]
    for f in sorted(byf):
        G=byf[f]; used=set(); new=[]
        for tr in active:
            last=tr[-1]
            if last['frame']!=f-1: done.append(tr); continue
            c=G[(G.color==last['color'])&(np.abs(G.u-last['u'])<8+0.03*abs(last['u']-CX))&(G.vb>=last['vb'])&(G.vb<=last['vb']+14)]
            c=c[~c.index.isin(used)]
            if len(c): j=c.index[np.argmin(np.abs(c.u-last['u']))]; used.add(j); tr.append(G.loc[j].to_dict()); new.append(tr)
            else: done.append(tr)
        new+=[[G.loc[j].to_dict()] for j in G.index if j not in used]; active=new
    L=[t for t in done+active if len(t)>=6]; pd.to_pickle(L,'out/cone_tracks_0813.pkl'); print('tracks of 6+ frames',len(L)); return L
if __name__=='__main__':
    tracks(prep())
