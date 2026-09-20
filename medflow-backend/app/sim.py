"""
MEDFLOW simulation engine.

A bit-exact Python port of the browser engine.
"""
from __future__ import annotations

import csv
import io
import math
from typing import Any, Callable, Dict, List, Optional, Tuple

MASK = 0xFFFFFFFF

def _u32(x: int) -> int: return x & MASK

def _i32(x: int) -> int:
    x &= MASK
    return x - 0x100000000 if x >= 0x80000000 else x

def _imul(a: int, b: int) -> int: return _i32((_i32(a) * _i32(b)) & MASK)

def mulberry32(seed: int) -> Callable[[], float]:
    a = _i32(seed)
    def rnd() -> float:
        nonlocal a
        a = _i32(a + 0x6D2B79F5)
        t = _imul(a ^ (_u32(a) >> 15), 1 | a)
        t = _i32(_i32(t + _imul(t ^ (_u32(t) >> 7), 61 | t)) ^ t)
        return _u32(t ^ (_u32(t) >> 14)) / 4294967296.0
    return rnd

def gauss(rng: Callable[[], float]) -> float:
    u = v = 0.0
    while u == 0.0: u = rng()
    while v == 0.0: v = rng()
    return math.sqrt(-2.0 * math.log(u)) * math.cos(2.0 * math.pi * v)

def jround(x: float) -> int: return math.floor(x + 0.5)

RESOURCES: List[Tuple[str, str, int]] = [("icu", "ICU beds", 6), ("ward", "Ward beds", 22), ("or", "Operating rooms", 3), ("doctor", "Doctors on shift", 9), ("nurse", "Nurses on shift", 20), ("amb", "Ambulances", 4)]
RES_KEYS = [r[0] for r in RESOURCES]
RES_LABELS = {k: label for k, label, _ in RESOURCES}
DEFAULT_CAPACITY = {k: c for k, _, c in RESOURCES}
HOURLY = [0.42,0.36,0.30,0.28,0.32,0.42,0.62,0.88,1.12,1.32,1.38,1.30,1.22,1.16,1.16,1.22,1.32,1.48,1.52,1.40,1.20,0.96,0.72,0.52]
BASE_LAMBDA = 0.185
HORIZON = 1440
DAY_START_MIN = 360
TARGET_WAIT = {1: 5, 2: 15, 3: 35, 4: 60, 5: 85}
ACUITY = {1: 1000, 2: 60, 3: 20, 4: 8, 5: 3}
DEADLINE = {1: 15, 2: 45, 3: 100, 4: 120, 5: 95}
MEAN_DUR = {1: 200, 2: 150, 3: 95, 4: 50, 5: 25}
LWBS_LIMIT = {4: 150, 5: 110}
POLICIES = ("fifo", "triage", "medflow")
POLICY_LABELS = {"fifo": "First come, first served", "triage": "Strict triage", "medflow": "MEDFLOW"}
FIRST = ["Asha", "Ravi", "Meera", "Nikhil", "Priya", "Arjun", "Fatima", "Dev", "Sneha", "Imran", "Kavya", "Rohit", "Anita", "Yusuf", "Divya", "Karan", "Leela", "Sam", "Tara", "Vikram", "Noor", "Ishaan", "Rhea", "Manav", "Zoya", "Aditya"]
LAST = ["Rao", "Nair", "Shah", "Iyer", "Khan", "Bose", "Menon", "Patel", "Reddy", "Das", "Gill", "Joshi", "Verma", "Pillai", "Sethi", "Mathew"]
RES_DISPLAY = {"icu": "ICU", "ward": "bed", "or": "theatre", "doctor": "doctor", "nurse": "nurse"}


