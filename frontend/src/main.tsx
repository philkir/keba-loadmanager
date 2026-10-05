import React,{useEffect,useRef,useState} from 'react';
import {createRoot} from 'react-dom/client';
import {Activity,ArrowDownLeft,ArrowUpRight,ArrowRight,BatteryCharging,Building2,Check,ChevronDown,ChevronRight,CircleHelp,Clock3,Cloud,Cpu,FlaskConical,Gauge,History,LayoutDashboard,LoaderCircle,Pause,Play,PlugZap,Radio,RefreshCw,Settings2,ShieldCheck,Unplug,WifiOff,Zap} from 'lucide-react';
import type {State,Sample,Event,Settings,Simulation,Station,MeterSettings} from './types';
import './style.css';

const number=(n:number,d=1)=>n.toLocaleString('de-DE',{minimumFractionDigits:d,maximumFractionDigits:d});
const time=(ts:number)=>new Date(ts*1000).toLocaleTimeString('de-DE',{hour:'2-digit',minute:'2-digit'});
const labels={charging:'Lädt',paused:'Pausiert',available:'Verfügbar',safe:'Fehler',waiting:'Wartet',offline:'Nicht erreichbar',setup:'Modbus aus'};
const reasons={charging:'Leistung wird am Gerät gemessen',paused:'Ladevorgang unterbrochen',available:'Kein Fahrzeug verbunden',safe:'Gerät meldet einen Fehler',waiting:'Fahrzeug wartet auf Ladefreigabe',offline:'Verbindung nicht erreichbar',setup:'Gerät erreichbar, Modbus TCP noch aus'};

async function read<T>(path:string):Promise<T>{
  const res=await fetch(`/api/${path}`,{signal:AbortSignal.timeout(6000),cache:'no-store'});
  if(!res.ok)throw new Error('Lokale Instanz nicht erreichbar');
  return res.json();
}

type User={id:number;username:string;role:'admin'|'operator'|'viewer';created_at:number};
type AuthStatus={setup_required:boolean;user:User|null};

function AuthGate(){
  const [status,setStatus]=useState<AuthStatus|null>(null);
  const load=async()=>{const r=await fetch('/auth/status',{cache:'no-store'});setStatus(await r.json());};
  useEffect(()=>{void load();},[]);
  if(!status)return <div className="loading"><LoaderCircle className="spin"/><h2>Anmeldung wird vorbereitet</h2></div>;
  if(!status.user)return <AuthScreen setup={status.setup_required} onDone={load}/>;
  return <App user={status.user} onLogout={async()=>{await fetch('/auth/logout',{method:'POST'});await load();}}/>;
}

