def winrate(d):
    if not d:
        return 0.0
    return sum(d)/len(d)

def volatilidade(d):
    if len(d) < 10:
        return 0.0
    trocas = sum(1 for i in range(1,len(d)) if d[i]!=d[i-1])
    return trocas/(len(d)-1)

def streak_loss(d):
    s=0; mx=0
    for x in d:
        if x==0:
            s+=1; mx=max(mx,s)
        else:
            s=0
    return mx

def risco_conf(short_wr, long_wr, vol, max_reds):
    base = 0.6*short_wr + 0.3*long_wr + 0.1*(1.0-vol)
    pena = 0.0
    if max_reds>=3:
        pena += 0.05*(max_reds-2)
    if vol>0.6:
        pena += 0.05
    return max(0.0, min(1.0, base-pena))
