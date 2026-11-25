import numpy as np, pandas as pd, glob, os
from common import ROOT, SUS
rows=[]; inter=[]
for f in sorted(glob.glob(ROOT+'/udp_logger/logs/sensor_log_2026072[8-9]*.csv.gz')+glob.glob(ROOT+'/udp_logger/logs/sensor_log_2026073*.csv.gz')+glob.glob(ROOT+'/udp_logger/logs/sensor_log_202608*.csv.gz')):
    sid=os.path.basename(f)[11:-7]; d=pd.read_csv(f,low_memory=False)
    if len(d)<3000: continue
    s=d.stm_ms.astype(float).values; ts=d.timestamp.values
    med=pd.Series(s).rolling(41,center=True,min_periods=5).median().values
    ok=np.abs(s-med)<3000; s=s[ok]; ts=ts[ok]
    ds=np.diff(s); newbatch=ts[1:]!=ts[:-1]
    v=(ds>0)&(ds<20000)
    k=np.rint(ds/10)                         # periods spanned
    single=v&(k==2); burst=v&(k>=3); pair=v&(ds==19)&np.r_[False,ds[:-1]==11]
    lost_single=single.sum(); lost_burst=(k[burst]-1).sum(); n=len(s); tot=n+lost_single+lost_burst
    # batch stats
    bsz=pd.Series(ts).groupby(pd.Series(ts)).size()
    i19=s[1:][v&(ds==19)]; 
    if len(i19)>50: inter+=list(np.diff(i19))
    rows.append(dict(sid=sid[4:13],rows=n,loss_pct=round(100*(tot-n)/tot,1),single_pct=round(100*lost_single/tot,1),burst_pct=round(100*lost_burst/tot,1),
        n19=int((v&(ds==19)).sum()),n11_then_19=int(pair.sum()),burst_events=int(burst.sum()),burst_at_batch_edge=round(100*newbatch[burst].mean(),0) if burst.sum() else np.nan,single_at_batch_edge=round(100*newbatch[single].mean(),0) if single.sum() else np.nan,edge_share_all=round(100*newbatch.mean(),1),burst_med_ms=float(np.median(ds[burst])) if burst.sum() else np.nan,burst_p95_ms=float(np.quantile(ds[burst],.95)) if burst.sum() else np.nan,batch_rows_med=float(bsz.median()),backward=int((ds<0).sum())))
T=pd.DataFrame(rows); pd.set_option('display.width',260); pd.set_option('display.max_columns',30); print(T.to_string(index=False)); T.to_csv('out/loss_breakdown.csv',index=False)
inter=np.array(inter); print('interval between 19ms gaps (stm ms): quantiles',np.quantile(inter,[.05,.25,.5,.75,.95]),' top values',pd.Series(inter).round(-1).value_counts().head(6).to_dict())