function AuthScreen({setup,onDone}:{setup:boolean;onDone:()=>Promise<void>}){
  const [username,setUsername]=useState(''),[password,setPassword]=useState(''),[confirm,setConfirm]=useState(''),[error,setError]=useState(''),[busy,setBusy]=useState(false);
  const submit=async(e:React.FormEvent<HTMLFormElement>)=>{e.preventDefault();setError('');const data=new FormData(e.currentTarget), submittedUsername=String(data.get('username')||''), submittedPassword=String(data.get('password')||''), submittedConfirm=String(data.get('confirm')||'');if(setup&&submittedPassword!==submittedConfirm){setError('Die Passwörter stimmen nicht überein.');return;}setBusy(true);try{const r=await fetch(setup?'/auth/setup':'/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:submittedUsername,password:submittedPassword})});const body:{detail?:string}=await r.json();if(!r.ok)throw new Error(body.detail||'Anmeldung fehlgeschlagen.');await onDone();}catch(e){setError(e instanceof Error?e.message:'Anmeldung fehlgeschlagen.');}finally{setBusy(false);}};
  return <main className="app-shell"><section className="content" style={{maxWidth:520,margin:'8vh auto'}}><div className="panel form-panel"><p className="eyebrow">KEBA LOAD MANAGER</p><h1>{setup?'Administrator einrichten':'Anmelden'}</h1><p className="subtitle">{setup?'Lege das erste Administratorkonto für diesen Ladepark an. Passwort-Manager werden vollständig unterstützt.':'Melde dich an, um den Ladepark zu sehen und zu verwalten.'}</p><form onSubmit={submit}><label className="field">Benutzername oder E-Mail<input name="username" autoComplete="username" required minLength={3} maxLength={254} value={username} onChange={e=>setUsername(e.target.value)}/></label><label className="field">Passwort<input name="password" type="password" autoComplete={setup?'new-password':'current-password'} required minLength={12} maxLength={256} value={password} onChange={e=>setPassword(e.target.value)}/></label>{setup&&<label className="field">Passwort wiederholen<input name="confirm" type="password" autoComplete="new-password" required value={confirm} onChange={e=>setConfirm(e.target.value)}/></label>}{error&&<p className="alert error">{error}</p>}<button className="button solid" disabled={busy}>{busy?'Bitte warten …':setup?'Administrator erstellen':'Anmelden'}</button></form></div></section></main>;
}

function Chart({samples,limit}:{samples:Sample[];limit:number}){
  const w=800,h=172,pad=8, max=Math.max(30,limit,...samples.map(s=>s.total_kw));
  const min=Math.min(0,...samples.map(s=>Math.min(s.total_kw,s.building_kw)));
  const x=(i:number)=>pad+i*(w-pad*2)/Math.max(1,samples.length-1);
  const y=(v:number)=>h-8-((v-min)/(max-min))*(h-20);
  const line=(field:'total_kw'|'building_kw')=>samples.map((s,i)=>`${x(i)},${y(s[field])}`).join(' ');
  return <div className="chart"><div className="axis-labels"><span>{max.toFixed(0)} kW</span><span>{((max+min)/2).toFixed(0)}</span><span>{min.toFixed(0)}</span></div>
    <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" role="img" aria-label="Verlauf von Netzbezug und Gebäudeverbrauch">
      <defs><linearGradient id="fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#a7cbb7" stopOpacity=".7"/><stop offset="100%" stopColor="#a7cbb7" stopOpacity=".06"/></linearGradient></defs>
      {[min,(max+min)/2,max].map(v=><line key={v} x1={0} x2={w} y1={y(v)} y2={y(v)} stroke="#e9ebe5" strokeDasharray="3 5"/>)}
      <line x1="0" x2={w} y1={y(limit)} y2={y(limit)} stroke="#c09559" strokeDasharray="5 5"/>
      {samples.length>1&&<><polygon points={`${x(0)},${y(0)} ${line('total_kw')} ${x(samples.length-1)},${y(0)}`} fill="url(#fill)"/><polyline points={line('total_kw')} fill="none" stroke="#317e5b" strokeWidth="2.5" vectorEffect="non-scaling-stroke"/><polyline points={line('building_kw')} fill="none" stroke="#9aaca3" strokeWidth="2" vectorEffect="non-scaling-stroke"/></>}
    </svg>
    {samples.length<2&&<p className="chart-empty">Der Verlauf entsteht mit den nächsten Messungen.</p>}
    <div className="chart-times"><span>{samples.length?time(samples[0].ts):'Jetzt'}</span><span>{samples.length?time(samples[samples.length-1].ts):'Jetzt'}</span></div>
  </div>;
}

function App({user,onLogout}:{user:User;onLogout:()=>Promise<void>}){
  const [state,setState]=useState<State|null>(null),[samples,setSamples]=useState<Sample[]>([]),[events,setEvents]=useState<Event[]>([]);
  const [page,setPage]=useState('overview'),[offline,setOffline]=useState(false),[busy,setBusy]=useState(false),[notice,setNotice]=useState('');
  const [settings,setSettings]=useState<Settings|null>(null),[sim,setSim]=useState<Simulation|null>(null);
  const [dirtySettings,setDirtySettings]=useState(false),[dirtySim,setDirtySim]=useState(false);
  const alive=useRef(true);
  async function refresh(){
    const next=await read<State>('state');
    if(!alive.current)return;
    if(Date.now()/1000-next.timestamp>6)throw new Error('Messwerte veraltet');
    setState(next);setOffline(false);
  }
  useEffect(()=>{
    alive.current=true;let timer:ReturnType<typeof setTimeout>;let count=0;
    const poll=async()=>{
      try{await refresh();if(count++%3===0){const [h,e]=await Promise.all([read<Sample[]>('history'),read<Event[]>('events')]);if(alive.current){setSamples(h);setEvents(e);}}}
      catch{if(alive.current)setOffline(true);}
      if(alive.current)timer=setTimeout(poll,2000);
    };
    void poll();return()=>{alive.current=false;clearTimeout(timer);};
  },[]);
  useEffect(()=>{if(state&&!dirtySettings)setSettings({...state.settings});},[state,dirtySettings]);
  useEffect(()=>{if(state&&!dirtySim)setSim({...state.simulation});},[state,dirtySim]);
  useEffect(()=>{if(notice){const t=setTimeout(()=>setNotice(''),6500);return()=>clearTimeout(t);}},[notice]);

  async function command(kind:string,value:unknown,station_id?:string){
    if(busy||offline)return false;
    setBusy(true);
    try{
      const response=await fetch('/api/commands',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:crypto.randomUUID(),issued_at:Date.now()/1000,kind,value,station_id}),signal:AbortSignal.timeout(6000)});
      const body:unknown=await response.json();
      const detail=typeof body==='object'&&body!==null&&'detail' in body&&typeof body.detail==='string'?body.detail:'Änderung nicht übernommen.';
      if(!response.ok)throw new Error(detail);
      await refresh();setEvents(await read<Event[]>('events'));setNotice(kind==='local_start'?'Startanfrage gesendet. Die Rückmeldung erscheint am Ladepunkt.':kind==='local_access'?'KEBA-Zugang hinterlegt.':'Änderung von der lokalen Instanz bestätigt.');return true;
    }catch(e){setNotice(e instanceof Error?e.message:'Keine Bestätigung erhalten. Status vor erneutem Versuch prüfen.');return false;}
    finally{setBusy(false);}
  }
  const disabled=busy||offline||!state||user.role==='viewer';
  const commissioning=state?.mode==='commissioning',active=state?.mode==='active',hardware=commissioning||active;
  const fallback=hardware&&state?.building_source==='fallback';
  const hasBuildingValue=Boolean(state&&state.building_source!=='unavailable'&&state.building_source!=='hold'&&(state.meter_online||fallback));
  const hasGridValue=Boolean(state&&(state.meter_online||fallback));
  const nav=[{id:'overview',label:'Übersicht',icon:LayoutDashboard},{id:'history',label:'Ereignisse',icon:History},hardware?{id:'commissioning',label:'Ladepunkte',icon:PlugZap}:{id:'simulation',label:'Simulation',icon:FlaskConical},{id:'settings',label:'Einstellungen',icon:Settings2}];
  const changeSettings=(patch:Partial<Settings>)=>{setSettings(s=>s?{...s,...patch}:s);setDirtySettings(true);};
  const changeSim=(patch:Partial<Simulation>)=>{setSim(s=>s?{...s,...patch}:s);setDirtySim(true);};
  const applyScenario=async(patch:Partial<Simulation>)=>{if(state&&await command('simulation',{...state.simulation,...patch}))setDirtySim(false);};

  return <div className="app-shell">
    <aside className="sidebar"><a className="brand" href="#" onClick={e=>{e.preventDefault();setPage('overview');}}><span className="brand-symbol"><Zap size={23} fill="currentColor"/></span><span>ladepark<span className="brand-sub">ENERGIE IM GLEICHGEWICHT</span></span></a>
      <div className="site-switch"><span className="site-icon"><Building2 size={19}/></span><div><strong>{state?.site_name||'Unser Ladepark'}</strong><small>4 Ladepunkte · KEBA</small></div></div>
      <p className="nav-label">VERWALTUNG</p><nav>{nav.map(n=><button key={n.id} className={page===n.id?'nav-item selected':'nav-item'} onClick={()=>setPage(n.id)}><n.icon size={18}/>{n.label}{page===n.id&&<span className="nav-dot"/>}</button>)}</nav>
      <div className="sidebar-bottom"><div className="local-info"><span className={`status-dot ${offline?'red':''}`}/><div><strong>{offline?'Instanz nicht erreichbar':'Lokale Instanz'}</strong><small>{user.username} · {user.role}</small></div></div><span className="version">MVP 0.2 <span>{active?'AKTIV':commissioning?'NUR LESEN':'SIMULATION'}</span></span></div>
    </aside>
    <main><header className="topbar"><div className="breadcrumb">Ladepark <ChevronRight size={13}/><strong>{nav.find(n=>n.id===page)?.label}</strong></div><div className="top-right"><span className={`simulation-pill ${active?'active':''}`}>{hardware?<PlugZap size={13}/>:<FlaskConical size={13}/>} {active?'Regelung aktiv':commissioning?'Nur-Lese-Modus':'Simulationsmodus'}</span><span className="avatar">{user.username.slice(0,2).toUpperCase()}</span><button className="text-button" onClick={()=>void onLogout()}>Abmelden</button></div></header>
    <div className="content"><div className="page-heading"><div><p className="eyebrow">ENERGIEMANAGEMENT</p><h1>{page==='overview'?(active?'Lokale Lastregelung.':commissioning?'Echte Geräte im lokalen Netz.':'Alles im Gleichgewicht.'):page==='simulation'?'Szenarien ausprobieren.':page==='commissioning'?'Ladestationen verwalten.':page==='settings'?'Dein Ladepark. Deine Regeln.':'Jede Änderung im Blick.'}</h1><p className="subtitle">{page==='overview'?(active?(state?.settings.meter.enabled?'Die Hauptanschlussmessung bestimmt das verfügbare Ladebudget.':'Die Ladefreigaben werden anhand des konfigurierten Fallback-Verbrauchs verteilt.'):commissioning?'Live-Verbindung zu den KEBA-Ladestationen im Nur-Lese-Modus.':'Ein Anschluss. Vier Ladepunkte. Intelligent verteilt.'):page==='simulation'?'Teste den Regler mit wechselnder Last und unterbrochenen Verbindungen.':page==='commissioning'?'IP-Adressen speichern, Modbus prüfen und die KEBA-Weboberfläche öffnen.':page==='settings'?'Die lokale Instanz prüft und bestätigt jede Änderung.':'Regelzustände und bestätigte Einstellungen aus der lokalen Instanz.'}</p></div>
      {page==='overview'&&state&&(active||state.mode==='simulation')&&<button className="button secondary" disabled={disabled} onClick={()=>void command('settings',{...state.settings,paused:!state.settings.paused})}>{state.settings.paused?<Play size={15}/>:<Pause size={15}/>} {state.settings.paused?'Alle fortsetzen':'Alle pausieren'}</button>}
    </div>
    {offline&&<div className="alert error" role="alert"><WifiOff size={20}/><div><strong>Verbindung zur lokalen Instanz unterbrochen</strong><p>Die angezeigten Werte sind veraltet. Änderungen sind gesperrt. Der lokale Regler kann unabhängig weiterlaufen.</p></div></div>}
    {!state?<div className="loading"><LoaderCircle className="spin"/><h2>{offline?'Auf lokale Instanz warten':'Ladepark wird verbunden'}</h2><p>Messwerte werden von der lokalen FastAPI abgerufen.</p></div>:<>
      {commissioning&&!offline&&<div className="alert warning" role="alert"><ShieldCheck size={21}/><div><strong>Inbetriebnahme im Nur-Lese-Modus</strong><p>Es werden nur echte Geräte gelesen. Automatische Leistungsfreigaben und Modbus-Schreibzugriffe sind gesperrt.</p></div><button className="text-button" onClick={()=>setPage('commissioning')}>Geräte öffnen <ArrowRight size={15}/></button></div>}
      {active&&fallback&&!offline&&<div className="alert active-control" role="status"><ShieldCheck size={21}/><div><strong>Aktive Regelung mit Fallback</strong><p>Da kein Gebäudenzähler verbunden ist, reserviert der Regler dauerhaft {number(state.settings.fallback_building_kw)} kW als Gebäudeverbrauch.</p></div><button className="text-button" onClick={()=>setPage('settings')}>Fallback anpassen <ArrowRight size={15}/></button></div>}
      {(state.status==='safe'||state.measurement_error)&&!offline&&<div className="alert warning" role="alert"><ShieldCheck size={21}/><div><strong>{state.status==='holding'?'Kurze Messlücke':active?'Sicherer Halt aktiv':'Messung nicht verfügbar'}</strong><p>{state.measurement_error||'Messung oder Ladepunkt nicht erreichbar.'} {state.status==='holding'?'Bestehende Freigaben werden kurz gehalten; keine Leistungserhöhung. Nach spätestens 5 Sekunden Messalter folgt ein sicherer Halt.':active?'Ladefreigaben auf 0 A angefordert.':''}</p></div></div>}
      {state.overload&&!offline&&<div className="alert error" role="alert"><Activity size={20}/><div><strong>Hauptanschluss überschreitet eine Anschlussgrenze</strong><p>Die Ladeleistung wurde reduziert. Der Regler kann den Gebäudeverbrauch selbst nicht senken.</p></div></div>}
      {page==='overview'&&<>
        <EnergyFlow state={state}/>{hardware&&state.settings.meter.enabled&&<MeterMeasurements meter={state.meter}/>}
        <section className={`metrics ${offline?'stale':''}`}>
          <article className="metric primary"><div className="metric-label">{fallback?'Kalkulierter Netzbezug':state.total_kw<0?'Netzeinspeisung':'Aktueller Netzbezug'} <ArrowDownLeft size={19}/></div><div className="metric-value">{hasGridValue?number(Math.abs(state.total_kw)):'—'} <span>kW</span></div><div className="metric-meter"><i style={{width:`${Math.max(0,Math.min(100,state.total_kw/state.settings.power_limit_kw*100))}%`}}/></div><div className="metric-foot"><span>Ziel {number(state.settings.power_limit_kw)} kW · Puffer bis {number(state.power_ceiling_kw??state.settings.power_limit_kw)} kW</span><strong>{hasGridValue?`${number(Math.abs(state.total_kw)/state.settings.power_limit_kw*100,0)} %`:'—'}</strong></div></article>
          <article className="metric"><div className="metric-label">{fallback?'Fallback Gebäudelast':'Gebäudelast (netto)'} <Building2 size={19}/></div><div className="metric-value">{hasBuildingValue?number(state.building_kw):'—'} <span>kW</span></div><div className="metric-foot"><span className="legend grey"/> {fallback?'Konservativer Ersatzwert':hardware?'Hauptanschluss minus Ladeleistung':'Gebäude hat Vorrang'}</div></article>
          <article className="metric"><div className="metric-label">Ladeleistung <PlugZap size={20}/></div><div className="metric-value">{number(state.charging_kw)} <span>kW</span></div><div className="metric-foot"><span className="legend green"/>{state.stations.filter(s=>s.status==='charging').length} von 4 Ladepunkten laden</div></article>
          <article className="metric"><div className="metric-label">Verfügbares Budget <Gauge size={19}/></div><div className="metric-value">{hasBuildingValue?number(state.headroom_kw):'—'} <span>kW</span></div><div className="metric-foot">Nach {number(state.settings.reserve_kw)} kW Regelreserve</div></article>
        </section>
        <section className="overview-grid"><article className="panel chart-panel"><div className="panel-heading"><div><h2>Leistung im Verlauf</h2><p>{hardware?'Netzbezug und Gebäudelast; Messausfälle werden nicht gespeichert':'Echte Messpunkte der laufenden Simulation'}</p></div><span className="quiet-tag"><Clock3 size={12}/> Letzte 15 Minuten</span></div><div className="chart-legend"><span><i className="legend green"/> Netzbezug</span><span><i className="legend grey"/> {fallback?'Fallback':'Gebäude'}</span><span><i className="limit-line"/> Anschlussgrenze</span></div><Chart samples={samples} limit={state.settings.power_limit_kw}/></article>
          <article className="panel phase-panel"><div className="panel-heading"><div><h2>Phasen im Blick</h2><p>Strom je Außenleiter</p></div><Activity size={18}/></div>{state.phase_currents_a.map((a,i)=><div className="phase" key={i}><div><strong>L{i+1}</strong><span>{hasGridValue?number(a):'—'} <small>/ {state.settings.phase_limit_a} A</small></span></div><div className="phase-track"><i className={a>state.settings.phase_limit_a?'over':''} style={{width:`${hasGridValue?Math.min(100,a/state.settings.phase_limit_a*100):0}%`}}/></div></div>)}<div className="phase-note"><CircleHelp size={13}/><span>{hardware?(state.meter_online?'Gemessene Ströme am Hauptanschluss inklusive Ladepunkten.':fallback?'Fallback-Last wird gleichmäßig auf drei Phasen angesetzt.':'Keine gültige Hauptanschlussmessung.'):`${state.settings.phase_limit_a} A ist eine Simulationsannahme.`}</span></div></article></section>
        <div className="section-heading"><div><h2>Deine Ladepunkte <span>04</span></h2><p>{commissioning?'Live-Status der lokal konfigurierten KEBA-Geräte.':'Laufende Ladungen bleiben bevorzugt. Freie Leistung wird schrittweise verteilt.'}</p></div><span className="live-label"><span className={`status-dot ${offline?'red':''}`}/>{offline?'Daten veraltet':'Live · alle 2 Sekunden'}</span></div>
        <section className="station-grid">{state.stations.map(s=><StationCard key={s.id} station={s} disabled={disabled} globalPaused={state.settings.paused} hardware={hardware} controllable={Boolean(active||state.mode==='simulation')} requested={Boolean(state.manual_start_ids?.includes(s.id))} onStart={()=>void command(active?'start':'station',active?{}:{paused:false},s.id)} onLocalStart={active?()=>void command('local_start',{},s.id):undefined} onConfigureLocal={()=>setPage('commissioning')} onPause={()=>void command('station',{paused:true},s.id)} onPriority={()=>void command('station',{priority:s.priority==='high'?'normal':'high'},s.id)}/>)}</section>
        <div className="bottom-row"><div><ShieldCheck size={16}/><span>{active?'Aktive Modbus-Leistungsfreigaben mit Geräte-Failsafe':commissioning?'Nur-Lesen · keine Leistungsfreigaben':'Lastmanagement lokal · Betrieb ohne Cloud-Verbindung vorgesehen'}</span></div><div><Cloud size={16}/><span>Monta bleibt unabhängig per OCPP verbunden</span></div></div>
      </>}
      {page==='simulation'&&sim&&<div className="settings-grid"><section className="panel form-panel"><div className="panel-heading"><div><h2>Gebäudelast simulieren</h2><p>Diese Änderungen wirken ausschließlich auf simulierte Geräte.</p></div><FlaskConical size={20}/></div><label className="range-label" htmlFor="building">Gebäudeverbrauch <strong>{number(sim.building_kw)} kW</strong></label><input id="building" type="range" min="0" max="30" step="0.5" value={sim.building_kw} onChange={e=>changeSim({building_kw:Number(e.target.value)})}/><div className="range-ends"><span>0 kW</span><span>30 kW</span></div><label className="field">Verteilung auf die Phasen<select value={sim.profile} onChange={e=>changeSim({profile:e.target.value as Simulation['profile']})}><option value="balanced">Gleichmäßig · 33 / 33 / 33 %</option><option value="uneven">Ungleichmäßig · 60 / 25 / 15 %</option></select></label><label className="toggle-row"><span><strong>Zähler erreichbar</strong><small>Bei Ausfall pausieren alle Ladepunkte.</small></span><input type="checkbox" checked={sim.meter_online} onChange={e=>changeSim({meter_online:e.target.checked})}/></label><button className="button solid" disabled={disabled||!dirtySim} onClick={async()=>{if(await command('simulation',sim))setDirtySim(false);}}>{busy?<LoaderCircle className="spin" size={16}/>:<Check size={16}/>} Szenario anwenden</button></section>
        <section className="panel form-panel"><div className="panel-heading"><div><h2>Schnell ausprobieren</h2><p>Ein Klick setzt ein vollständiges Lastszenario.</p></div></div>{[{label:'Normaler Betrieb',desc:'7 kW Gebäudelast, alle vier Fahrzeuge verbunden',icon:Building2,data:{building_kw:7,profile:'balanced',meter_online:true,disconnected:[],offline:[]}},{label:'Hoher Gebäudeverbrauch',desc:'18 kW Last, knappe Leistung für Fahrzeuge',icon:Gauge,data:{building_kw:18,profile:'balanced',meter_online:true,disconnected:[],offline:[]}},{label:'Eine Phase am Limit',desc:'12 kW, davon 60 % auf Phase L1',icon:Activity,data:{building_kw:12,profile:'uneven',meter_online:true,disconnected:[],offline:[]}},{label:'Zählerausfall',desc:'Der Regler wechselt in den sicheren Halt',icon:WifiOff,data:{meter_online:false}}].map(p=><button className="scenario" key={p.label} disabled={disabled} onClick={()=>void applyScenario(p.data as Partial<Simulation>)}><span className="scenario-icon"><p.icon size={20}/></span><span><strong>{p.label}</strong><small>{p.desc}</small></span><ArrowUpRight size={17}/></button>)}</section>
        <section className="panel form-panel full"><div className="panel-heading"><div><h2>Fahrzeuge & Verbindungen</h2><p>Fahrzeug abstecken oder einen Kommunikationsausfall nachstellen.</p></div></div>{state.stations.map(s=><div className="connection-row" key={s.id}><span><strong>{s.name}</strong><small>{s.model}</small></span><button className="button secondary" disabled={disabled} onClick={()=>void applyScenario({disconnected:s.connected?[...state.simulation.disconnected,s.id]:state.simulation.disconnected.filter(id=>id!==s.id)})}>{s.connected?<Unplug size={15}/>:<PlugZap size={15}/>} {s.connected?'Fahrzeug abstecken':'Fahrzeug verbinden'}</button><button className="button secondary" disabled={disabled} onClick={()=>void applyScenario({offline:s.online?[...state.simulation.offline,s.id]:state.simulation.offline.filter(id=>id!==s.id)})}>{s.online?<WifiOff size={15}/>:<RefreshCw size={15}/>} {s.online?'Verbindung trennen':'Verbindung herstellen'}</button></div>)}</section></div>}
      {page==='commissioning'&&hardware&&<div className="settings-grid commissioning-grid">{state.stations.map(s=><CommissioningStation key={s.id} station={s} disabled={disabled} onSave={patch=>command('station',patch,s.id)} onLocalAccess={active&&user.role==='admin'?credentials=>command('local_access',credentials,s.id):undefined}/>)}</div>}
      {page==='settings'&&settings&&<div className="settings-grid"><section className="panel form-panel"><div className="panel-heading"><div><h2>Standort & Grenzen</h2><p>{active?'Diese Werte steuern die realen Ladefreigaben.':commissioning?'Die Grenzwerte werden gespeichert, aber im Nur-Lese-Modus nicht geregelt.':'Werte gelten für die Simulation.'}</p></div></div><label className="field">Name des Standorts<input maxLength={60} value={settings.site_name} onChange={e=>changeSettings({site_name:e.target.value})}/></label><div className="field-pair"><label className="field">Regelziel / Vertragsleistung (kW)<input type="number" min="5" max="25" step="0.5" value={settings.power_limit_kw} onChange={e=>changeSettings({power_limit_kw:Number(e.target.value)})}/></label><label className="field">Absicherung je Phase (A)<input type="number" min="6" max="120" value={settings.phase_limit_a} onChange={e=>changeSettings({phase_limit_a:Number(e.target.value)})}/></label></div><div className="field-pair"><label className="field">Regelreserve (kW)<input type="number" min="0.5" max="5" step="0.5" value={settings.reserve_kw} onChange={e=>changeSettings({reserve_kw:Number(e.target.value)})}/></label><label className="field">Fallback Gebäudelast (kW)<input type="number" min="0" max="20" step="0.5" value={settings.fallback_building_kw} onChange={e=>changeSettings({fallback_building_kw:Number(e.target.value)})}/></label></div><label className="field">Leistungspuffer über Regelziel (%)<input type="number" min="0" max="10" step="1" value={settings.power_tolerance_pct} onChange={e=>changeSettings({power_tolerance_pct:Number(e.target.value)})}/><small>Nur für Lastspitzen: maximal {number(settings.power_limit_kw*(1+settings.power_tolerance_pct/100))} kW. Die Absicherung je Phase gilt separat.</small></label><label className="field">Reihenfolge wartender Fahrzeuge (s)<input type="number" min="30" max="900" step="30" value={settings.rotation_seconds} onChange={e=>changeSettings({rotation_seconds:Number(e.target.value)})}/></label><div className="field-pair"><label className="field">Hochregeln: +1 A alle (s)<input type="number" min="5" max="60" value={settings.ramp_up_seconds} onChange={e=>changeSettings({ramp_up_seconds:Number(e.target.value)})}/></label><label className="field">Wiederanlauf nach Stopp (s)<input type="number" min="10" max="300" value={settings.restart_delay_seconds} onChange={e=>changeSettings({restart_delay_seconds:Number(e.target.value)})}/></label></div><button className="button solid" disabled={disabled||!dirtySettings} onClick={async()=>{if(await command('settings',settings))setDirtySettings(false);}}><Check size={16}/> Einstellungen speichern</button></section><section className="panel form-panel architecture"><div className="panel-heading"><div><h2>So arbeitet dein System</h2><p>Die Regelung bleibt vor Ort.</p></div></div>{[{icon:Cloud,title:'Cloudflare Worker',text:'React-Oberfläche und geschützte API-Weiterleitung.'},{icon:ShieldCheck,title:'Lokale Python-Instanz',text:active?'Schreibt sichere Stromlimits direkt an die KEBA-Wallboxen.':commissioning?'Liest die echten Wallboxen im lokalen Netz aus.':'Ein eigenständiger Regler verteilt die verfügbare Leistung.'},{icon:History,title:'SQLite',text:'Einstellungen, Ereignisse und Verlauf bleiben lokal gespeichert.'},{icon:PlugZap,title:'Monta',text:'Monta bleibt per OCPP angebunden. Ein manueller Start kann zusätzlich direkt an der KEBA angefragt werden.'}].map(s=><div className="architecture-row" key={s.title}><s.icon size={22}/><div><strong>{s.title}</strong><p>{s.text}</p></div></div>)}<div className="info-note">{active?'Der Shelly misst Gebäude und Ladepunkte gemeinsam. Ohne aktivierten Zähler gilt der Fallback; Kurze Kommunikationslücken werden innerhalb der 5-Sekunden-Messfrist überbrückt, danach werden 0 A angefordert. Jede Wallbox erhält einen 10-Sekunden-Failsafe.':commissioning?'Nur-Lese-Modus: Modbus-Schreibzugriffe sind gesperrt.':'Hardwaresteuerung ist im Simulationsmodus deaktiviert.'}</div></section></div>}
      {page==='settings'&&settings&&hardware&&<MeterPanel settings={settings.meter} meter={state.meter} disabled={disabled} dirty={dirtySettings} onChange={patch=>changeSettings({meter:{...settings.meter,...patch}})} onSave={async()=>{if(await command('settings',settings))setDirtySettings(false);}}/>}
      {page==='settings'&&settings&&<SettingsHelp settings={settings}/>}
      {page==='settings'&&user.role==='admin'&&<UserManagement/>}
      {page==='history'&&<section className="panel events-panel"><div className="panel-heading"><div><h2>Ereignisprotokoll</h2><p>Die letzten 80 Ereignisse · lokal gespeichert</p></div><span className="quiet-tag">{events.length} Einträge</span></div><div className="events-table"><div className="event-row event-head"><span>ZEITPUNKT</span><span>STATUS</span><span>EREIGNIS</span></div>{events.map(e=><div className="event-row" key={e.id}><span>{new Date(e.ts*1000).toLocaleDateString('de-DE',{day:'2-digit',month:'2-digit'})} · {time(e.ts)}</span><span><i className={`event-tag ${e.level}`}>{e.level==='warning'?'Hinweis':'Information'}</i></span><span>{e.message}</span></div>)}</div></section>}
      <footer><span>KEBA P30 x + P40 · Lokales Lastmanagement</span><span>{state.mode==='simulation'?'Simulierte Messwerte':'Messwerte'} · Stand {time(state.timestamp)} Uhr</span></footer>
    </>}
    </div></main>{notice&&<div className="toast" role="status"><CircleHelp size={17}/>{notice}<button aria-label="Meldung schließen" onClick={()=>setNotice('')}>×</button></div>}
  </div>;
}

