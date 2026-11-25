import numpy as np, pandas as pd, matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
plt.rcParams.update({'font.size':11,'axes.spines.top':False,'axes.spines.right':False,'axes.grid':True,'grid.color':'#e6e6e6','axes.edgecolor':'#888','figure.dpi':130,'axes.titlelocation':'left','axes.titleweight':'bold'})
BLUE='#2a6fdb'; GREY='#9a9a9a'
T=pd.read_csv('out/loss_breakdown.csv',dtype={'sid':str}); lab=[s[:4]+' '+s[5:7]+':'+s[7:9] for s in T.sid]
fig,ax=plt.subplots(1,2,figsize=(12,7.6),gridspec_kw=dict(width_ratios=[1.5,1]))
y=np.arange(len(T))[::-1]
ax[0].barh(y,T.single_pct,color=GREY,height=.7,label='periodic: 1 sample every 200 ms'); ax[0].barh(y,T.burst_pct,left=T.single_pct+.3,color=BLUE,height=.7,label='bursts: 3+ consecutive samples')
ax[0].set_yticks(y); ax[0].set_yticklabels(lab,fontsize=9); ax[0].set_xlabel('samples lost [% of expected]'); ax[0].grid(axis='y',visible=False)
ax[0].set_title('Periodic loss is a constant ~5%; burst loss varies 0-53%'); ax[0].legend(frameon=False,loc='lower right')
for yy,v in zip(y,T.loss_pct): ax[0].text(v+1.2,yy,f'{v:.0f}',va='center',fontsize=8,color='#444')
S=['7/31 03:45','7/31 09:57','7/31 10:18','8/11 08:28','8/13 00:13']; coast=[23.7,4.5,3.3,2.2,2.2]; powered=[60.3,53.9,41.4,12.4,19.9]
x=np.arange(len(S)); ax[1].bar(x-.2,coast,.38,color=GREY,label='coasting (< 2 A)'); ax[1].bar(x+.2,powered,.38,color=BLUE,label='powered (> 20 A)')
for xi,(c,p) in enumerate(zip(coast,powered)): ax[1].text(xi-.2,c+1,f'{c:.0f}',ha='center',fontsize=9); ax[1].text(xi+.2,p+1,f'{p:.0f}',ha='center',fontsize=9)
ax[1].set_xticks(x); ax[1].set_xticklabels(S,fontsize=9,rotation=20); ax[1].set_ylabel('burst loss [% of expected]'); ax[1].grid(axis='x',visible=False)
ax[1].set_title('At the same motor speed (>1000 rpm),\nburst loss follows traction current'); ax[1].legend(frameon=False)
plt.tight_layout(); plt.savefig('out/fig5_loss.png')
