import React,{useEffect,useRef,useState} from 'react';
import {createRoot} from 'react-dom/client';
import {Activity,ArrowDownLeft,ArrowUpRight,ArrowRight,Building2,Check,ChevronRight,CircleHelp,Clock3,Cloud,FlaskConical,Gauge,History,LayoutDashboard,LoaderCircle,Pause,Play,PlugZap,RefreshCw,Settings2,ShieldCheck,Unplug,WifiOff,Zap} from 'lucide-react';
import type {State,Sample,Event,Settings,Simulation,Station} from './types';
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

function Chart({samples,limit}:{samples:Sample[];limit:number}){
  const w=800,h=172,pad=8, max=Math.max(30,limit,...samples.map(s=>s.total_kw));
  const x=(i:number)=>pad+i*(w-pad*2)/Math.max(1,samples.length-1);
  const y=(v:number)=>h-8-(v/max)*(h-20);
  const line=(field:'total_kw'|'building_kw')=>samples.map((s,i)=>`${x(i)},${y(s[field])}`).join(' ');
  return <div className="chart"><div className="axis-labels"><span>{max.toFixed(0)} kW</span><span>{(max/2).toFixed(0)}</span><span>0</span></div>
    <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" role="img" aria-label="Verlauf von Netzbezug und Gebäudeverbrauch">
      <defs><linearGradient id="fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#a7cbb7" stopOpacity=".7"/><stop offset="100%" stopColor="#a7cbb7" stopOpacity=".06"/></linearGradient></defs>
      {[0,max/2,max].map(v=><line key={v} x1={0} x2={w} y1={y(v)} y2={y(v)} stroke="#e9ebe5" strokeDasharray="3 5"/>)}
      <line x1="0" x2={w} y1={y(limit)} y2={y(limit)} stroke="#c09559" strokeDasharray="5 5"/>
      {samples.length>1&&<><polygon points={`${x(0)},${h} ${line('total_kw')} ${x(samples.length-1)},${h}`} fill="url(#fill)"/><polyline points={line('total_kw')} fill="none" stroke="#317e5b" strokeWidth="2.5" vectorEffect="non-scaling-stroke"/><polyline points={line('building_kw')} fill="none" stroke="#9aaca3" strokeWidth="2" vectorEffect="non-scaling-stroke"/></>}
    </svg>
    {samples.length<2&&<p className="chart-empty">Der Verlauf entsteht mit den nächsten Messungen.</p>}
    <div className="chart-times"><span>{samples.length?time(samples[0].ts):'Jetzt'}</span><span>{samples.length?time(samples[samples.length-1].ts):'Jetzt'}</span></div>
  </div>;
}