function MeterMeasurements({meter}:{meter:State['meter']}){
  const age=meter?.timestamp?Math.max(0,Math.floor(Date.now()/1000-meter.timestamp)):null;
  return <section className="panel form-panel" style={{marginBottom:20}} aria-label="Shelly-Messwerte">
    <div className="panel-heading"><div><h2>Hauptanschluss · Shelly</h2><p>{meter?.online?'Aktuelle Messung':meter?.currents_a?'Letzte gültige Messung · derzeit keine aktuelle Verbindung':'Noch keine gültige Messung'}{age!==null?` · vor ${age} s`:''}</p></div><Activity size={20}/></div>
    {meter?.error&&<p role="status">{meter.error}</p>}
    <div className={`field-pair ${!meter?.online?'stale':''}`}>{[0,1,2].map(p=><p key={p}><strong>L{p+1}</strong> · {meter?.currents_a?number(meter.currents_a[p],2):'—'} A · {meter?.voltages_v?number(meter.voltages_v[p],1):'—'} V · {meter?.phase_powers_kw?number(meter.phase_powers_kw[p],2):'—'} kW</p>)}</div>
  </section>;
}

function MeterPanel({settings,meter,disabled,dirty,onChange,onSave}:{settings:MeterSettings;meter:State['meter'];disabled:boolean;dirty:boolean;onChange:(patch:Partial<MeterSettings>)=>void;onSave:()=>Promise<void>}){
  return <section className="panel form-panel" style={{marginTop:20}} aria-labelledby="meter-title">
    <div className="panel-heading"><div><h2 id="meter-title">Hauptanschluss · Shelly Pro 3EM 120A v2</h2><p>Misst den gesamten Anschluss inklusive aller Ladepunkte über Modbus TCP.</p></div><Activity size={20}/></div>
    <label className="toggle-row"><span><strong>Hauptanschlussmessung aktivieren</strong><small>Kurze Kommunikationslücken werden überbrückt. Ab 5 Sekunden Messalter oder bei ungültigen Werten gilt ein sicherer Halt.</small></span><input type="checkbox" disabled={disabled} checked={settings.enabled} onChange={e=>onChange({enabled:e.target.checked})}/></label>
    <div className="field-pair"><label className="field">Shelly-IP-Adresse<input disabled={disabled} placeholder="192.168.1.217" value={settings.host} onChange={e=>onChange({host:e.target.value})}/></label><label className="field">Modbus-Port<input disabled={disabled} type="number" min="1" max="65535" value={settings.port} onChange={e=>onChange({port:Number(e.target.value)})}/></label></div>
    <label className="field">Modbus Unit-ID<input disabled={disabled} type="number" min="1" max="255" value={settings.device_id} onChange={e=>onChange({device_id:Number(e.target.value)})}/></label>
    <p className="info-note">Am Shelly Modbus TCP aktivieren, das Profil „triphase“ und die passenden 120-A-Stromwandler einstellen. Gerätezeit über NTP synchronisieren. Shelly A/B/C müssen L1/L2/L3 der Ladepunkte entsprechen.</p>
    <p role="status">{meter?.online?`Verbunden · ${number(meter.power_kw??0)} kW am Hauptanschluss · Stand ${time(meter.timestamp)} Uhr`:meter?.error||'Keine aktive Messverbindung.'}</p>
    <MeterMeasurements meter={meter}/>
    <button className="button solid" disabled={disabled||!dirty||(settings.enabled&&!settings.host)} onClick={()=>void onSave()}><Check size={16}/> Messung speichern</button>
  </section>;
}