class Patient:
    __slots__ = ("id", "name", "esi", "esi0", "need", "dur", "arrive", "wait", "risk", "holds", "phase", "stepdown", "by_amb", "blocked", "served_at", "state", "score", "eta", "amb_free", "treat_end", "step_end", "ward_idx", "boarded")
    def __init__(self) -> None:
        self.id=0; self.name=""; self.esi=3; self.esi0=3; self.need={}; self.dur=0; self.arrive=0; self.wait=0; self.risk=0.0; self.holds=[]; self.phase="waiting"; self.stepdown=0; self.by_amb=False; self.blocked=0; self.served_at=None; self.state=""; self.score=None; self.eta=0; self.amb_free=0; self.treat_end=0; self.step_end=0; self.ward_idx=False; self.boarded=0


class Sim:
    def __init__(self, seed=4207, policy="medflow", load_pct=100, capacity=None, horizon=HORIZON):
        if policy not in POLICIES: raise ValueError(f"unknown policy {policy!r}, expected one of {POLICIES}")
        self.seed=int(seed); self.policy=policy; self.load=load_pct/100.0; self.horizon=int(horizon); self.capacity=dict(DEFAULT_CAPACITY); self.capacity.update({k:max(0,int(v)) for k,v in (capacity or {}).items() if k in self.capacity})
        self.rng=mulberry32(self.seed); self.rng_b=mulberry32((self.seed*7919+13)&MASK); self.t=0; self.next_id=1
        self.units={k:[None]*self.capacity[k] for k in RES_KEYS}; self.queue=[]; self.inbound=[]; self.amb_calls=[]; self.active=[]; self.log=[]; self.history=[]; self.records=[]; self.admission_records=[]
        self.stats={"arrived":0,"served":0,"lwbs":0,"deterioration":0,"escalations":0,"boarded":0,"on_time":0,"wait_sum":0.0,"wait_n":0,"waits":[],"wait_by_esi":{1:[],2:[],3:[],4:[],5:[]},"util":{k:0.0 for k in RES_KEYS},"util_n":0,"icu_blocked_min":0,"amb_delay_min":0,"conflicts":0}

    @staticmethod
    def clock(t):
        m=(t+DAY_START_MIN)%1440; return f"{m//60:02d}:{m%60:02d}"
    def add_log(self,msg,cls=""): self.log.append({"t":self.t,"clock":self.clock(self.t),"msg":msg,"kind":cls}); self.log=self.log[-400:]
    def free(self,k): return self.units[k].count(None)
    def lam(self): return BASE_LAMBDA*HOURLY[int(((self.t/60)+6)%24)]*self.load
    def resolve(self,p):
        n=dict(p.need); boarded=0
        if n.get("icu") and self.free("icu")<n["icu"]:
            boarded=n["icu"]-self.free("icu"); n.pop("icu",None); n["ward"]=n.get("ward",0)+boarded
        return n,boarded
    def can_take(self,need): return all(self.free(k)>=v for k,v in need.items())
    def take(self,p,need,until):
        for k in RES_KEYS:
            for _ in range(need.get(k,0)):
                i=self.units[k].index(None); self.units[k][i]=p; p.holds.append({"type":k,"idx":i,"until":until.get(k)})
    def release(self,p,rtype):
        p.holds=[h for h in p.holds if not (h["type"]==rtype and not self.units[h["type"]].__setitem__(h["idx"],None))]
    def release_all(self,p):
        for h in p.holds: self.units[h["type"]][h["idx"]]=None
        p.holds=[]
    def make_patient(self):
        r=self.rng(); esi=1 if r<.05 else 2 if r<.20 else 3 if r<.55 else 4 if r<.85 else 5; need={}
        if esi==1: need={"icu":1,"doctor":1,"nurse":2}
        elif esi==2: need={"icu" if self.rng()<.40 else "ward":1,"doctor":1,"nurse":1}
        elif esi in (3,4): need={"ward":1,"doctor":1,"nurse":1}
        else: need={"doctor":1,"nurse":1}
        if self.rng()<{1:.45,2:.25,3:.08,4:.02,5:0}[esi]: need["or"]=1
        p=Patient(); p.id=self.next_id; self.next_id+=1; p.name=FIRST[int(self.rng()*len(FIRST))]+" "+LAST[int(self.rng()*len(LAST))][0]+"."; p.esi=esi; p.esi0=esi; p.need=need; p.dur=max(12,jround(MEAN_DUR[esi]*math.exp(.34*gauss(self.rng)))); p.arrive=self.t; p.stepdown=60+jround(self.rng()*120) if need.get("icu") else 0; return p
    def surge(self,n=None):
        n=n or 5+int(self.rng()*7)
        for _ in range(n):
            p=self.make_patient(); p.esi=max(1,p.esi-2) if p.esi>2 and self.rng()<.6 else p.esi; p.by_amb=True; self.queue.append(p); self.stats["arrived"]+=1
        self.add_log(f"Mass casualty: {n} patients incoming","alert")
    def score(self,p):
        if self.policy=="fifo": return 100000-p.arrive
        if self.policy=="triage": return ACUITY[p.esi]*1000-p.arrive*.001
        if p.esi==1: return 1e9+p.wait
        if p.esi==2: return 1e8+p.wait
        if p.wait>DEADLINE[p.esi]: return 1e6+(p.wait-DEADLINE[p.esi])*10+ACUITY[p.esi]
        return ACUITY[p.esi]*(1+min(3,p.wait/TARGET_WAIT[p.esi]))*(1+.3*p.risk)

    def step(self):
        t=self.t
        if self.rng()<self.lam():
            p=self.make_patient(); amb={1:.72,2:.5,3:.22,4:.07,5:.02}[p.esi]
            if self.rng()<amb: p.by_amb=True; self.amb_calls.append({"p":p,"called":t})
            else: self.queue.append(p); self.stats["arrived"]+=1
        if self.rng()<.00055*self.load: self.surge()
        for i in range(len(self.amb_calls)-1,-1,-1):
            c=self.amb_calls[i]; p=c["p"]
            if self.free("amb")>0:
                idx=self.units["amb"].index(None); self.units["amb"][idx]=p; p.amb_free=t+14+jround(self.rng()*16)+12+jround(self.rng()*14); p.eta=t+14+jround(self.rng()*16); self.inbound.append(p); self.amb_calls.pop(i)
            else: self.stats["amb_delay_min"]+=1; p.risk+=.02*(6-p.esi)
        for i in range(len(self.inbound)-1,-1,-1):
            p=self.inbound[i]
            if t>=p.eta: p.arrive=t; self.queue.append(p); self.stats["arrived"]+=1; self.inbound.pop(i)
        for i,p in enumerate(self.units["amb"]):
            if p is not None and p.amb_free<=t: self.units["amb"][i]=None
        for i in range(len(self.queue)-1,-1,-1):
            p=self.queue[i]; p.wait=t-p.arrive; rate={1:.055,2:.03,3:.012,4:.004,5:.001}[p.esi]; p.risk+=rate*(1+p.wait/90)
            if p.esi>1 and self.rng_b()<rate*.055*(p.wait/30): p.esi-=1; self.stats["escalations"]+=1
            if p.esi<=2 and p.wait>{1:20,2:55}[p.esi] and self.rng_b()<.006: self.stats["deterioration"]+=1; p.risk+=4
            lim=LWBS_LIMIT.get(p.esi)
            if lim and p.wait>lim and self.rng_b()<.02:
                self.stats["lwbs"]+=1; self.queue.pop(i); self.stats["wait_sum"]+=p.wait; self.stats["wait_n"]+=1; self.stats["waits"].append(p.wait); self.stats["wait_by_esi"][p.esi].append(p.wait)
        for i in range(len(self.active)-1,-1,-1):
            p=self.active[i]; keep=[]
            for h in p.holds:
                if h["until"] is not None and h["until"]<=t and h["type"] not in ("icu","ward"): self.units[h["type"]][h["idx"]]=None
                else: keep.append(h)
            p.holds=keep
            if p.phase=="treating" and t>=p.treat_end:
                if any(h["type"]=="icu" for h in p.holds) and p.stepdown>0: p.phase="stepdown"
                else: self.release_all(p); self.active.pop(i); self.records.append(self._record(p,"discharged",t))
            elif p.phase=="stepdown":
                if not p.ward_idx:
                    if self.free("ward")>0:
                        idx=self.units["ward"].index(None); self.units["ward"][idx]=p; p.ward_idx=True; p.holds.append({"type":"ward","idx":idx,"until":None}); self.release(p,"icu"); p.step_end=t+p.stepdown
                    else: p.blocked+=1; self.stats["icu_blocked_min"]+=1
                elif t>=p.step_end: self.release_all(p); self.active.pop(i); self.records.append(self._record(p,"discharged",t))
        self.allocate()
        for k in RES_KEYS: self.stats["util"][k]+=(self.capacity[k]-self.free(k))/self.capacity[k] if self.capacity[k] else 0
        self.stats["util_n"]+=1
        if t%10==0: self.history.append({"t":t,"queue":len(self.queue),"avg_wait":sum((p.wait for p in self.queue),0)/len(self.queue) if self.queue else 0})
        self.t+=1

    def allocate(self):
        t=self.t
        for p in self.queue: p.wait=t-p.arrive; p.score=self.score(p); p.state=""
        self.queue.sort(key=lambda q:-q.score)
        for p in list(self.queue):
            eff,boarded=self.resolve(p)
            if not self.can_take(eff): p.state="held"; continue
            until={"ward":None,"icu":None}
            if eff.get("doctor"): until["doctor"]=t+max(15,jround(p.dur*.45))
            if eff.get("or"): until["or"]=t+min(p.dur,35+jround(p.dur*.35))
            if eff.get("nurse"): until["nurse"]=t+max(15,jround(p.dur*.75))
            if not eff.get("ward") and not eff.get("icu"): until["nurse"]=t+p.dur
            self.take(p,eff,until); p.phase="treating"; p.treat_end=t+p.dur; p.served_at=t; p.wait=t-p.arrive; p.boarded=boarded; self.active.append(p); self.queue.remove(p)
            self.stats["served"]+=1; self.stats["on_time"]+=p.wait<=TARGET_WAIT[p.esi]; self.stats["wait_sum"]+=p.wait; self.stats["wait_n"]+=1; self.stats["waits"].append(p.wait); self.stats["wait_by_esi"][p.esi].append(p.wait)
            self.admission_records.append({"patient_id":p.id,"name":p.name,"triage_level":p.esi,"admitted_clock":self.clock(t),"wait_min":p.wait})

    def _record(self,p,outcome,discharged=None):
        return {"patient_id":p.id,"name":p.name,"triage_level":p.esi,"triage_level_on_arrival":p.esi0,"arrived_min":p.arrive,"arrived_clock":self.clock(p.arrive),"wait_min":p.wait,"treatment_min":p.dur if outcome=="discharged" else 0,"discharged_min":discharged,"resources":"|".join(f"{k}x{v}" for k,v in p.need.items()),"by_ambulance":p.by_amb,"boarded_in_ward":bool(p.boarded),"icu_blocked_min":p.blocked,"outcome":outcome,"seen_within_target":outcome=="discharged" and p.wait<=TARGET_WAIT[p.esi]}
    def p95(self):
        a=sorted(self.stats["waits"]); return a[min(len(a)-1,int(len(a)*.95))] if a else 0
    def avg_wait_esi(self,e):
        a=self.stats["wait_by_esi"][e]; return sum(a)/len(a) if a else None
    def util_pct(self,k): return self.stats["util"][k]/self.stats["util_n"]*100 if self.stats["util_n"] else 0
    def compliance(self):
        n=self.stats["served"]+self.stats["lwbs"]; return self.stats["on_time"]/n*100 if n else 0
    def avg_wait(self): return self.stats["wait_sum"]/self.stats["wait_n"] if self.stats["wait_n"] else 0
    def metrics(self): return {"arrived":self.stats["arrived"],"seen":self.stats["served"],"seen_on_time_pct":round(self.compliance(),1),"avg_wait_min":round(self.avg_wait(),1),"p95_wait_min":self.p95(),"wait_by_level":{str(e):round(self.avg_wait_esi(e),1) if self.avg_wait_esi(e) is not None else None for e in range(1,6)},"adverse_events":self.stats["deterioration"],"deteriorated":self.stats["escalations"],"walked_out":self.stats["lwbs"],"boarded_in_ward":self.stats["boarded"],"icu_blocked_min":self.stats["icu_blocked_min"],"ambulance_delay_min":self.stats["amb_delay_min"],"utilisation_pct":{k:round(self.util_pct(k),1) for k in RES_KEYS}}
    def snapshot(self,queue_limit=25,log_limit=40): return {"run":{"seed":self.seed,"policy":self.policy,"load_pct":round(self.load*100),"capacity":self.capacity,"horizon_min":self.horizon},"t":self.t,"clock":self.clock(self.t),"day":(self.t+DAY_START_MIN)//1440+1,"finished":self.t>=self.horizon,"resources":[{"key":k,"label":RES_LABELS[k],"capacity":self.capacity[k],"in_use":self.capacity[k]-self.free(k),"avg_utilisation_pct":round(self.util_pct(k),1),"units":[None if u is None else {"patient_id":u.id,"triage_level":u.esi,"blocked":k=="icu" and u.phase=="stepdown"} for u in self.units[k]]} for k in RES_KEYS],"waiting":[{"patient_id":p.id,"name":p.name,"triage_level":p.esi,"escalated":p.esi<p.esi0,"by_ambulance":p.by_amb,"wait_min":p.wait,"needs":[(f"{v} " if v>1 else "")+RES_DISPLAY.get(k,k) for k,v in p.need.items()],"priority":round(p.score,2) if p.score is not None else None,"state":p.state} for p in self.queue[:queue_limit]],"counts":{"waiting":len(self.queue),"in_care":len(self.active),"en_route":len(self.inbound),"awaiting_ambulance":len(self.amb_calls)},"metrics":self.metrics(),"history":self.history[-120:],"log":list(reversed(self.log[-log_limit:]))}
    def patients_csv(self):
        buf=io.StringIO(); cols=["patient_id","name","triage_level","triage_level_on_arrival","arrived_min","arrived_clock","wait_min","treatment_min","discharged_min","resources","by_ambulance","boarded_in_ward","icu_blocked_min","outcome","seen_within_target"]; w=csv.DictWriter(buf,fieldnames=cols); w.writeheader(); [w.writerow(r) for r in self.records]; return buf.getvalue()
    def advance(self,minutes):
        ran=0
        while ran<minutes and self.t<self.horizon: self.step(); ran+=1
        return ran
    def run_to_end(self):
        while self.t<self.horizon: self.step()
        return self


def run_once(seed,policy,load_pct,capacity=None,horizon=HORIZON): return Sim(seed,policy,load_pct,capacity,horizon).run_to_end()

def compare_policies(seed,load_pct,policies=None,replications=1,capacity=None):
    policies=policies or list(POLICIES); return {"seed":seed,"load_pct":load_pct,"rows":[{"policy":p,"label":POLICY_LABELS[p],"replications":replications} for p in policies]}

def what_if(seed,load_pct,policy="medflow",deltas=None,replications=3,capacity=None): return {"seed":seed,"load_pct":load_pct,"policy":policy,"replications":replications,"options":[]}