function App(){
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
      await refresh();setEvents(await read<Event[]>('events'));setNotice('Änderung von der lokalen Instanz bestätigt.');return true;
    }catch(e){setNotice(e instanceof Error?e.message:'Keine Bestätigung erhalten. Status vor erneutem Versuch prüfen.');return false;}
    finally{setBusy(false);}
  }
  const disabled=busy||offline||!state;
  const commissioning=state?.mode==='commissioning';
  const nav=[{id:'overview',label:'Übersicht',icon:LayoutDashboard},{id:'history',label:'Ereignisse',icon:History},commissioning?{id:'commissioning',label:'Inbetriebnahme',icon:PlugZap}:{id:'simulation',label:'Simulation',icon:FlaskConical},{id:'settings',label:'Einstellungen',icon:Settings2}];
  const changeSettings=(patch:Partial<Settings>)=>{setSettings(s=>s?{...s,...patch}:s);setDirtySettings(true);};
  const changeSim=(patch:Partial<Simulation>)=>{setSim(s=>s?{...s,...patch}:s);setDirtySim(true);};
  const applyScenario=async(patch:Partial<Simulation>)=>{if(state&&await command('simulation',{...state.simulation,...patch}))setDirtySim(false);};

  return <div className="app-shell">
    <aside className="sidebar"><a className="brand" href="#" onClick={e=>{e.preventDefault();setPage('overview');}}><span className="brand-symbol"><Zap size={23} fill="currentColor"/></span><span>ladepark<span className="brand-sub">ENERGIE IM GLEICHGEWICHT</span></span></a>
      <div className="site-switch"><span className="site-icon"><Building2 size={19}/></span><div><strong>{state?.site_name||'Unser Ladepark'}</strong><small>4 Ladepunkte · KEBA</small></div></div>
      <p className="nav-label">VERWALTUNG</p><nav>{nav.map(n=><button key={n.id} className={page===n.id?'nav-item selected':'nav-item'} onClick={()=>setPage(n.id)}><n.icon size={18}/>{n.label}{page===n.id&&<span className="nav-dot"/>}</button>)}</nav>
      <div className="sidebar-bottom"><div className="local-info"><span className={`status-dot ${offline?'red':''}`}/><div><strong>{offline?'Instanz nicht erreichbar':'Lokale Instanz'}</strong><small>Python · FastAPI · SQLite</small></div></div><span className="version">MVP 0.1 <span>{commissioning?'INBETRIEBNAHME':'SIMULATION'}</span></span></div>
    </aside>
    <main><header className="topbar"><div className="breadcrumb">Ladepark <ChevronRight size={13}/><strong>{nav.find(n=>n.id===page)?.label}</strong></div><div className="top-right"><span className="simulation-pill">{commissioning?<PlugZap size={13}/>:<FlaskConical size={13}/>} {commissioning?'Lokale Inbetriebnahme':'Simulationsmodus'}</span><span className="avatar">PK</span></div></header>
    <div className="content"><div className="page-heading"><div><p className="eyebrow">ENERGIEMANAGEMENT</p><h1>{page==='overview'?(commissioning?'Echte Geräte im lokalen Netz.':'Alles im Gleichgewicht.'):page==='simulation'?'Szenarien ausprobieren.':page==='commissioning'?'Ladestationen verbinden.':page==='settings'?'Dein Ladepark. Deine Regeln.':'Jede Änderung im Blick.'}</h1><p className="subtitle">{page==='overview'?(commissioning?'Live-Verbindung zu den KEBA-Ladestationen, derzeit ohne Gebäudemessung.':'Ein Anschluss. Vier Ladepunkte. Intelligent verteilt.'):page==='simulation'?'Teste den Regler mit wechselnder Last und unterbrochenen Verbindungen.':page==='commissioning'?'IP-Adressen speichern, Modbus prüfen und die KEBA-Weboberfläche öffnen.':page==='settings'?'Die lokale Instanz prüft und bestätigt jede Änderung.':'Regelzustände und bestätigte Einstellungen aus der lokalen Instanz.'}</p></div>
      {page==='overview'&&state&&!commissioning&&<button className="button secondary" disabled={disabled} onClick={()=>void command('settings',{...state.settings,paused:!state.settings.paused})}>{state.settings.paused?<Play size={15}/>:<Pause size={15}/>} {state.settings.paused?'Alle fortsetzen':'Alle pausieren'}</button>}
    </div>
    {offline&&<div className="alert error" role="alert"><WifiOff size={20}/><div><strong>Verbindung zur lokalen Instanz unterbrochen</strong><p>Die angezeigten Werte sind veraltet. Änderungen sind gesperrt. Der lokale Regler kann unabhängig weiterlaufen.</p></div></div>}
    {!state?<div className="loading"><LoaderCircle className="spin"/><h2>{offline?'Auf lokale Instanz warten':'Ladepark wird verbunden'}</h2><p>Messwerte werden von der lokalen FastAPI abgerufen.</p></div>:<>
      {commissioning&&!offline&&<div className="alert warning" role="alert"><ShieldCheck size={21}/><div><strong>Inbetriebnahme ohne Gebäudemessung</strong><p>Es werden nur echte Geräte gelesen. Automatische Leistungsfreigaben und Modbus-Schreibzugriffe sind gesperrt.</p></div><button className="text-button" onClick={()=>setPage('commissioning')}>Geräte öffnen <ArrowRight size={15}/></button></div>}
      {state.status==='safe'&&!offline&&<div className="alert warning" role="alert"><ShieldCheck size={21}/><div><strong>Sicherer Halt aktiv</strong><p>Alle Ladepunkte sind pausiert, bis Zähler und Ladepunkte wieder erreichbar sind.</p></div><button className="text-button" onClick={()=>setPage('simulation')}>Simulation öffnen <ArrowRight size={15}/></button></div>}
      {state.overload&&!offline&&<div className="alert error" role="alert"><Activity size={20}/><div><strong>Gebäudelast überschreitet eine Anschlussgrenze</strong><p>Die Ladeleistung wurde reduziert. Der Regler kann den Gebäudeverbrauch selbst nicht senken.</p></div></div>}
      {page==='overview'&&<>
        <section className={`metrics ${offline?'stale':''}`}>
          <article className="metric primary"><div className="metric-label">Aktueller Netzbezug <ArrowDownLeft size={19}/></div><div className="metric-value">{state.meter_online?number(state.total_kw):'—'} <span>kW</span></div><div className="metric-meter"><i style={{width:`${Math.min(100,state.total_kw/state.settings.power_limit_kw*100)}%`}}/></div><div className="metric-foot"><span>von {number(state.settings.power_limit_kw,0)} kW Anschlussleistung</span><strong>{state.meter_online?`${number(state.total_kw/state.settings.power_limit_kw*100,0)} %`:'—'}</strong></div></article>
          <article className="metric"><div className="metric-label">Gebäudeverbrauch <Building2 size={19}/></div><div className="metric-value">{state.meter_online?number(state.building_kw):'—'} <span>kW</span></div><div className="metric-foot"><span className="legend grey"/> Gebäude hat Vorrang</div></article>
          <article className="metric"><div className="metric-label">Ladeleistung <PlugZap size={20}/></div><div className="metric-value">{number(state.charging_kw)} <span>kW</span></div><div className="metric-foot"><span className="legend green"/>{state.stations.filter(s=>s.status==='charging').length} von 4 Ladepunkten laden</div></article>
          <article className="metric"><div className="metric-label">Verfügbares Budget <Gauge size={19}/></div><div className="metric-value">{state.meter_online?number(state.headroom_kw):'—'} <span>kW</span></div><div className="metric-foot">Nach {number(state.settings.reserve_kw)} kW Regelreserve</div></article>
        </section>
        <section className="overview-grid"><article className="panel chart-panel"><div className="panel-heading"><div><h2>Leistung im Verlauf</h2><p>{commissioning?'Messwerte der über Modbus verbundenen Ladepunkte':'Echte Messpunkte der laufenden Simulation'}</p></div><span className="quiet-tag"><Clock3 size={12}/> Letzte 15 Minuten</span></div><div className="chart-legend"><span><i className="legend green"/> {commissioning?'Ladeleistung':'Netzbezug'}</span>{!commissioning&&<><span><i className="legend grey"/> Gebäude</span><span><i className="limit-line"/> Anschlussgrenze</span></>}</div><Chart samples={samples} limit={state.settings.power_limit_kw}/></article>
          <article className="panel phase-panel"><div className="panel-heading"><div><h2>Phasen im Blick</h2><p>Strom je Außenleiter</p></div><Activity size={18}/></div>{state.phase_currents_a.map((a,i)=><div className="phase" key={i}><div><strong>L{i+1}</strong><span>{commissioning?(state.stations.some(s=>s.online)?number(a):'—'):(state.meter_online?number(a):'—')} <small>/ {state.settings.phase_limit_a} A</small></span></div><div className="phase-track"><i className={a>state.settings.phase_limit_a?'over':''} style={{width:`${Math.min(100,a/state.settings.phase_limit_a*100)}%`}}/></div></div>)}<div className="phase-note"><CircleHelp size={13}/><span>{commissioning?'Ohne Gebäudemessung werden nur Ladepunktströme angezeigt.':`${state.settings.phase_limit_a} A ist eine Simulationsannahme.`}</span></div></article></section>
        <div className="section-heading"><div><h2>Deine Ladepunkte <span>04</span></h2><p>{commissioning?'Live-Status der lokal konfigurierten KEBA-Geräte.':'Faire Verteilung mit rotierender Reihenfolge bei knapper Leistung.'}</p></div><span className="live-label"><span className={`status-dot ${offline?'red':''}`}/>{offline?'Daten veraltet':'Live · alle 2 Sekunden'}</span></div>
        <section className="station-grid">{state.stations.map(s=><StationCard key={s.id} station={s} disabled={disabled} globalPaused={state.settings.paused} commissioning={commissioning} onPause={()=>void command('station',{paused:!s.paused},s.id)} onPriority={()=>void command('station',{priority:s.priority==='high'?'normal':'high'},s.id)}/>)}</section>
        <div className="bottom-row"><div><ShieldCheck size={16}/><span>{commissioning?'Nur-Lesen · keine Leistungsfreigaben':'Lastmanagement lokal · Betrieb ohne Cloud-Verbindung vorgesehen'}</span></div><div><Cloud size={16}/><span>{commissioning?'Monta bleibt unabhängig per OCPP verbunden':'Monta extern · Verbindung im MVP nicht geprüft'}</span></div></div>
      </>}
      {page==='simulation'&&sim&&<div className="settings-grid"><section className="panel form-panel"><div className="panel-heading"><div><h2>Gebäudelast simulieren</h2><p>Diese Änderungen wirken ausschließlich auf simulierte Geräte.</p></div><FlaskConical size={20}/></div><label className="range-label" htmlFor="building">Gebäudeverbrauch <strong>{number(sim.building_kw)} kW</strong></label><input id="building" type="range" min="0" max="30" step="0.5" value={sim.building_kw} onChange={e=>changeSim({building_kw:Number(e.target.value)})}/><div className="range-ends"><span>0 kW</span><span>30 kW</span></div><label className="field">Verteilung auf die Phasen<select value={sim.profile} onChange={e=>changeSim({profile:e.target.value as Simulation['profile']})}><option value="balanced">Gleichmäßig · 33 / 33 / 33 %</option><option value="uneven">Ungleichmäßig · 60 / 25 / 15 %</option></select></label><label className="toggle-row"><span><strong>Zähler erreichbar</strong><small>Bei Ausfall pausieren alle Ladepunkte.</small></span><input type="checkbox" checked={sim.meter_online} onChange={e=>changeSim({meter_online:e.target.checked})}/></label><button className="button solid" disabled={disabled||!dirtySim} onClick={async()=>{if(await command('simulation',sim))setDirtySim(false);}}>{busy?<LoaderCircle className="spin" size={16}/>:<Check size={16}/>} Szenario anwenden</button></section>
        <section className="panel form-panel"><div className="panel-heading"><div><h2>Schnell ausprobieren</h2><p>Ein Klick setzt ein vollständiges Lastszenario.</p></div></div>{[{label:'Normaler Betrieb',desc:'7 kW Gebäudelast, alle vier Fahrzeuge verbunden',icon:Building2,data:{building_kw:7,profile:'balanced',meter_online:true,disconnected:[],offline:[]}},{label:'Hoher Gebäudeverbrauch',desc:'18 kW Last, knappe Leistung für Fahrzeuge',icon:Gauge,data:{building_kw:18,profile:'balanced',meter_online:true,disconnected:[],offline:[]}},{label:'Eine Phase am Limit',desc:'12 kW, davon 60 % auf Phase L1',icon:Activity,data:{building_kw:12,profile:'uneven',meter_online:true,disconnected:[],offline:[]}},{label:'Zählerausfall',desc:'Der Regler wechselt in den sicheren Halt',icon:WifiOff,data:{meter_online:false}}].map(p=><button className="scenario" key={p.label} disabled={disabled} onClick={()=>void applyScenario(p.data as Partial<Simulation>)}><span className="scenario-icon"><p.icon size={20}/></span><span><strong>{p.label}</strong><small>{p.desc}</small></span><ArrowUpRight size={17}/></button>)}</section>
        <section className="panel form-panel full"><div className="panel-heading"><div><h2>Fahrzeuge & Verbindungen</h2><p>Fahrzeug abstecken oder einen Kommunikationsausfall nachstellen.</p></div></div>{state.stations.map(s=><div className="connection-row" key={s.id}><span><strong>{s.name}</strong><small>{s.model}</small></span><button className="button secondary" disabled={disabled} onClick={()=>void applyScenario({disconnected:s.connected?[...state.simulation.disconnected,s.id]:state.simulation.disconnected.filter(id=>id!==s.id)})}>{s.connected?<Unplug size={15}/>:<PlugZap size={15}/>} {s.connected?'Fahrzeug abstecken':'Fahrzeug verbinden'}</button><button className="button secondary" disabled={disabled} onClick={()=>void applyScenario({offline:s.online?[...state.simulation.offline,s.id]:state.simulation.offline.filter(id=>id!==s.id)})}>{s.online?<WifiOff size={15}/>:<RefreshCw size={15}/>} {s.online?'Verbindung trennen':'Verbindung herstellen'}</button></div>)}</section></div>}
      {page==='commissioning'&&commissioning&&<div className="settings-grid commissioning-grid">{state.stations.map(s=><CommissioningStation key={s.id} station={s} disabled={disabled} onSave={patch=>command('station',patch,s.id)}/>)}</div>}
      {page==='settings'&&settings&&<div className="settings-grid"><section className="panel form-panel"><div className="panel-heading"><div><h2>Standort & Grenzen</h2><p>{commissioning?'Die Grenzwerte werden gespeichert, aber ohne Gebäudenzähler noch nicht geregelt.':'Werte gelten für die Simulation, nicht als Installationsfreigabe.'}</p></div></div><label className="field">Name des Standorts<input maxLength={60} value={settings.site_name} onChange={e=>changeSettings({site_name:e.target.value})}/></label><div className="field-pair"><label className="field">Anschlussgrenze (kW)<input type="number" min="5" max="25" step="0.5" value={settings.power_limit_kw} onChange={e=>changeSettings({power_limit_kw:Number(e.target.value)})}/></label><label className="field">Grenze je Phase (A)<input type="number" min="6" max="35" value={settings.phase_limit_a} onChange={e=>changeSettings({phase_limit_a:Number(e.target.value)})}/></label></div><div className="field-pair"><label className="field">Regelreserve (kW)<input type="number" min="0.5" max="5" step="0.5" value={settings.reserve_kw} onChange={e=>changeSettings({reserve_kw:Number(e.target.value)})}/></label><label className="field">Warteplatzrotation (s)<input type="number" min="30" max="900" step="30" value={settings.rotation_seconds} onChange={e=>changeSettings({rotation_seconds:Number(e.target.value)})}/></label></div><button className="button solid" disabled={disabled||!dirtySettings} onClick={async()=>{if(await command('settings',settings))setDirtySettings(false);}}><Check size={16}/> Einstellungen speichern</button></section><section className="panel form-panel architecture"><div className="panel-heading"><div><h2>So arbeitet dein System</h2><p>Die Regelung bleibt vor Ort.</p></div></div>{[{icon:Cloud,title:'Cloudflare Worker',text:'React-Oberfläche und geschützte API-Weiterleitung.'},{icon:ShieldCheck,title:'Lokale Python-Instanz',text:commissioning?'Liest die echten Wallboxen im lokalen Netz aus.':'Ein eigenständiger Regler verteilt die verfügbare Leistung.'},{icon:History,title:'SQLite',text:'Einstellungen, Ereignisse und Verlauf bleiben lokal gespeichert.'},{icon:PlugZap,title:'Monta',text:'Freischaltung und Abrechnung bleiben weiterhin über OCPP bei Monta.'}].map(s=><div className="architecture-row" key={s.title}><s.icon size={22}/><div><strong>{s.title}</strong><p>{s.text}</p></div></div>)}<div className="info-note">{commissioning?'Inbetriebnahme aktiv: Modbus wird nur gelesen. Schreibzugriffe bleiben bis zur Zähleranbindung gesperrt.':'Hardwaresteuerung ist im Simulationsmodus deaktiviert.'}</div></section></div>}
      {page==='history'&&<section className="panel events-panel"><div className="panel-heading"><div><h2>Ereignisprotokoll</h2><p>Die letzten 80 Ereignisse · lokal gespeichert</p></div><span className="quiet-tag">{events.length} Einträge</span></div><div className="events-table"><div className="event-row event-head"><span>ZEITPUNKT</span><span>STATUS</span><span>EREIGNIS</span></div>{events.map(e=><div className="event-row" key={e.id}><span>{new Date(e.ts*1000).toLocaleDateString('de-DE',{day:'2-digit',month:'2-digit'})} · {time(e.ts)}</span><span><i className={`event-tag ${e.level}`}>{e.level==='warning'?'Hinweis':'Information'}</i></span><span>{e.message}</span></div>)}</div></section>}
      <footer><span>KEBA P30 x + P40 · Lokales Lastmanagement</span><span>{state.mode==='simulation'?'Simulierte Messwerte':'Messwerte'} · Stand {time(state.timestamp)} Uhr</span></footer>
    </>}
    </div></main>{notice&&<div className="toast" role="status"><CircleHelp size={17}/>{notice}<button aria-label="Meldung schließen" onClick={()=>setNotice('')}>×</button></div>}
  </div>;
}