function SettingsHelp({settings}:{settings:Settings}){
  const powerBudget=Math.max(0,settings.power_limit_kw-settings.fallback_building_kw-settings.reserve_kw);
  const phaseBudget=Math.max(0,settings.phase_limit_a-settings.fallback_building_kw*1000/690-settings.reserve_kw*1000/690);
  const current=Math.max(0,Math.floor(Math.min(powerBudget*1000/690,phaseBudget)));
  const chargingKw=current*690/1000;
  return <section className="panel settings-help" aria-labelledby="settings-help-title">
    <div className="panel-heading"><div><h2 id="settings-help-title">Was bedeuten diese Werte?</h2><p>Die Vorschau zeigt den Betrieb ohne aktivierte Hauptanschlussmessung.</p></div><ShieldCheck size={20}/></div>
    <div className="settings-help-grid">
      <article><strong>Name des Standorts</strong><p>Bezeichnung im Dashboard und Ereignisprotokoll. Dieser Wert beeinflusst die Regelung nicht.</p></article>
      <article><strong>Anschlussgrenze</strong><p>Ziel für die Gesamtleistung von Gebäude und Ladepunkten. Ein konfigurierter Leistungspuffer erlaubt vorübergehend bis zu 10 % darüber. Die Phasengrenzen bleiben unverändert.</p></article>
      <article><strong>Grenze je Phase</strong><p>Maximal erlaubter Strom auf L1, L2 und L3. Die strengere Grenze aus Gesamtleistung und Phasenstrom entscheidet.</p></article>
      <article><strong>Ruhiger Ladebetrieb</strong><p>Hochregeln um 1 A alle {settings.ramp_up_seconds} Sekunden bei bestätigtem Spielraum. Nach einem erforderlichen Stopp wartet der Regler {settings.restart_delay_seconds} Sekunden. Kurze Lastspitzen werden abgefangen, harte Grenzen sofort berücksichtigt.</p></article><article><strong>Regelreserve</strong><p>Puffer für Lastschwankungen. Neue Leistung wird mit voller Reserve freigegeben. Kurze Lastspitzen dürfen diesen Puffer nutzen; die Anschlussgrenzen gelten weiterhin sofort.</p></article>
      <article><strong>Fallback-Gebäudelast</strong><p>Angenommener Verbrauch ohne aktivierten Stromzähler. Bei Ausfall eines aktivierten Zählers gilt stattdessen ein sicherer Halt. Er muss mindestens so hoch wie der höchste plausible gleichzeitige Gebäudeverbrauch sein.</p></article>
      <article><strong>Warteplatzrotation</strong><p>Ändert bei knapper Leistung nach {settings.rotation_seconds} Sekunden die Reihenfolge wartender Fahrzeuge. Laufende Sitzungen werden nicht durch die Rotation verdrängt.</p></article>
    </div>
    <div className="budget-preview">
      <div><span>Ladebudget ohne Zähler</span><strong>{number(powerBudget)} kW</strong><small>{number(settings.power_limit_kw)} − {number(settings.fallback_building_kw)} − {number(settings.reserve_kw)} kW</small></div>
      <div><span>Verfügbar je Phase</span><strong>{number(phaseBudget)} A</strong><small>nach Fallback und Reserve</small></div>
      <div><span>Maximal bei einem Ladepunkt</span><strong>{current} A · {number(chargingKw)} kW</strong><small>dreiphasig, auf ganze Ampere abgerundet</small></div>
    </div>
    <p className="fallback-warning"><ShieldCheck size={16}/><span>Ohne echten Gebäudestromzähler ist die Anschlussgrenze nur dann geschützt, wenn die tatsächliche Gebäudelast den eingestellten Fallback nicht überschreitet.</span></p>
  </section>;
}

function EnergyFlow({state}:{state:State}){
  const limit=state.settings.power_limit_kw;
  const granted=state.estimated_charging_kw??state.charging_kw;
  const building=Math.max(0,state.building_kw);
  const values=[building,granted,state.settings.reserve_kw];
  const used=values.reduce((sum,value)=>sum+value,0);
  const free=Math.max(0,limit-used);
  const online=state.stations.filter(s=>s.online).length;
  const capable=state.stations.filter(s=>(s.hardware_limit_a??s.max_current_a)>=32).length;
  if(state.building_source==='hold')return <section className="control-deck"><div className="control-deck-copy"><h2>Freigaben kurz gehalten</h2><p>Die nächste vollständige Messung wird abgewartet. Es wird keine zusätzliche Ladeleistung vergeben.</p></div></section>;
  if(state.building_source==='unavailable')return <section className="control-deck"><div className="control-deck-copy"><h2>Kein Ladebudget freigegeben</h2><p>Eine vollständige, aktuelle Messung ist für die Regelung erforderlich.</p></div></section>;
  return <section className="control-deck" aria-label="Live-Regelbudget">
    <div className="control-deck-copy"><span className="control-kicker"><Radio size={14}/> LIVE CONTROL</span><h2>{number(granted)} kW für Fahrzeuge freigegeben</h2><p>{number(building)} kW {state.building_source==='fallback'?'Fallback':'Gebäudelast'} · {number(state.settings.reserve_kw)} kW Reserve · Grenze {number(limit,0)} kW</p></div>
    <div className="energy-stack" aria-label={`${number(used)} von ${number(limit)} Kilowatt verplant`}>
      <span className="energy-building" style={{width:`${Math.min(100,building/limit*100)}%`}} title="Reservierte Gebäudelast"/>
      <span className="energy-charging" style={{width:`${Math.min(100,granted/limit*100)}%`}} title="Ladefreigabe"/>
      <span className="energy-reserve" style={{width:`${Math.min(100,state.settings.reserve_kw/limit*100)}%`}} title="Reserve"/>
    </div>
    <div className="energy-legend"><span><i className="building"/>Gebäude {number(building)} kW</span><span><i className="charging"/>Freigabe {number(granted)} kW</span><span><i className="reserve"/>Reserve {number(state.settings.reserve_kw)} kW</span><strong>{number(free)} kW frei</strong></div>
    <div className="control-health"><span><Radio size={15}/><strong>{online}/4</strong> Modbus online</span><span><Cpu size={15}/><strong>{capable}/4</strong> mit 32 A erkannt</span><span><ShieldCheck size={15}/><strong>10 s</strong> Failsafe</span></div>
  </section>;
}