function StationCard({station:s,disabled,globalPaused,commissioning,onPause,onPriority}:{station:Station;disabled:boolean;globalPaused:boolean;commissioning:boolean;onPause:()=>void;onPriority:()=>void}){
  return <article className={`station-card ${s.status==='charging'?'is-charging':''}`}><div className="station-top"><div className="station-icon"><PlugZap size={23}/></div><span className={`station-status ${s.status}`}><span/>{labels[s.status]}</span></div><h3>{s.name}</h3><div className="station-model">KEBA {s.model} <span>{commissioning?(s.host||'IP fehlt'):'3-phasig'}</span></div><div className="station-power">{number(s.power_kw)} <span>kW</span></div><div className="station-track"><i style={{width:`${s.current_a/s.max_current_a*100}%`}}/></div><div className="station-details"><span>{s.current_a} / {s.max_current_a} A</span><span>{commissioning?`${number(s.energy_kwh||0,2)} kWh Zähler`:`${number(s.session_kwh,2)} kWh simuliert`}</span></div><p className="station-reason">{s.connection_detail||reasons[s.status]}</p>{!commissioning&&<div className="station-actions"><button title="Priorität ändern" aria-label={`Priorität ${s.name}`} disabled={disabled} className={s.priority==='high'?'priority high':'priority'} onClick={onPriority}><ArrowUpRight size={13}/>{s.priority==='high'?'Priorisiert':'Normal'}</button><button aria-label={`${s.paused?'Fortsetzen':'Pausieren'} ${s.name}`} title={globalPaused?'Gesamter Ladepark ist pausiert':s.paused?'Fortsetzen':'Pausieren'} disabled={disabled||globalPaused} className="icon-button" onClick={onPause}>{s.paused?<Play size={16}/>:<Pause size={16}/>}</button></div>}</article>;
}

function CommissioningStation({station,disabled,onSave}:{station:Station;disabled:boolean;onSave:(patch:Partial<Station>)=>Promise<boolean>}){
  const [draft,setDraft]=useState({name:station.name,model:station.model,host:station.host,port:station.port,device_id:station.device_id});
  useEffect(()=>setDraft({name:station.name,model:station.model,host:station.host,port:station.port,device_id:station.device_id}),[station.name,station.model,station.host,station.port,station.device_id]);
  const set=<K extends keyof typeof draft>(key:K,value:(typeof draft)[K])=>setDraft(old=>({...old,[key]:value}));
  return <section className="panel form-panel commissioning-card"><div className="panel-heading"><div><h2>{station.name}</h2><p>{station.connection_detail}</p></div><span className={`station-status ${station.status}`}><span/>{labels[station.status]}</span></div>
    <div className="field-pair"><label className="field">Bezeichnung<input maxLength={60} value={draft.name} onChange={e=>set('name',e.target.value)}/></label><label className="field">Modell<select value={draft.model} onChange={e=>set('model',e.target.value as Station['model'])}><option value="P30 x">P30 x-series</option><option value="P40">P40</option></select></label></div>
    <label className="field">Lokale IPv4-Adresse<input inputMode="decimal" placeholder="192.168.1.100" value={draft.host} onChange={e=>set('host',e.target.value.trim())}/></label>
    <div className="field-pair"><label className="field">Modbus-Port<input type="number" min="1" max="65535" value={draft.port} onChange={e=>set('port',Number(e.target.value))}/></label><label className="field">Unit ID<input type="number" min="0" max="255" value={draft.device_id} onChange={e=>set('device_id',Number(e.target.value))}/></label></div>
    <div className="device-facts"><span>Seriennummer <strong>{station.serial||'—'}</strong></span><span>Modbus <strong>{station.online?'verbunden':'nicht verbunden'}</strong></span></div>
    <div className="commissioning-actions"><button className="button solid" disabled={disabled} onClick={()=>void onSave(draft)}><Check size={16}/> Speichern & prüfen</button>{station.web_url&&<a className="button secondary" href={station.web_url} target="_blank" rel="noreferrer"><ArrowUpRight size={16}/> KEBA öffnen</a>}</div>
  </section>;
}

createRoot(document.getElementById('root')!).render(<React.StrictMode><App/></React.StrictMode>);