function StationCard({station:s,disabled,globalPaused,hardware,controllable,requested,onStart,onPause,onPriority,onLocalStart,onConfigureLocal}:{station:Station;disabled:boolean;globalPaused:boolean;hardware:boolean;controllable:boolean;requested:boolean;onLocalStart?:()=>void;onConfigureLocal:()=>void;onStart:()=>void;onPause:()=>void;onPriority:()=>void}){
  const currents=s.currents_a??[0,0,0],voltages=s.voltages_v??[null,null,null];
  const target=s.commanded_current_a??s.current_a;
  const localPending=s.local_start_result?.status==='pending'||Boolean(s.local_start_result&&['accepted','uncertain'].includes(s.local_start_result.status)&&Date.now()/1000-s.local_start_result.applied_at<30);
  return <article className={`station-card station-card-v2 ${s.status==='charging'?'is-charging':''}`}>
    <div className="station-top"><div className="station-identity"><div className="station-icon"><PlugZap size={22}/></div><div><h3>{s.name}</h3><div className="station-model">KEBA {s.model} · {hardware?(s.host||'IP fehlt'):'3-phasig'}</div></div></div><span className={`station-status ${s.status}`}><span/>{labels[s.status]}</span></div>
    <div className="station-live"><div><span>Aktuelle Leistung</span><strong>{number(s.power_kw)} <small>kW</small></strong></div><BatteryCharging size={28}/></div>
    <div className="station-track"><i style={{width:`${Math.min(100,s.current_a/Math.max(1,s.max_current_a)*100)}%`}}/></div>
    <div className="station-kpis"><div><span>Gemessen</span><strong>{number(s.current_a)} A</strong></div><div><span>Freigabe</span><strong>{number(target,0)} A</strong></div><div title="Von der Wallbox gemeldete aktuelle Obergrenze; berücksichtigt Installation, Kabel und Temperatur."><span>Hardware aktuell</span><strong>{number(s.hardware_limit_a??s.max_current_a,0)} A</strong></div></div>
    <p className="station-reason">{s.connection_detail||reasons[s.status]}</p>
    {controllable&&<div className="station-actions station-actions-v2"><button disabled={disabled} className={s.priority==='high'?'priority high':'priority'} onClick={onPriority}><ArrowUpRight size={15}/>{s.priority==='high'?'Hohe Priorität':'Normal priorisiert'}</button>{s.paused||s.status==='waiting'?<button disabled={disabled||globalPaused||!s.connected} className="button solid station-command" onClick={onStart}><Play size={15}/> {requested?'Angefordert':s.paused?'Fortsetzen':'Erneut anfordern'}</button>:<button disabled={disabled||globalPaused||!s.connected} className="button secondary station-command" onClick={onPause}><Pause size={15}/> Pausieren</button>}</div>}
    {onLocalStart&&<div className="manual-start-control">
      <div><strong>Manueller Ladestart</strong><small>Freigabe direkt an der KEBA anfordern.</small></div>
      {s.local_start_configured?<button className="button solid" disabled={disabled||globalPaused||s.paused||!s.connected||!s.online||s.status==='charging'||s.status==='safe'||localPending} onClick={onLocalStart}>{s.local_start_result?.status==='pending'?<LoaderCircle className="spin" size={15}/>:<Play size={15}/>} Manuell starten</button>:<button className="button secondary" onClick={onConfigureLocal}>KEBA-Zugang einrichten</button>}
      {s.local_start_result&&<p role="status" className={`local-start-status ${s.local_start_result.status}`}>{s.local_start_result.message}</p>}
    </div>}
    {hardware&&<details className="station-telemetry"><summary>Alle Gerätedaten <ChevronDown size={16}/></summary><dl>
      <div><dt>Phasenströme</dt><dd>{currents.map((v,i)=>`L${i+1} ${number(v)} A`).join(' · ')}</dd></div>
      <div><dt>Spannungen</dt><dd>{voltages.map((v,i)=>`L${i+1} ${v==null?'—':number(v,0)+' V'}`).join(' · ')}</dd></div>
      <div><dt>Installationslimit</dt><dd>{number(s.installation_limit_a??s.max_current_a,0)} A</dd></div>
      <div><dt>Geräte-/Hardwarelimit</dt><dd>{number(s.device_limit_a??s.max_current_a,0)} / {number(s.hardware_limit_a??s.max_current_a,0)} A</dd></div>
      <div><dt>Adaptives Bedarfslimit</dt><dd>{number(s.adaptive_limit_a??s.max_current_a,0)} A</dd></div>
      <div><dt>Ungenutzte Freigabe</dt><dd>{number(s.unused_grant_a??0)} A</dd></div>
      <div><dt>Sitzung / Gesamt</dt><dd>{number(s.session_kwh,2)} / {number(s.energy_kwh??0,2)} kWh</dd></div>
      <div><dt>Leistungsfaktor</dt><dd>{s.power_factor_pct==null?'—':number(s.power_factor_pct)+' %'}</dd></div>
      <div><dt>RFID-UID</dt><dd>{s.rfid_uid||'—'}</dd></div>
      <div><dt>Seriennummer</dt><dd>{s.serial||'—'}</dd></div>
      <div><dt>Firmware / Hardware</dt><dd>{s.firmware||'—'} / {s.hardware_revision??'—'}</dd></div>
      <div><dt>Phasenmodus</dt><dd>{s.phase_count?`${s.phase_count}-phasig`:'—'}</dd></div>
      <div><dt>Failsafe</dt><dd>{s.failsafe_timeout_s??'—'} s · {s.failsafe_current_a??'—'} A</dd></div>
      <div><dt>Fehlercode</dt><dd>{s.error_code?`0x${s.error_code.toString(16).toUpperCase()}`:'Kein Fehler'}</dd></div>
    </dl></details>}
  </article>;
}

function CommissioningStation({station,disabled,onSave,onLocalAccess}:{station:Station;disabled:boolean;onSave:(patch:Partial<Station>)=>Promise<boolean>;onLocalAccess?:(credentials:{username:string;password:string})=>Promise<boolean>}){
  const [draft,setDraft]=useState({name:station.name,model:station.model,host:station.host,port:station.port,device_id:station.device_id});
  useEffect(()=>setDraft({name:station.name,model:station.model,host:station.host,port:station.port,device_id:station.device_id}),[station.name,station.model,station.host,station.port,station.device_id]);
  const set=<K extends keyof typeof draft>(key:K,value:(typeof draft)[K])=>setDraft(old=>({...old,[key]:value}));
  return <section className="panel form-panel commissioning-card"><div className="panel-heading"><div><h2>{station.name}</h2><p>{station.connection_detail}</p></div><span className={`station-status ${station.status}`}><span/>{labels[station.status]}</span></div>
    <div className="field-pair"><label className="field">Bezeichnung<input maxLength={60} value={draft.name} onChange={e=>set('name',e.target.value)}/></label><label className="field">Modell<select value={draft.model} onChange={e=>set('model',e.target.value as Station['model'])}><option value="P30 x">P30 x-series</option><option value="P40">P40</option></select></label></div>
    <label className="field">Lokale IPv4-Adresse<input inputMode="decimal" placeholder="192.168.1.100" value={draft.host} onChange={e=>set('host',e.target.value.trim())}/></label>
    <div className="field-pair"><label className="field">Modbus-Port<input type="number" min="1" max="65535" value={draft.port} onChange={e=>set('port',Number(e.target.value))}/></label><label className="field">Unit ID<input type="number" min="0" max="255" value={draft.device_id} onChange={e=>set('device_id',Number(e.target.value))}/></label></div>
    <div className="device-facts"><span>Seriennummer <strong>{station.serial||'—'}</strong></span><span>Modbus <strong>{station.online?'verbunden':'nicht verbunden'}</strong></span></div>
    <div className="commissioning-actions"><button className="button solid" disabled={disabled} onClick={()=>void onSave(draft)}><Check size={16}/> Speichern & prüfen</button>{station.web_url&&<a className="button secondary" href={station.web_url} target="_blank" rel="noreferrer"><ArrowUpRight size={16}/> KEBA öffnen</a>}</div>
    {onLocalAccess&&<LocalAccessForm station={station} disabled={disabled} onSave={onLocalAccess}/>}
  </section>;
}

function LocalAccessForm({station,disabled,onSave}:{station:Station;disabled:boolean;onSave:(credentials:{username:string;password:string})=>Promise<boolean>}){
  const [username,setUsername]=useState('admin'),[password,setPassword]=useState('');
  const submit=async(e:React.FormEvent<HTMLFormElement>)=>{e.preventDefault();if(await onSave({username,password}))setPassword('');};
  return <form className="local-access-form" onSubmit={submit} autoComplete="off"><h3>Manueller Ladestart</h3><p>Hinterlege den Zugang zur lokalen KEBA-Weboberfläche dieses Ladepunkts. Die Zugangsdaten bleiben auf der lokalen Instanz.</p><span className="quiet-tag">{station.local_start_configured?'Zugang hinterlegt':'Noch nicht eingerichtet'}</span><label className="field">KEBA-Benutzername<input name={`keba-user-${station.id}`} required maxLength={128} value={username} onChange={e=>setUsername(e.target.value)}/></label><label className="field">KEBA-Passwort<input name={`keba-password-${station.id}`} type="password" required maxLength={256} value={password} onChange={e=>setPassword(e.target.value)} placeholder={station.local_start_configured?'Zum Ersetzen neu eingeben':''}/></label><button className="button secondary" disabled={disabled||!password||!station.host}><Check size={16}/> Zugang speichern</button></form>;
}

function UserManagement(){
  const [users,setUsers]=useState<User[]>([]),[username,setUsername]=useState(''),[password,setPassword]=useState(''),[role,setRole]=useState<User['role']>('operator'),[error,setError]=useState('');
  const load=async()=>{const r=await fetch('/auth/users');if(r.ok){const body:{users:User[]}=await r.json();setUsers(body.users);}};
  useEffect(()=>{void load();},[]);
  const add=async(e:React.FormEvent)=>{e.preventDefault();setError('');const r=await fetch('/auth/users',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username,password,role})});const body:{detail?:string}=await r.json();if(!r.ok){setError(body.detail||'Benutzer konnte nicht angelegt werden.');return;}setUsername('');setPassword('');await load();};
  return <section className="panel form-panel" style={{marginTop:20}}><div className="panel-heading"><div><h2>Benutzerverwaltung</h2><p>Admins verwalten Konten, Operatoren dürfen regeln, Viewer sehen nur Statusdaten.</p></div></div><div className="events-table">{users.map(u=><div className="event-row" key={u.id}><span><strong>{u.username}</strong></span><span>{u.role}</span><span>angelegt {new Date(u.created_at*1000).toLocaleDateString('de-DE')}</span></div>)}</div><form onSubmit={add}><div className="field-pair"><label className="field">Benutzername oder E-Mail<input required minLength={3} maxLength={254} value={username} onChange={e=>setUsername(e.target.value)}/></label><label className="field">Rolle<select value={role} onChange={e=>setRole(e.target.value as User['role'])}><option value="operator">Operator</option><option value="viewer">Viewer</option><option value="admin">Administrator</option></select></label></div><label className="field">Temporäres Passwort (mindestens 12 Zeichen)<input required type="password" minLength={12} value={password} onChange={e=>setPassword(e.target.value)}/></label>{error&&<p className="alert error">{error}</p>}<button className="button solid">Benutzer anlegen</button></form></section>;
}

createRoot(document.getElementById('root')!).render(<React.StrictMode><AuthGate/></React.StrictMode>);
